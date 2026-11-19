"""
Persisted settings for fan-bridge.

Settings live in a JSON file on a mounted volume (default /data/settings.json)
and are edited from the dashboard. The loop calls load() every cycle; the file
is only re-read when its mtime changes.

Every key has a schema entry (type + range). save() validates and clamps, so
neither the UI nor a hand-edited file can push nonsense into the loop.
"""

import copy
import json
import os
import threading

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

CURVE_TEMP_MIN = 30
CURVE_TEMP_MAX = 110
CURVE_POINTS = 6

# Default curve on control temp (junction, or HBM + mem_offset_c).
# Below the first point the script has no opinion and iLO runs the fans.
# A floor below what iLO is already running does nothing, so the low end of
# the curve is cheap insurance; it only bites once it climbs past iLO's own
# speed (usually 15-25% on an ML350 Gen10).
DEFAULT_CURVE = [[60, 15], [70, 25], [78, 35], [84, 50], [90, 70], [96, 100]]

DEFAULTS = {
    "thresholds": DEFAULT_CURVE,

    # Load trigger: power/busy jump the moment work starts, about a second
    # before junction spikes 20+ C. 0 floor = off.
    "load_power_w": 100,
    "load_busy_pct": 50,
    "load_floor_pct": 40,

    # Fast up, slow down. After the floor stops being needed it holds for
    # hold_sec, then drops step_down_pct every step_down_sec until it reaches
    # min_fan_pct (handed back to iLO).
    "hold_sec": 60,
    "step_down_sec": 15,
    "step_down_pct": 5,
    "min_fan_pct": 0,
    # Round the target UP to this many %, so the floor isn't re-PATCHed for
    # every 1% wiggle (iLO PATCHes take ~0.5-2 s each).
    "quantize_pct": 5,

    "poll_sec": 1,
    "ilo_poll_sec": 5,           # how often to read actual fan speeds back from iLO

    # HBM limit is ~94 C vs ~105 C junction, so HBM counts as this much hotter.
    # null = ignore HBM.
    "mem_offset_c": 11,
    "failsafe_fan_pct": 60,
    "failsafe_after_polls": 5,

    # GPU perf level on demand (perf.py). Off = always "high".
    "perf_control": False,
    "perf_idle_sec": 600,
    "perf_busy_pct": 20,
    "perf_power_w": 80,

    "paused": False,
}

# key -> (type, min, max). None bounds = unbounded.
SCHEMA = {
    "load_power_w": (float, 0, 400),
    "load_busy_pct": (float, 0, 100),
    "load_floor_pct": (int, 0, 100),
    "hold_sec": (float, 0, 3600),
    "step_down_sec": (float, 1, 600),
    "step_down_pct": (int, 1, 100),
    "min_fan_pct": (int, 0, 100),
    "quantize_pct": (int, 1, 25),
    "poll_sec": (float, 0.5, 30),
    "ilo_poll_sec": (float, 2, 120),
    "mem_offset_c": (float, 0, 40),     # also accepts None
    "failsafe_fan_pct": (int, 20, 100),
    "failsafe_after_polls": (int, 1, 60),
    "perf_control": (bool, None, None),
    "perf_idle_sec": (float, 0, 86400),
    "perf_busy_pct": (float, 0, 100),
    "perf_power_w": (float, 0, 400),
    "paused": (bool, None, None),
}

# Keys from the old trigger/cooldown/held model. Dropped on load.
_OBSOLETE = {"target_max_c", "max_fan_pct", "hysteresis_c", "cooldown_sec", "release_sec",
             "idle_poll_sec", "active_poll_sec", "target_fan_ids", "thresholds_customized"}

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


def clean_curve(points) -> list:
    """Validate a curve: CURVE_POINTS [temp, pct] pairs, temps strictly
    increasing (>= 1 C apart) inside the window, pct 0-100 and non-decreasing."""
    if not isinstance(points, list) or len(points) != CURVE_POINTS:
        raise ValueError(f"curve must be {CURVE_POINTS} [temp, pct] points")
    out, prev_t, prev_p = [], CURVE_TEMP_MIN - 1, 0
    for pt in points:
        t, p = float(pt[0]), int(round(float(pt[1])))
        t = max(CURVE_TEMP_MIN, min(CURVE_TEMP_MAX, t))
        t = max(t, prev_t + 1)
        p = max(prev_p, min(100, max(0, p)))
        out.append([round(t, 1), p])
        prev_t, prev_p = t, p
    if out[-1][0] > CURVE_TEMP_MAX:
        raise ValueError("curve points too close to the top of the range")
    return out


def _coerce(key, value):
    typ, lo, hi = SCHEMA[key]
    if key == "mem_offset_c" and value in (None, "", "null"):
        return None
    if typ is bool:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "on", "yes")
        return bool(value)
    v = typ(float(value))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _normalize(data: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in data.items():
        if k in _OBSOLETE:
            continue
        if k == "thresholds":
            try:
                out[k] = clean_curve(v)
            except (ValueError, TypeError, IndexError):
                pass  # old 7-point curve or garbage -> default curve
        elif k in SCHEMA:
            try:
                out[k] = _coerce(k, v)
            except (ValueError, TypeError):
                pass
    return out


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH) or ".", exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)  # atomic: the loop never reads a half-written file
    _cache["mtime"] = os.path.getmtime(SETTINGS_PATH)
    _cache["data"] = data


def _load_locked() -> dict:
    try:
        mtime = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        _write(copy.deepcopy(DEFAULTS))
        return _cache["data"]
    if mtime == _cache["mtime"] and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(SETTINGS_PATH) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = _cache["data"] or {}
    data = _normalize(raw)
    if data != raw:
        _write(data)
    else:
        _cache["mtime"], _cache["data"] = mtime, data
    return data


def load() -> dict:
    """Current settings (a copy; safe to mutate)."""
    with _lock:
        return copy.deepcopy(_load_locked())


def save(partial: dict) -> dict:
    """Validate and merge partial into the stored settings. Unknown keys are
    ignored. Raises ValueError for a bad curve."""
    with _lock:
        current = copy.deepcopy(_load_locked())
        for k, v in partial.items():
            if k == "thresholds":
                current[k] = clean_curve(v)
            elif k in SCHEMA:
                current[k] = _coerce(k, v)
        _write(current)
        return copy.deepcopy(current)
