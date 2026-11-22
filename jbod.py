"""
JBOD drive-bay fans: drive temperatures -> fan % -> ESP32 (esp32/jbod_fans).

The enclosure's own fans are removed, so these three fans are the drives'
only cooling. That shapes every rule here:

  target   = curve(hottest JBOD drive), never below jbod_min_pct
             all drives spun down / no temps -> jbod_min_pct (the expander
             and PSUs still need air)
  up       at once
  down     hold jbod_hold_sec, then jbod_step_down_pct every jbod_step_down_sec
  sensors  temps unreadable for 3 polls in a row -> jbod_failsafe_pct

The ESP32 enforces its own floor too: if it stops hearing from us for `ttl`
seconds it goes to jbod_failsafe_pct (or its exhaust-probe curve) by itself,
so a dead container never leaves the drives without air.

Drive temps come from one of:
  unraid    /var/local/emhttp/disks.ini (mounted at UNRAID_DISKS_INI). No
            privileges needed, never wakes a drive. Unraid refreshes it every
            "Tunable (poll_attributes)" seconds (Settings -> Disk Settings);
            the default is 1800 s, so set it to 60.
  smartctl  `smartctl -n standby` per device. Fresh every poll, skips sleeping
            drives, but the container needs the disks (privileged or devices:).
"""

import collections
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

DISKS_INI = os.environ.get("UNRAID_DISKS_INI", "/unraid/disks.ini")
TOKEN = os.environ.get("JBOD_TOKEN", "")
SMART_EVERY_SEC = 30          # smartctl is slower; temps move slowly anyway
FAIL_POLLS = 3
HISTORY_LEN = 720             # one point per poll -> 1 h at 5 s

_lock = threading.Lock()
state = {
    "enabled": False,
    "phase": "off",           # off | ok | hold | ramp | failsafe | no-link
    "target_pct": None,
    "sent_pct": None,
    "reason": "",
    "hottest": None,          # {"name", "temp_c"}
    "drives": [],             # [{name, device, temp_c, standby, used, expander}]
    "esp": None,              # last /set response
    "esp_ok": False,
    "esp_error": None,
    "esp_ts": None,
    "source_error": None,
    "countdown_sec": 0,
}
history = collections.deque(maxlen=HISTORY_LEN)   # [ts, hottest_c, pct, avg_rpm, probe_c]


def snapshot() -> dict:
    with _lock:
        snap = dict(state)
        snap["history"] = list(history)[-180:]
    return snap


def _set(**kw) -> None:
    with _lock:
        state.update(kw)


# ---------------------------------------------------------------------------
# Drive temperatures
# ---------------------------------------------------------------------------

def parse_disks_ini(text: str) -> list:
    """Unraid's disks.ini -> [{name, device, id, temp_c, standby}].
    Sections look like ["disk1"] followed by key="value" lines; temp is "*"
    while a drive is spun down."""
    drives, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^\["?([^"\]]+)"?\]$', line)
        if m:
            cur = {"name": m.group(1)}
            drives.append(cur)
            continue
        m = re.match(r'^([A-Za-z0-9_]+)="(.*)"$', line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    out = []
    for d in drives:
        dev = d.get("device", "")
        if not dev or d.get("type", "").lower() == "flash":
            continue
        t = d.get("temp", "*")
        try:
            temp = float(t)
        except ValueError:
            temp = None
        out.append({"name": d["name"], "device": dev, "id": d.get("id", ""),
                    "temp_c": temp, "standby": temp is None})
    return out


def _smartctl(dev: str):
    """(temp_c, standby) for one device. -n standby: don't wake it to ask."""
    path = dev if dev.startswith("/") else "/dev/" + dev
    try:
        r = subprocess.run(["smartctl", "-n", "standby", "-j", "-A", path],
                           capture_output=True, text=True, timeout=15)
        j = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, False
    if r.returncode & 2 and "temperature" not in j:
        return None, True                      # in standby, left asleep
    t = (j.get("temperature") or {}).get("current")
    return (float(t) if t is not None else None), False


def _smartctl_drives(patterns: list) -> list:
    devs = patterns or sorted(os.path.basename(p) for p in glob.glob("/dev/sd[a-z]") + glob.glob("/dev/sd[a-z][a-z]"))
    out = []
    for dev in devs:
        temp, standby = _smartctl(dev)
        out.append({"name": os.path.basename(dev), "device": os.path.basename(dev), "id": "",
                    "temp_c": temp, "standby": standby})
    return out


def behind_expander(device: str) -> bool:
    """True when the disk hangs off a SAS expander -- i.e. it is in a JBOD
    shelf like the D3600, not on the server's own backplane."""
    try:
        return "/expander-" in os.path.realpath(f"/sys/block/{device}")
    except OSError:
        return False


def _patterns(s: dict) -> list:
    return [p.strip() for p in (s.get("jbod_disks") or "").split(",") if p.strip()]


def select_drives(drives: list, patterns: list) -> list:
    """Mark which drives count. Explicit patterns match name, device or serial
    (substring, 6+ characters so "disk1" never matches "disk10"). No patterns: drives behind a SAS expander, else all."""
    for d in drives:
        d["expander"] = behind_expander(d["device"])
    if patterns:
        for d in drives:
            d["used"] = any(p == d["name"] or p == d["device"] or (len(p) >= 6 and p in d.get("id", ""))
                            for p in patterns)
    else:
        any_exp = any(d["expander"] for d in drives)
        for d in drives:
            d["used"] = d["expander"] or not any_exp
    return drives


def read_drives(s: dict, cache: dict, now: float) -> list:
    if s["jbod_source"] == "smartctl":
        if cache.get("ts") and now - cache["ts"] < SMART_EVERY_SEC:
            return cache["drives"]
        drives = _smartctl_drives(_patterns(s))
    else:
        with open(DISKS_INI) as f:
            drives = parse_disks_ini(f.read())
    drives = select_drives(drives, _patterns(s) if s["jbod_source"] == "unraid" else [])
    cache.update(ts=now, drives=drives)
    return drives


# ---------------------------------------------------------------------------
# Decision (pure, unit-tested)
# ---------------------------------------------------------------------------

def curve_pct(temp: float, curve: list) -> float:
    if temp < curve[0][0]:
        return curve[0][1]
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp < t1:
            return p0 + (temp - t0) / (t1 - t0) * (p1 - p0)
    return curve[-1][1]


def compute_target(drives: list, s: dict):
    """(pct, reason, hottest) from the drives marked used."""
    used = [d for d in drives if d.get("used")]
    temps = [d for d in used if d["temp_c"] is not None]
    lo = s["jbod_min_pct"]
    if not temps:
        why = "all JBOD drives spun down" if used else "no JBOD drives found"
        return lo, why, None
    hot = max(temps, key=lambda d: d["temp_c"])
    pct = curve_pct(hot["temp_c"], s["jbod_curve"])
    pct = max(lo, min(100, int(-(-pct // 5) * 5)))   # round up to 5 %
    return pct, f"{hot['name']} {hot['temp_c']:.0f}°C", {"name": hot["name"], "temp_c": hot["temp_c"]}


class Slew:
    """Up at once; down after a hold, one step at a time."""

    def __init__(self):
        self.pct = None
        self.need_ts = 0.0        # last time the current pct was still needed
        self.step_ts = None       # last step down (None = not ramping yet)

    def decide(self, target: int, now: float, s: dict):
        """-> (pct, phase, countdown_sec)"""
        if self.pct is None or target >= self.pct:
            self.pct, self.need_ts, self.step_ts = target, now, None
            return self.pct, "ok", 0
        hold_end = self.need_ts + s["jbod_hold_sec"]
        if now < hold_end:
            return self.pct, "hold", round(hold_end - now)
        if self.step_ts is None or now - self.step_ts >= s["jbod_step_down_sec"]:
            self.pct = max(target, self.pct - s["jbod_step_down_pct"])
            self.step_ts = now
        if self.pct <= target:
            return self.pct, "ok", 0
        return self.pct, "ramp", round(s["jbod_step_down_sec"] - (now - self.step_ts))


# ---------------------------------------------------------------------------
# ESP32 link
# ---------------------------------------------------------------------------

def send(host: str, pct: int, ttl: int, failsafe: int, temp, timeout: float = 3.0) -> dict:
    q = {"pct": pct, "ttl": ttl, "fs": failsafe}
    if temp is not None:
        q["t"] = f"{temp:.1f}"
    req = urllib.request.Request(f"http://{host}/set?{urllib.parse.urlencode(q)}", data=b"", method="POST",
                                 headers={"X-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_forever(log) -> None:
    slew, cache, fails, last_phase, link_was = Slew(), {}, 0, None, None
    while True:
        s = config.load()
        now = time.time()
        if not s["jbod_enabled"]:
            _set(enabled=False, phase="off", reason="disabled (the ESP32 runs its own failsafe)")
            time.sleep(2)
            continue
        try:
            drives = read_drives(s, cache, now)
            fails = 0
            target, reason, hottest = compute_target(drives, s)
            pct, phase, countdown = slew.decide(target, now, s)
            _set(drives=drives, hottest=hottest, source_error=None)
        except Exception as e:  # unreadable disks.ini, smartctl missing, ...
            fails += 1
            _set(source_error=f"{type(e).__name__}: {e}")
            target, reason, hottest = None, "drive temps unreadable", None
            if fails >= FAIL_POLLS:
                pct, phase, countdown = s["jbod_failsafe_pct"], "failsafe", 0
                slew.pct, slew.need_ts = pct, now
            else:
                pct, phase, countdown = (slew.pct or s["jbod_failsafe_pct"]), "ok", 0
        ttl = int(max(15, s["jbod_poll_sec"] * 4))
        try:
            resp = send(s["jbod_host"], pct, ttl, s["jbod_failsafe_pct"], hottest and hottest["temp_c"])
            rpms = [r for r in resp.get("rpm", []) if r]
            history.append([round(now, 1), hottest and hottest["temp_c"], resp.get("pct", pct),
                            round(sum(rpms) / len(rpms)) if rpms else None, resp.get("probe_c")])
            _set(esp=resp, esp_ok=True, esp_error=None, esp_ts=now)
            if link_was is False:
                log(f"JBOD: ESP32 at {s['jbod_host']} reachable again")
            link_was = True
        except (OSError, urllib.error.URLError, ValueError) as e:
            _set(esp_ok=False, esp_error=str(getattr(e, "reason", e)))
            if link_was is not False:
                log(f"JBOD: can't reach ESP32 at {s['jbod_host']} ({getattr(e, 'reason', e)}); it will fail safe on its own")
            link_was = False
            phase = "no-link"
        if phase == "failsafe" and last_phase != "failsafe":
            log(f"JBOD: drive temps unreadable, fans to {pct}%")
        prev = state.get("sent_pct")
        if prev is not None and prev != pct:
            log(f"JBOD fans {prev}% → {pct}% ({reason})")
        _set(enabled=True, phase=phase, target_pct=target, sent_pct=pct, reason=reason, countdown_sec=countdown)
        last_phase = phase
        time.sleep(s["jbod_poll_sec"])
