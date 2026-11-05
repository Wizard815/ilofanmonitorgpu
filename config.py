"""
Shared, persisted configuration for mi50-fan-bridge.

Settings live in a JSON file on a mounted volume (default
/data/settings.json) so they survive container restarts and are editable
live from the Flask UI -- no env var / compose editing required for
day-to-day tuning. Only iLO connection basics (host, secrets path, SSL
verify) plus the poll loop's own bind address stay as env vars, since
those are container/network wiring concerns rather than tuning knobs.
"""

import json
import os
import threading

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

DEFAULTS = {
    # target_max_c is its own independent value now -- a vertical marker
    # line on the curve chart, dragged left/right on its own, NOT one of
    # the curve's points. max_fan_pct is still tracked for the "Reset to
    # auto curve" seed.
    "target_max_c": 78,
    "max_fan_pct": 100,

    # Advanced/internal knobs -- not shown in the UI at all; still real
    # and editable via the JSON settings file or /api/state if ever needed.
    "hysteresis_c": 3,           # kept for internal curve smoothing only, not the cooldown timer -- see cooldown_sec
    "cooldown_sec": 60,          # seconds temp must stay below trigger before the script stops actively adjusting
    "release_sec": 120,          # seconds AFTER cooldown ends before the floor actually resets to min_fan_pct
    "idle_poll_sec": 1,          # how often to check temps while UNDER trigger (iLO in control) -- shown in UI; sysfs reads are ~1 ms
    "active_poll_sec": 1,        # how often to check temps once OVER trigger (script in control) -- near-real-time

    # Load trigger: GPU power/busy jump the moment work starts, ~1 s before
    # junction spikes 20+ C, so raise the floor on load, not only on heat.
    "load_power_w": 100,         # any GPU at or above this power counts as "under load" (idle is ~25-50 W)
    "load_busy_pct": 50,         # ... or at or above this busy %
    "load_floor_pct": 40,        # floor pushed immediately while under load (curve still wins if higher)

    # Fast up, slow down: raise at once, lower only after the lower target
    # has held for step_down_sec, and by at most step_down_pct per step.
    "step_down_sec": 20,
    "step_down_pct": 5,

    # GPU perf level on demand (perf.py): "high" while in use, "auto" after
    # perf_idle_sec without activity. Off = always high, the old behaviour.
    "perf_control": False,
    "perf_idle_sec": 600,        # idle time before dropping to auto
    "perf_busy_pct": 20,         # activity = any GPU busy at or above this ...
    "perf_power_w": 80,          # ... or drawing at or above this (idle: ~45 W high, less in auto)

    # Safety: HBM limit is 94 C vs 105 C junction, so fans also react to
    # HBM + mem_offset_c (None turns that off). If sensors vanish for
    # failsafe_after_polls polls in a row, push failsafe_fan_pct.
    "mem_offset_c": 11,
    "failsafe_fan_pct": 60,
    "failsafe_after_polls": 5,
    "min_fan_pct": 0,            # global fan floor to reset to once release_sec elapses (0 = no floor, iLO fully in control) -- shown in UI
    "target_fan_ids": "1",       # unused on this hardware -- firmware only supports one global floor, not per-fan

    "thresholds": [],  # 7 independent [temp, pct] curve points -- auto-derived if empty, see derive_thresholds()
    "thresholds_customized": False,  # once True (user dragged a curve point), the curve is no longer
                                      # auto-reseeded when max_fan_pct changes elsewhere

    "paused": False,  # UI can pause the automated loop without stopping the container
}

_lock = threading.Lock()


# Offsets used only to SEED the curve initially (and on "Reset to auto
# curve") relative to a rough default trigger of 78C. Once seeded, curve
# points are fully independent of target_max_c -- moving the trigger
# marker does NOT move curve points, and moving a curve point does NOT
# move the trigger. They are two separate, unconnected controls on the
# same chart.
#
# Percentages kept low: ML350/DL380 Gen10 chassis fans are large,
# high-static-pressure units -- HPE's own troubleshooting docs note fan
# speeds above 60% are expected to be loud, and idle/normal operation on
# these chassis typically sits in the 15-25% range even under moderate
# load. The old default ramped straight to 65% at trigger and 100% ten
# degrees later, which is far louder/faster than needed for two MI50s.
SEED_OFFSETS = [-10, -5, -2, 0, 5, 10, 15]
DEFAULT_SEED_PCTS = [15, 22, 28, 35, 45, 60, 80]

# Fixed temperature window the curve/trigger are allowed to occupy. Server
# enforces this independently of the client-side clamp in the browser, so
# a stale or manipulated request can't push the curve to absurd temps.
CURVE_TEMP_MIN = 40
CURVE_TEMP_MAX = 110


def _clamp_temp(t: float) -> float:
    return max(CURVE_TEMP_MIN, min(CURVE_TEMP_MAX, t))


def derive_thresholds(seed_center_c: float, max_fan_pct: int) -> list:
    """
    Builds the initial 7-point fan curve, seeded around seed_center_c
    (typically the current target_max_c at the time of seeding/reset,
    but this is a ONE-TIME seed, not a live link -- after this, curve
    points are independent of target_max_c). Last point's % follows
    max_fan_pct; the rest are a fixed ramp shape.
    """
    t = float(seed_center_c)
    pcts = DEFAULT_SEED_PCTS[:-1] + [max_fan_pct]
    return [[round(_clamp_temp(t + off), 1), pct] for off, pct in zip(SEED_OFFSETS, pcts)]


def _ensure_file() -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    if not os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "w") as f:
            json.dump(DEFAULTS, f, indent=2)


def load() -> dict:
    """Loads current settings, filling in any keys missing (e.g. after an
    upgrade adds a new setting) with defaults, without clobbering the rest.
    Seeds thresholds from target_max_c/max_fan_pct if the stored list is
    empty (fresh install, or user cleared it manually) -- a ONE-TIME seed,
    not a live link; the curve is independent of target_max_c afterward."""
    with _lock:
        _ensure_file()
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
        merged = {**DEFAULTS, **data}
        if not merged.get("thresholds"):
            merged["thresholds"] = derive_thresholds(merged["target_max_c"], merged["max_fan_pct"])
        if merged != data:
            with open(SETTINGS_PATH, "w") as f:
                json.dump(merged, f, indent=2)
        return merged


def save(new_settings: dict) -> dict:
    """Merges new_settings on top of current settings and persists as-is
    -- no cross-field logic. target_max_c, thresholds, and max_fan_pct
    are fully independent now; changing one never touches the others.
    Use save_curve_drag() / save_trigger_drag() for the UI's drag flows,
    which set thresholds_customized appropriately. Returns the full
    merged settings dict."""
    with _lock:
        _ensure_file()
        with open(SETTINGS_PATH, "r") as f:
            current = json.load(f)
        current.update(new_settings)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(current, f, indent=2)
        return current


def target_fan_id_set(settings: dict) -> set:
    return {f.strip() for f in settings.get("target_fan_ids", "1").split(",") if f.strip()}


def set_customized_flag(value: bool) -> dict:
    """Explicitly sets thresholds_customized without touching anything else.
    Used by the 'Reset to auto curve' action, which stores a freshly
    derived curve but should NOT be treated as a manual customization."""
    return save({"thresholds_customized": value})


def _enforce_temp_order(thresholds: list, min_gap: float = 1.0) -> list:
    """
    Ensures each point's temp is at least min_gap above the previous
    point's temp, after clamping every temp to [CURVE_TEMP_MIN,
    CURVE_TEMP_MAX]. This is the server-side backstop for the same rule
    the client enforces live during a drag (a point can't cross a
    neighbor) -- kept here too since the server never fully trusts client
    math. Percentages pass through untouched.
    """
    out = []
    prev_t = CURVE_TEMP_MIN - min_gap
    for t, p in thresholds:
        t = _clamp_temp(float(t))
        if t <= prev_t:
            t = min(CURVE_TEMP_MAX, prev_t + min_gap)
        out.append([round(t, 1), int(p)])
        prev_t = t
    return out


def save_curve_drag(client_thresholds: list) -> dict:
    """
    Called after dragging any of the 7 fan-curve points. These points
    have NOTHING to do with the trigger -- dragging a curve point never
    touches target_max_c, and dragging the trigger never touches these
    points. Stores the dragged [temp, pct] list after clamping/reordering
    server-side as a backstop.
    """
    with _lock:
        _ensure_file()
        with open(SETTINGS_PATH, "r") as f:
            current = json.load(f)
        current["thresholds"] = _enforce_temp_order(client_thresholds)
        current["thresholds_customized"] = True
        with open(SETTINGS_PATH, "w") as f:
            json.dump(current, f, indent=2)
        return current


def save_trigger_drag(new_trigger_c: float) -> dict:
    """
    Called after dragging the trigger marker (a standalone vertical line
    on the chart, not a curve point). Only target_max_c changes -- the
    curve's 7 points are completely untouched.
    """
    with _lock:
        _ensure_file()
        with open(SETTINGS_PATH, "r") as f:
            current = json.load(f)
        current["target_max_c"] = _clamp_temp(float(new_trigger_c))
        with open(SETTINGS_PATH, "w") as f:
            json.dump(current, f, indent=2)
        return current
