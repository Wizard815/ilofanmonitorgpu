"""
Background control loop (daemon thread started by app.py), an iLO writer
thread, and a slower thread that reads actual fan speeds back from iLO.

Control model -- one curve, one floor:

  control temp = hottest of (junction, HBM + mem_offset_c) across GPUs
  predicted    = control temp + rise rate * predict_sec   (only when rising)
  target       = max(curve(predicted), load_floor if any GPU is loaded)
                 rounded UP to quantize_pct

  baseline     = what iLO runs the fans at on its own (learned from the
                 readback while iLO is in charge, or set by hand). A floor at
                 or below it changes nothing, so:

  TAKEOVER  target above baseline and above the floor -> raise at once
  HOLD      target dropped -> keep the floor for hold_sec; the timer does
            not run while the temperature is still rising
  RAMP      step down step_down_pct every step_down_sec
  HANDOFF   the next step would land at/below the baseline -> jump straight
            to min_fan_pct, iLO is in charge again

Latency: sensors are read from sysfs (~1 ms) every poll_sec. iLO PATCHes
(0.3-2 s) run on their own thread, so a slow iLO never delays the next
sensor read; only the newest requested value is ever sent.
"""

import collections
import queue
import threading
import time

import config
import ilo_fan
import mclk
import perf

_lock = threading.Lock()
_MAX_LOG_LINES = 200
HISTORY_LEN = 900          # one point per second -> 15 min
PUSH_RETRY_SEC = 5         # back off after a failed PATCH
TREND_WINDOW_SEC = 4       # rise rate is a least-squares slope over this window
RISING_C_PER_S = 0.15      # above this the hold timer is paused
BASELINE_SETTLE_SEC = 20   # wait this long after a handoff before learning the baseline

state = {
    "last_poll_ts": None,      # healthcheck reads this
    "gpus": [],
    "control_c": None,
    "control_card": None,
    "control_source": None,    # "junction" or "HBM 70 + 11"
    "rate_c_per_s": 0.0,
    "predicted_c": None,
    "target_pct": None,
    "floor_pct": None,         # what iLO has accepted (None = unknown)
    "pending_pct": None,       # requested, PATCH in flight
    "baseline_pct": None,      # what iLO runs on its own
    "baseline_source": None,   # "learned" | "set"
    "phase": "starting",       # starting|ilo|boost|hold|ramp|failsafe|paused
    "countdown_sec": 0,        # hold: until first step down; ramp: until next step
    "reason": "",
    "loaded_cards": [],
    "dry_run": False,          # no iLO creds: decisions are shown but not sent
    "latency": None,           # {"detect_to_applied_ms", "patch_ms", "pct", "ts"} of the last raise
    "perf_mode": None,
    "perf_idle_remaining_sec": 0,
    "ilo": {"fans": [], "floor_pct": None, "ambient_c": None, "ts": None, "error": None},
    "last_error": None,
    "recent_log": [],
}
history = collections.deque(maxlen=HISTORY_LEN)   # [ts, control_c, floor_pct, ilo_fan_avg]
_ilo_wake = threading.Event()


def _log(msg: str) -> None:
    ilo_fan.log(msg)
    line = time.strftime("%H:%M:%S ") + msg
    with _lock:
        state["recent_log"].append(line)
        del state["recent_log"][:-_MAX_LOG_LINES]


def _set(**kw) -> None:
    with _lock:
        state.update(kw)


def get_state_snapshot() -> dict:
    with _lock:
        snap = dict(state)
        snap["recent_log"] = list(state["recent_log"])
        snap["ilo"] = dict(state["ilo"])
    snap["mclk"] = mclk.snapshot()
    return snap


def get_history(since: float = 0) -> list:
    with _lock:
        return [h for h in history if h[0] > since]


# ---------------------------------------------------------------------------
# Pure decision logic (no I/O, unit-tested in tests/test_controller.py)
# ---------------------------------------------------------------------------

def control_temp(gpus: list, mem_offset):
    """(temp, card, source) the curve runs on: the hottest of junction (or
    edge) and HBM + mem_offset across all GPUs. (None, None, None) if nothing
    is readable."""
    best = (None, None, None)
    for g in gpus:
        cands = [(g.get("fan_curve_c"), g.get("fan_curve_kind") or "junction")]
        if g.get("memory_c") is not None and mem_offset is not None:
            cands.append((g["memory_c"] + mem_offset, f"HBM {g['memory_c']:.0f} + {mem_offset:g}"))
        for v, src in cands:
            if v is not None and (best[0] is None or v > best[0]):
                best = (v, g.get("card"), src)
    return best


def loaded_cards(gpus: list, s: dict) -> list:
    return [g["card"] for g in gpus
            if (g.get("power_w") is not None and g["power_w"] >= s["load_power_w"])
            or (g.get("busy_pct") is not None and g["busy_pct"] >= s["load_busy_pct"])]


def compute_target(temp_c, loaded: list, s: dict, rate: float = 0.0):
    """(target %, reason). temp_c is the (predicted) control temperature."""
    curve = ilo_fan.curve_pct_for_temp(temp_c, s["thresholds"]) if temp_c is not None else 0
    load = s["load_floor_pct"] if loaded else 0
    raw = max(curve, load)
    q = max(1, s["quantize_pct"])
    target = min(100, -(-raw // q) * q) if raw > 0 else 0   # round up
    target = max(target, s["min_fan_pct"])
    if raw == 0:
        reason = "below curve"
    elif load >= curve:
        reason = f"load on {', '.join(loaded)}"
    else:
        reason = f"curve at {temp_c:.0f}\u00b0C" + (f", rising {rate:.1f}\u00b0C/s" if rate >= RISING_C_PER_S else "")
    return target, reason


class Trend:
    """Least-squares slope of the control temperature over the last few seconds."""

    def __init__(self, window: float = TREND_WINDOW_SEC):
        self.window = window
        self.pts = collections.deque()

    def add(self, ts: float, temp: float) -> float:
        self.pts.append((ts, temp))
        while self.pts and ts - self.pts[0][0] > self.window:
            self.pts.popleft()
        if len(self.pts) < 3 or self.pts[-1][0] - self.pts[0][0] < 0.8:
            return 0.0
        n = len(self.pts)
        mt = sum(p[0] for p in self.pts) / n
        mv = sum(p[1] for p in self.pts) / n
        den = sum((p[0] - mt) ** 2 for p in self.pts)
        return sum((p[0] - mt) * (p[1] - mv) for p in self.pts) / den if den else 0.0


def predict(temp: float, rate: float, s: dict) -> float:
    """Look ahead along a rising trend (never below the measured temp, capped at +15 C)."""
    if rate <= 0 or not s["predict_sec"]:
        return temp
    return temp + min(15.0, rate * s["predict_sec"])


class FloorController:
    """Decides the fan minimum. `floor` is what iLO has actually accepted
    (only changed through commit()). Pure: no I/O, no threads."""

    def __init__(self):
        self.floor = None
        self.needed_ts = None      # last time the floor was still needed (hold timer start)
        self.last_step_ts = None
        self.rise_ts = None        # when the current raise was first detected (for latency)

    def commit(self, pct: int) -> None:
        self.floor = pct
        if self.rise_ts is not None and pct is not None:
            self.rise_ts = None

    def adopt(self, pct: int, now: float) -> None:
        """Take over a floor found on iLO at start-up: hold it, then ramp."""
        self.floor = pct
        self.needed_ts = now
        self.last_step_ts = None

    def decide(self, target: int, now: float, s: dict, rising: bool = False, baseline=None):
        """Returns (desired floor, phase, countdown_sec)."""
        lo = s["min_fan_pct"]
        noop = max(lo, baseline or 0)        # a floor at or below this changes nothing

        # Nothing to add on top of what iLO already does.
        if target <= noop and (self.floor is None or self.floor <= noop):
            self.needed_ts = self.last_step_ts = self.rise_ts = None
            return lo, "ilo", 0

        # Takeover / boost: straight up.
        if self.floor is None or target >= self.floor:
            if self.floor is None or target > self.floor:
                self.rise_ts = self.rise_ts or now
            self.needed_ts = now
            self.last_step_ts = None
            return target, "boost", 0

        # target < floor: hold first. Never start coming down while still heating up.
        if rising or self.needed_ts is None:
            self.needed_ts = now
        held = now - self.needed_ts
        if held < s["hold_sec"]:
            return self.floor, "hold", round(s["hold_sec"] - held)

        # Ramp down; hand straight back to iLO once the next step stops mattering.
        if self.last_step_ts is None or now - self.last_step_ts >= s["step_down_sec"]:
            self.last_step_ts = now
            nxt = max(target, self.floor - s["step_down_pct"])
            if nxt <= noop:
                return lo, "ramp", 0
            return nxt, "ramp", round(s["step_down_sec"]) if nxt > target else 0
        return self.floor, "ramp", round(s["step_down_sec"] - (now - self.last_step_ts))


class IloWriter:
    """Sends fan-minimum PATCHes on its own thread. Only the newest request
    is sent; results come back through `results` for the loop to commit."""

    def __init__(self, ilo):
        self.ilo = ilo
        self.results = queue.Queue()
        self._want = None
        self._cv = threading.Condition()

    def request(self, pct: int, detect_ts) -> None:
        with self._cv:
            self._want = (pct, detect_ts, time.time())
            self._cv.notify()

    def run(self) -> None:
        while True:
            with self._cv:
                while self._want is None:
                    self._cv.wait()
                pct, detect_ts, req_ts = self._want
                self._want = None
            t0 = time.time()
            ok = True if self.ilo is None else self.ilo.set_fan_floor_pct(pct)
            t1 = time.time()
            self.results.put({"pct": pct, "ok": ok, "detect_ts": detect_ts, "t0": t0, "t1": t1})
            if ok:
                _ilo_wake.set()
            else:
                time.sleep(PUSH_RETRY_SEC)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def _make_ilo():
    try:
        return ilo_fan.IloFanController()
    except (FileNotFoundError, KeyError, ValueError) as e:
        _log(f"No iLO connection ({e}). Running in dry-run: decisions are shown, nothing is sent.")
        return None


def run_forever(ilo) -> None:
    ctl = FloorController()
    trend = Trend()
    perf_ctl = perf.PerfController()
    writer = IloWriter(ilo)
    threading.Thread(target=_supervise, args=(writer.run,), daemon=True, name="ilo-writer").start()
    no_temps = 0
    last_phase = None
    pending = None              # pct requested from the writer, not yet answered
    last_change_ts = time.time()
    learned_baseline = None
    last_hist_ts = 0.0
    _set(dry_run=ilo is None)

    # Adopt whatever floor iLO already has (e.g. left by a restart), then hold/ramp it down normally.
    if ilo is not None:
        try:
            found = ilo.read_status().get("floor_pct")
            if found is not None:
                ctl.adopt(int(found), time.time())
                _log(f"iLO fan minimum is currently {found}%.")
        except Exception as e:
            _log(f"Could not read the current iLO fan minimum ({e}); will set it on the first cycle.")

    while True:
        s = config.load()
        now = time.time()

        # Results from the writer thread.
        while True:
            try:
                r = writer.results.get_nowait()
            except queue.Empty:
                break
            if r["pct"] == pending:
                pending = None
            if not r["ok"]:
                _log(f"Setting fan minimum to {r['pct']}% failed; retrying in {PUSH_RETRY_SEC}s.")
                continue
            old = ctl.floor
            ctl.commit(r["pct"])
            last_change_ts = r["t1"]
            patch_ms = round((r["t1"] - r["t0"]) * 1000)
            if old is None or r["pct"] > old:
                lat = {"pct": r["pct"], "patch_ms": patch_ms, "ts": r["t1"],
                       "detect_to_applied_ms": round((r["t1"] - r["detect_ts"]) * 1000) if r["detect_ts"] else None}
                _set(latency=lat)
                took = f"{lat['detect_to_applied_ms']} ms after detection" if lat["detect_to_applied_ms"] is not None else f"{patch_ms} ms"
                _log(f"{'Takeover' if (old or 0) <= (state['baseline_pct'] or s['min_fan_pct']) else 'Raised'}: "
                     f"fan minimum \u2191 {r['pct']}% ({state['reason']}), set on iLO {took}.")
            elif r["pct"] <= s["min_fan_pct"]:
                _log(f"Handed back to iLO (fan minimum {r['pct']}%).")
            else:
                _log(f"Fan minimum \u2193 {r['pct']}%.")

        if s["paused"]:
            if last_phase != "paused":
                _log("Paused. The fan minimum is left where it is.")
                perf_ctl.release()
            last_phase = "paused"
            _set(phase="paused", countdown_sec=0, last_poll_ts=now)
            time.sleep(s["poll_sec"])
            continue

        gpus = ilo_fan.get_gpu_sensors()
        temp, card, source = control_temp(gpus, s["mem_offset_c"])
        rate, predicted = 0.0, None

        if temp is None:
            no_temps += 1
            err = "No GPU sensors readable (sysfs and rocm-smi returned nothing)."
            if no_temps == 1:
                _log(err)
            if no_temps < s["failsafe_after_polls"]:
                _set(gpus=gpus, last_poll_ts=now, last_error=err)
                time.sleep(s["poll_sec"])
                continue
            target, reason, loaded = s["failsafe_fan_pct"], "sensors lost", []
        else:
            if no_temps >= s["failsafe_after_polls"]:
                _log("GPU sensors back.")
            no_temps = 0
            try:
                perf_state = perf_ctl.update(gpus, s, now, _log)
            except Exception as e:
                _log(f"Perf control error: {e} -> back to high.")
                try:
                    perf_ctl.release()
                except Exception:
                    pass
                perf_state = {"perf_mode": "error (high)", "perf_idle_remaining_sec": 0}
            _set(**perf_state)
            rate = trend.add(now, temp)
            predicted = predict(temp, rate, s)
            loaded = loaded_cards(gpus, s) if s["load_floor_pct"] > 0 else []
            target, reason = compute_target(predicted, loaded, s, rate)

        # Baseline: what iLO does on its own. Learned while it is in charge and settled.
        with _lock:
            ilo_st = dict(state["ilo"])
        fan_pcts = [f["pct"] for f in ilo_st.get("fans", []) if f.get("pct") is not None]
        # Any reading clearly above our minimum is iLO's own choice, so it counts too.
        if (pending is None and fan_pcts and ilo_st.get("ts")
                and ilo_st["ts"] - last_change_ts > BASELINE_SETTLE_SEC
                and ((ctl.floor or 0) <= s["min_fan_pct"] or min(fan_pcts) > (ctl.floor or 0) + 2)):
            learned_baseline = min(fan_pcts)
        if s["ilo_baseline_pct"]:
            baseline, b_src = s["ilo_baseline_pct"], "set"
        else:
            baseline, b_src = learned_baseline, ("learned" if learned_baseline is not None else None)

        desired, phase, countdown = ctl.decide(target, now, s, rising=rate >= RISING_C_PER_S, baseline=baseline)
        if no_temps >= s["failsafe_after_polls"]:
            phase = "failsafe"

        if desired != ctl.floor and desired != pending:
            pending = desired
            writer.request(desired, ctl.rise_ts if ctl.floor is None or desired > ctl.floor else None)

        if phase != last_phase and phase == "hold" and last_phase not in (None, "starting"):
            _log(f"Need dropped to {target}%: holding {ctl.floor}% for {s['hold_sec']:.0f}s"
                 " (paused while temps are still rising), then stepping down.")
        last_phase = phase

        with _lock:
            if now - last_hist_ts >= 1:
                last_hist_ts = now
                history.append([round(now, 1),
                                round(temp, 1) if temp is not None else None,
                                ctl.floor,
                                round(sum(fan_pcts) / len(fan_pcts)) if fan_pcts else None])
            state.update(
                gpus=gpus, control_c=temp, control_card=card, control_source=source,
                rate_c_per_s=round(rate, 2), predicted_c=round(predicted, 1) if predicted is not None else None,
                target_pct=target, floor_pct=ctl.floor, pending_pct=pending,
                baseline_pct=baseline, baseline_source=b_src,
                phase=phase, countdown_sec=countdown, reason=reason,
                loaded_cards=loaded, last_poll_ts=now,
                last_error=None if temp is not None else state["last_error"],
            )
        time.sleep(s["poll_sec"])


def _ilo_poller(ilo) -> None:
    """Reads actual fan speeds from iLO. Woken early right after a PATCH."""
    while True:
        try:
            st = ilo.read_status()
            with _lock:
                state["ilo"] = {**st, "ts": time.time(), "error": None}
        except Exception as e:
            with _lock:
                state["ilo"] = {**state["ilo"], "error": str(e)[:200]}
        _ilo_wake.wait(config.load()["ilo_poll_sec"])
        if _ilo_wake.is_set():
            _ilo_wake.clear()
            time.sleep(1.5)  # read once soon after a change (fans are still spooling), then normal pace


def _supervise(target, *args) -> None:
    # A crash must never leave the fans unmanaged: log it and restart.
    name = getattr(target, "__qualname__", repr(target))
    while True:
        try:
            target(*args)
        except Exception as e:
            _log(f"{name} crashed: {e!r}; restarting in 2 s.")
            _set(last_error=f"{name} crashed: {e!r}")
            time.sleep(2)


def start_background_thread() -> threading.Thread:
    ilo = _make_ilo()
    t = threading.Thread(target=_supervise, args=(run_forever, ilo), daemon=True, name="fan-loop")
    t.start()
    if ilo is not None:
        threading.Thread(target=_supervise, args=(_ilo_poller, ilo), daemon=True, name="ilo-poller").start()
    mclk.start_thread(_log)
    return t
