"""
Background control loop (daemon thread started by app.py) plus a slower
thread that reads actual fan speeds back from iLO.

Control model -- one curve, one floor:

  control temp = hottest of (junction, HBM + mem_offset_c) across GPUs
  target       = max(curve(control temp), load_floor if any GPU is loaded)
                 rounded UP to quantize_pct, never below min_fan_pct

  target above the floor  -> raise the floor at once
  target below the floor  -> hold for hold_sec, then drop step_down_pct
                             every step_down_sec until it meets the target
  floor back at min       -> iLO is fully in charge again

The floor is a *minimum*: iLO's own thermal logic still runs and can spin the
fans higher. A floor below what iLO is already doing has no audible effect.
"""

import collections
import threading
import time

import config
import ilo_fan
import mclk
import perf

_lock = threading.Lock()
_MAX_LOG_LINES = 200
HISTORY_LEN = 900          # ~15 min at 1 s polling
PUSH_RETRY_SEC = 5         # back off after a failed PATCH

state = {
    "last_poll_ts": None,      # healthcheck reads this
    "gpus": [],
    "control_c": None,
    "control_card": None,
    "control_source": None,    # "junction" or "HBM 70 + 11"
    "target_pct": None,
    "floor_pct": None,         # what we last successfully set on iLO (None = unknown)
    "phase": "starting",       # starting|ilo|boost|hold|ramp|failsafe|paused
    "countdown_sec": 0,        # hold: until first step down; ramp: until next step
    "reason": "",
    "loaded_cards": [],
    "dry_run": False,          # no iLO creds: decisions are shown but not sent
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


def compute_target(temp_c, loaded: list, s: dict):
    """(target %, reason)."""
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
        reason = f"curve at {temp_c:.0f}°C"
    return target, reason


class FloorController:
    """Fast up, hold, then step down. `floor` is the value iLO actually has
    (only changed through commit() after a successful PATCH)."""

    def __init__(self):
        self.floor = None
        self.needed_ts = None      # last time the target was >= floor
        self.last_step_ts = None

    def commit(self, pct: int, now: float) -> None:
        self.floor = pct

    def adopt(self, pct: int, now: float) -> None:
        """Take over a floor found on iLO at start-up: hold it, then ramp."""
        self.floor = pct
        self.needed_ts = now
        self.last_step_ts = None

    def decide(self, target: int, now: float, s: dict):
        """Returns (desired floor, phase, countdown_sec)."""
        if self.floor is None or target >= self.floor:
            self.needed_ts = now
            self.last_step_ts = None
            phase = "ilo" if target <= s["min_fan_pct"] else "boost"
            return target, phase, 0

        held = now - (now if self.needed_ts is None else self.needed_ts)
        if held < s["hold_sec"]:
            return self.floor, "hold", round(s["hold_sec"] - held)

        if self.last_step_ts is None or now - self.last_step_ts >= s["step_down_sec"]:
            self.last_step_ts = now
            desired = max(target, self.floor - s["step_down_pct"])
            return desired, "ramp", round(s["step_down_sec"]) if desired > target else 0
        return self.floor, "ramp", round(s["step_down_sec"] - (now - self.last_step_ts))


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
    perf_ctl = perf.PerfController()
    no_temps = 0
    last_phase = None
    retry_after = 0.0
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
            loaded = loaded_cards(gpus, s) if s["load_floor_pct"] > 0 else []
            target, reason = compute_target(temp, loaded, s)

        desired, phase, countdown = ctl.decide(target, now, s)
        if no_temps >= s["failsafe_after_polls"]:
            phase = "failsafe"

        if desired != ctl.floor and now >= retry_after:
            old = ctl.floor
            ok = True if ilo is None else ilo.set_fan_floor_pct(desired)
            if ok:
                ctl.commit(desired, now)
                arrow = "↑" if old is None or desired > old else "↓"
                _log(f"Fan minimum {arrow} {desired}%  ({reason}"
                     + (f", {temp:.1f}°C" if temp is not None and "curve" not in reason else "") + ")")
                _ilo_wake.set()
            else:
                retry_after = now + PUSH_RETRY_SEC
                _log(f"Setting fan minimum to {desired}% failed; retrying in {PUSH_RETRY_SEC}s.")

        if phase != last_phase and phase in ("hold", "ilo") and last_phase not in (None, "starting"):
            _log(f"Need dropped to {target}%: holding {ctl.floor}% for {s['hold_sec']:.0f}s, then stepping down."
                 if phase == "hold" else "Back to iLO control.")
        last_phase = phase

        with _lock:
            ilo_fans = [f["pct"] for f in state["ilo"]["fans"] if f.get("pct") is not None]
            history.append([round(now, 1),
                            round(temp, 1) if temp is not None else None,
                            ctl.floor,
                            round(sum(ilo_fans) / len(ilo_fans)) if ilo_fans else None])
            state.update(
                gpus=gpus, control_c=temp, control_card=card, control_source=source, target_pct=target,
                floor_pct=ctl.floor, phase=phase, countdown_sec=countdown, reason=reason,
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
            time.sleep(2)  # give the fans a moment to spin up before reading


def _supervise(target, *args) -> None:
    # A crash must never leave the fans unmanaged: log it and restart.
    while True:
        try:
            target(*args)
        except Exception as e:
            _log(f"{target.__name__} crashed: {e!r}; restarting in 2 s.")
            _set(last_error=f"{target.__name__} crashed: {e!r}")
            time.sleep(2)


def start_background_thread() -> threading.Thread:
    ilo = _make_ilo()
    t = threading.Thread(target=_supervise, args=(run_forever, ilo), daemon=True, name="fan-loop")
    t.start()
    if ilo is not None:
        threading.Thread(target=_supervise, args=(_ilo_poller, ilo), daemon=True, name="ilo-poller").start()
    mclk.start_thread(_log)
    return t
