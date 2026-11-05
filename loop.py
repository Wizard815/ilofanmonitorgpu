"""
Background polling loop. Runs in a daemon thread started by app.py so the
Flask process and the control loop share one container/process, with
`state` as the hand-off point between them.

Control model (four states):
  - IDLE: below trigger, floor already at min_fan_pct, nothing to do.
  - ACTIVE: hottest >= trigger. Script pushes the global fan minimum up
    along the curve derived from (trigger temp, max_fan_pct).
  - COOLDOWN: hottest dropped below trigger. Script keeps holding the
    last-pushed floor for `cooldown_sec` -- a real timer, not a
    temperature buffer. If hottest climbs back above trigger before this
    elapses, the timer cancels and the script returns to ACTIVE.
  - HELD: cooldown finished, but the floor is NOT yet reset -- it holds
    at whatever it last was for a further `release_sec`, in case temp
    climbs again shortly after. If it does, back to ACTIVE. Only once
    release_sec elapses does the script actually PATCH the floor back
    down to `min_fan_pct` (0 by default) -- otherwise the floor would
    stay wherever it was pushed indefinitely, since iLO never lowers a
    minimum on its own.
"""

import threading
import time

import config
import ilo_fan
import mclk
import perf

# Shared, thread-safe-ish state for the UI to read. Simple dict + lock is
# plenty here -- one writer (this loop), occasional readers (Flask requests).
state = {
    "current_floor_pct": None,     # None means "no active floor -- fully idle"
    "phase": "idle",               # "idle" | "active" | "cooldown" | "held"
    "script_in_control": False,    # true for active/cooldown/held -- only false when idle
    "cooldown_remaining_sec": 0,   # seconds left in the COOLDOWN phase
    "release_remaining_sec": 0,    # seconds left in the HELD phase
    "last_temps": [],              # [{"card": ..., "temp_c": <hottest sensor, for display>, "fan_curve_c": <junction, for control>, ...}]
    "last_poll_ts": None,
    "last_error": None,
    "recent_log": [],              # last N log lines, newest last
    "perf_mode": None,             # "high" | "auto" | "off (always high)" | "unmanaged"
    "perf_idle_remaining_sec": 0,  # seconds of idle left before dropping to auto
}
_state_lock = threading.Lock()
_MAX_LOG_LINES = 100
STATUS_LOG_SEC = 30


def _record_log(msg: str) -> None:
    ilo_fan.log(msg)
    with _state_lock:
        state["recent_log"].append(msg)
        if len(state["recent_log"]) > _MAX_LOG_LINES:
            state["recent_log"] = state["recent_log"][-_MAX_LOG_LINES:]


def get_state_snapshot() -> dict:
    with _state_lock:
        snap = {**state, "recent_log": list(state["recent_log"])}
    snap["mclk"] = mclk.snapshot()
    return snap


def run_forever() -> None:
    settings = config.load()
    _record_log(f"Loop starting. Settings loaded from {config.SETTINGS_PATH}")

    ilo = None
    try:
        ilo = ilo_fan.IloFanController()
    except (FileNotFoundError, KeyError) as e:
        _record_log(f"FATAL: could not load iLO creds: {e}. "
                     f"UI will still run but fan control is disabled until this is fixed.")

    # Start in HELD: iLO may still hold a floor from before a restart/crash, so a
    # cool start releases it after release_sec; a hot start goes ACTIVE at once.
    phase = "held"               # "idle" | "active" | "cooldown" | "held"
    current_floor = None         # last floor % we pushed, while active/cooldown/held (None = unknown)
    below_trigger_since = None   # timestamp when hottest first dropped below trigger (cooldown start)
    cooldown_done_since = time.time()  # timestamp when cooldown finished (held phase start)
    step_down_since = None       # timestamp when the target first fell below the current floor
    last_status_log = 0.0        # 1 s polling: only log unchanged status every STATUS_LOG_SEC
    perf_ctl = perf.PerfController()
    no_temps_count = 0           # consecutive polls with no sensor data

    while True:
        settings = config.load()  # re-read every cycle so UI edits apply live
        trigger_c = settings["target_max_c"]
        cooldown_sec = settings["cooldown_sec"]
        release_sec = settings.get("release_sec", 120)
        min_fan_pct = settings.get("min_fan_pct", 0)
        # Fast polling any time the script is actively holding a floor
        # (active/cooldown/held), since temp could climb again any
        # moment; slow polling only once fully idle.
        interval = settings["idle_poll_sec"] if phase == "idle" else settings["active_poll_sec"]

        if settings.get("paused"):
            _record_log("Paused via UI, skipping this cycle.")
            perf_ctl.release()  # paused: back to always-high
            time.sleep(interval)
            continue

        temps = ilo_fan.get_gpu_sensors()
        now = time.time()

        with _state_lock:
            state["last_temps"] = temps
            state["last_poll_ts"] = now

        if not temps:
            no_temps_count += 1
            _record_log(f"No GPU temps read this cycle ({no_temps_count}x in a row).")
            with _state_lock:
                state["last_error"] = "No GPU temps read (sysfs and rocm-smi returned nothing)"
            # Blind (e.g. after a GPU reset): go to the failsafe floor, never sit at a low one.
            failsafe = settings.get("failsafe_fan_pct", 60)
            if no_temps_count >= settings.get("failsafe_after_polls", 5) and ilo is not None \
                    and (current_floor is None or current_floor < failsafe):
                if ilo.set_fan_floor_pct(failsafe):
                    _record_log(f"Sensors lost -> failsafe fan floor {failsafe}%.")
                    current_floor = failsafe
                    phase = "active"
            time.sleep(interval)
            continue
        no_temps_count = 0

        with _state_lock:
            state["last_error"] = None

        # Perf level on demand. Never let it break the fan loop.
        try:
            perf_state = perf_ctl.update(temps, settings, now, _record_log)
        except Exception as e:
            _record_log(f"Perf control error: {e} -> back to high.")
            try:
                perf_ctl.release()
            except Exception:
                pass
            perf_state = {"perf_mode": "error (high)", "perf_idle_remaining_sec": 0}
        with _state_lock:
            state.update(perf_state)

        # Fan-curve decision uses each card's junction (fan_curve_c), NOT
        # the display-only "hottest spot across all sensors" temp_c --
        # junction is the throttle-relevant figure. Falls back to edge
        # per-card if junction wasn't reported for that card.
        # Control temp per card: junction, or HBM + mem_offset_c if that is
        # closer to its limit (HBM crit is 94 C vs 105 C junction).
        mem_offset = settings.get("mem_offset_c", 11)
        def _control_c(t):
            vals = [v for v in (t.get("fan_curve_c"),) if v is not None]
            if t.get("memory_c") is not None and mem_offset is not None:
                vals.append(t["memory_c"] + mem_offset)
            return max(vals) if vals else None
        ctl = [c for c in (_control_c(t) for t in temps) if c is not None]
        if not ctl:
            time.sleep(interval)
            continue
        hottest = max(ctl)

        # Load trigger: power/busy rise ~1 s before junction does.
        load_floor = settings.get("load_floor_pct", 0)
        loaded = [t for t in temps
                  if (t.get("power_w") is not None and t["power_w"] >= settings.get("load_power_w", 1e9))
                  or (t.get("busy_pct") is not None and t["busy_pct"] >= settings.get("load_busy_pct", 1e9))]
        under_load = bool(loaded) and load_floor > 0

        if hottest >= trigger_c or under_load:
            # Over trigger or under load: (re-)enter ACTIVE regardless of
            # what phase we were in -- cancels any cooldown/held timer.
            below_trigger_since = None
            cooldown_done_since = None
            target = ilo_fan.curve_pct_for_temp(hottest, settings)
            if under_load:
                target = max(target, load_floor)
            why = f"Hottest={hottest:.1f}C" + (f", load on {','.join(t['card'] for t in loaded)}" if under_load else "")

            if phase != "active":
                _record_log(f"{why} -> script taking over global fan floor.")
                step_down_since = None

            # Fast up, slow down: raise at once; lower only after the lower
            # target held for step_down_sec, by at most step_down_pct.
            new_floor = current_floor
            if current_floor is None or target > current_floor:
                new_floor = target
                step_down_since = None
            elif target < current_floor:
                if step_down_since is None:
                    step_down_since = now
                elif now - step_down_since >= settings.get("step_down_sec", 0):
                    new_floor = max(target, current_floor - settings.get("step_down_pct", 100))
                    step_down_since = now
            else:
                step_down_since = None

            if new_floor != current_floor:
                _record_log(f"{why} -> pushing fan floor to {new_floor}%")
                if ilo is None:
                    _record_log("Cannot push to iLO: controller not initialized (check creds).")
                else:
                    if ilo.set_fan_floor_pct(new_floor):
                        current_floor = new_floor
                        phase = "active"
                    else:
                        _record_log("Fan floor push FAILED, will retry next cycle.")
            else:
                phase = "active"
                if now - last_status_log >= STATUS_LOG_SEC:
                    _record_log(f"{why}, holding fan floor at {current_floor}%")
                    last_status_log = now

            with _state_lock:
                state["current_floor_pct"] = current_floor
                state["phase"] = "active"
                state["script_in_control"] = True
                state["cooldown_remaining_sec"] = 0
                state["release_remaining_sec"] = 0

        else:
            # Under trigger.
            if phase == "idle":
                if now - last_status_log >= STATUS_LOG_SEC:
                    _record_log(f"Hottest={hottest:.1f}C, below trigger, iLO in control.")
                    last_status_log = now
                with _state_lock:
                    state["current_floor_pct"] = None
                    state["phase"] = "idle"
                    state["script_in_control"] = False
                    state["cooldown_remaining_sec"] = 0
                    state["release_remaining_sec"] = 0

            elif phase == "active":
                # Just dropped below trigger -- start the cooldown timer.
                phase = "cooldown"
                below_trigger_since = now
                _record_log(f"Hottest={hottest:.1f}C dropped below trigger {trigger_c}C -- "
                            f"starting {cooldown_sec}s cooldown, holding floor at {current_floor}%.")
                with _state_lock:
                    state["current_floor_pct"] = current_floor
                    state["phase"] = "cooldown"
                    state["script_in_control"] = True
                    state["cooldown_remaining_sec"] = cooldown_sec
                    state["release_remaining_sec"] = 0

            elif phase == "cooldown":
                elapsed = now - below_trigger_since
                remaining = max(0, cooldown_sec - elapsed)
                with _state_lock:
                    state["cooldown_remaining_sec"] = round(remaining)

                if remaining <= 0:
                    phase = "held"
                    cooldown_done_since = now
                    _record_log(f"Cooldown complete -- holding floor at {current_floor}% for a further "
                                f"{release_sec}s before resetting to {min_fan_pct}%.")
                    with _state_lock:
                        state["phase"] = "held"
                        state["release_remaining_sec"] = release_sec
                else:
                    if now - last_status_log >= STATUS_LOG_SEC:
                        _record_log(f"Hottest={hottest:.1f}C, cooling down ({round(remaining)}s left, "
                                    f"holding fan floor at {current_floor if current_floor is not None else 'unknown '}%)")
                        last_status_log = now
                    with _state_lock:
                        state["current_floor_pct"] = current_floor
                        state["script_in_control"] = True

            elif phase == "held":
                elapsed = now - cooldown_done_since
                remaining = max(0, release_sec - elapsed)
                with _state_lock:
                    state["release_remaining_sec"] = round(remaining)

                if remaining <= 0:
                    _record_log(f"Release timer complete -- resetting fan floor to {min_fan_pct}% "
                                f"and handing control back to iLO.")
                    if ilo is not None:
                        if not ilo.set_fan_floor_pct(min_fan_pct):
                            _record_log("Release floor reset FAILED -- iLO may still be holding "
                                        "the last active floor. Will not retry automatically; "
                                        "check connectivity/creds.")
                    phase = "idle"
                    current_floor = None
                    below_trigger_since = None
                    cooldown_done_since = None
                    with _state_lock:
                        state["current_floor_pct"] = None
                        state["phase"] = "idle"
                        state["script_in_control"] = False
                        state["cooldown_remaining_sec"] = 0
                        state["release_remaining_sec"] = 0
                else:
                    if now - last_status_log >= STATUS_LOG_SEC:
                        _record_log(f"Hottest={hottest:.1f}C, held ({round(remaining)}s to release, "
                                    f"holding fan floor at {current_floor if current_floor is not None else 'unknown '}%)")
                        last_status_log = now
                    with _state_lock:
                        state["current_floor_pct"] = current_floor
                        state["script_in_control"] = True

        time.sleep(interval)


def _supervise() -> None:
    # A crash must never leave the fans unmanaged: log it and restart the loop.
    while True:
        try:
            run_forever()
        except Exception as e:
            _record_log(f"Loop crashed: {e!r} -- restarting in 2 s.")
            with _state_lock:
                state["last_error"] = f"Loop crashed: {e!r}"
            time.sleep(2)


def start_background_thread() -> threading.Thread:
    t = threading.Thread(target=_supervise, daemon=True, name="fan-poll-loop")
    t.start()
    mclk.start_thread(_record_log)  # own thread: the fan loop never waits on it
    return t
