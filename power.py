"""
Power cap and top core clock for each MI50, on request (Argos's Profiles ->
Power, or anything that POSTs /api/power).

fan-bridge already has each GPU's sysfs device dir mounted writable at
/gpu/<name> (see docker-compose.yml), so it can write what a container like
Argos cannot:

  power cap  hwmon/hwmon*/power1_cap (microwatts), within power1_cap_min/_max
  top clock  power_dpm_force_performance_level = manual, then pp_dpm_sclk
             "0 1 .. k" (every level up to the MHz asked for)

Safety:
- Only dirs whose PCI id is an MI50/MI60 are touched (perf._is_vega20).
- A raise (a higher cap, or a clock limit taken off) is refused while any
  GPU's control temperature is at or above POWER_GUARD_C (95 C), and while
  the fan loop is in failsafe (sensors lost). A cut is always allowed: it
  only makes the card cooler.
- While a card has a clock limit, perf-on-demand leaves that card alone
  (it would write high/auto over "manual"); lifting the limit hands it back.
- FAN_BRIDGE_POWER_TOKEN, when set, must come in the X-Power-Token header.
- Nothing is persistent: a reboot or driver reload brings back the BIOS
  values.
"""

import glob
import os
import threading

import ilo_fan
import perf

GPU_GLOB = os.environ.get("POWER_GPU_GLOB", "/gpu/*")
GUARD_C = float(os.environ.get("POWER_GUARD_C", "95"))
TOKEN = os.environ.get("FAN_BRIDGE_POWER_TOKEN", "")

_lock = threading.Lock()
limits = {}   # pci -> the top MHz allowed, while a clock limit is on


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(path, scale=1.0):
    v = _read(path)
    try:
        return round(float(v) / scale, 1) if v not in (None, "") else None
    except ValueError:
        return None


def _write(path, value):
    with open(path, "w") as f:
        f.write(value)


def parse_dpm(text):
    """pp_dpm_sclk -> [(level, MHz, active)]."""
    out = []
    for line in (text or "").splitlines():
        k, _, rest = line.partition(":")
        if not k.strip().isdigit():
            continue
        mhz = "".join(ch for ch in rest.lower().split("mhz")[0] if ch.isdigit())
        if mhz:
            out.append((int(k), int(mhz), "*" in rest))
    return out


def _pci(dev):
    for line in (_read(os.path.join(dev, "uevent")) or "").splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            return line.split("=", 1)[1]
    return os.path.basename(dev)


def read_card(dev):
    hw = sorted(glob.glob(os.path.join(dev, "hwmon", "hwmon*")))
    if not hw:
        return None
    hw = hw[0]
    dpm = parse_dpm(_read(os.path.join(dev, "pp_dpm_sclk")))
    pci = _pci(dev)
    perf_level = _read(os.path.join(dev, "power_dpm_force_performance_level"))
    return {
        "card": os.path.basename(dev), "pci": pci,
        "cap_w": _num(os.path.join(hw, "power1_cap"), 1e6),
        "cap_min_w": _num(os.path.join(hw, "power1_cap_min"), 1e6),
        "cap_max_w": _num(os.path.join(hw, "power1_cap_max"), 1e6),
        "cap_default_w": _num(os.path.join(hw, "power1_cap_default"), 1e6),
        "power_w": _num(os.path.join(hw, "power1_average"), 1e6) or _num(os.path.join(hw, "power1_input"), 1e6),
        "sclk_levels_mhz": [m for _, m, _ in dpm],
        "sclk_mhz": next((m for _, m, a in dpm if a), None),
        "perf_level": perf_level,
        "sclk_limited": pci in limits or perf_level == "manual",
        "sclk_limit_mhz": limits.get(pci),
        "_dev": dev, "_hw": hw,
    }


def cards():
    """Every mounted MI50/MI60, with its cap, range and clock levels."""
    out = []
    for dev in sorted(glob.glob(GPU_GLOB)):
        if os.path.isdir(dev) and perf._is_vega20(dev):
            c = read_card(dev)
            if c:
                out.append(c)
    return out


def held_cards():
    """PCI ids perf-on-demand must leave alone (a clock limit is on)."""
    with _lock:
        return set(limits)


def public(c):
    return {k: v for k, v in c.items() if not k.startswith("_")}


def _is_raise(c, want):
    cap = want.get("cap_w") or c.get("cap_default_w")
    if cap is not None and c.get("cap_w") is not None and cap > c["cap_w"] + 0.5:
        return True
    if want.get("sclk_mhz") is None:
        return bool(c.get("sclk_limited"))
    return c.get("sclk_limit_mhz") is not None and want["sclk_mhz"] > c["sclk_limit_mhz"]


def _set_one(c, want):
    cap = want.get("cap_w")
    cap = float(cap) if cap not in (None, "") else c.get("cap_default_w")
    if cap is not None:
        lo, hi = c.get("cap_min_w"), c.get("cap_max_w")
        if (lo is not None and cap < lo) or (hi is not None and cap > hi):
            raise ValueError(f"{cap:.0f} W is outside what the card allows ({lo:.0f}-{hi:.0f} W)")
        _write(os.path.join(c["_hw"], "power1_cap"), str(int(cap * 1_000_000)))
    mhz = want.get("sclk_mhz")
    dev = c["_dev"]
    if mhz in (None, ""):
        with _lock:
            was = limits.pop(c["pci"], None)
        if was is not None or c.get("perf_level") == "manual":
            _write(os.path.join(dev, "power_dpm_force_performance_level"), "auto")
            perf.forget()  # perf-on-demand writes its own level again on its next cycle
        return
    mhz = int(mhz)
    levels = c.get("sclk_levels_mhz") or []
    keep = [i for i, m in enumerate(levels) if m <= mhz] or [0]
    _write(os.path.join(dev, "power_dpm_force_performance_level"), "manual")
    _write(os.path.join(dev, "pp_dpm_sclk"), " ".join(str(i) for i in keep))
    with _lock:
        limits[c["pci"]] = levels[keep[-1]] if levels else mhz


def apply(want, control_c=None, failsafe=False, log=ilo_fan.log):
    """want: {pci: {"cap_w": W or None (the card's default), "sclk_mhz": MHz or None (no limit)}}.
    Returns (applied {pci: want}, errors [str])."""
    by = {c["pci"]: c for c in cards()}
    hot = control_c is not None and control_c >= GUARD_C
    applied, errors = {}, []
    for pci, w in (want or {}).items():
        c = by.get(pci)
        if c is None:
            errors.append(f"{pci}: not a mounted MI50/MI60 (see docker-compose.yml)")
            continue
        if not isinstance(w, dict):
            errors.append(f"{pci}: expected {{cap_w, sclk_mhz}}")
            continue
        if (hot or failsafe) and _is_raise(c, w):
            why = "sensors lost (failsafe)" if failsafe else f"{control_c:.0f} C, guard {GUARD_C:.0f} C"
            errors.append(f"{c['card']}: raise refused, {why}")
            continue
        try:
            _set_one(c, w)
            applied[pci] = w
            log(f"Power {c['card']}: cap {w.get('cap_w') or 'stock'}"
                + (f", sclk <= {w['sclk_mhz']} MHz" if w.get("sclk_mhz") else ""))
        except (OSError, ValueError, TypeError) as e:
            errors.append(f"{c['card']}: {e}")
    return applied, errors
