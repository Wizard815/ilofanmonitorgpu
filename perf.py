"""
On-demand GPU perf level: "high" (clocks locked at the top DPM state, ~10%
faster decode) while the GPUs are in use, "auto" (DPM, ~half the idle power,
cooler heatsink) once they have been idle for perf_idle_sec.

Writes go to bind-mounted sysfs device dirs, one per GPU, found at
/gpu/<pci>/power_dpm_force_performance_level (see docker-compose.yml). Sysfs
under /sys stays read-only in the container.

Fail-safe: any error, a disabled setting, or process exit puts every card
back to "high" -- the old always-on behaviour -- so a broken controller never
leaves the cards slow.
"""

import atexit
import glob
import os

import ilo_fan

PERF_GLOB = os.environ.get("PERF_SYSFS_GLOB", "/gpu/*/power_dpm_force_performance_level")


VEGA20_IDS = {"0x66a0", "0x66a1", "0x66a2", "0x66a3", "0x66a7", "0x66af"}  # MI50/MI60 family


def _is_vega20(dev_dir: str) -> bool:
    try:
        with open(os.path.join(dev_dir, "vendor")) as f:
            vendor = f.read().strip()
        with open(os.path.join(dev_dir, "device")) as f:
            device = f.read().strip()
    except OSError:
        return False
    return vendor == "0x1002" and device in VEGA20_IDS


def _perf_files() -> dict:
    """{name: path} for every mounted dir that really is an MI50/MI60 (card numbers can move)."""
    return {os.path.basename(os.path.dirname(p)): p for p in sorted(glob.glob(PERF_GLOB))
            if _is_vega20(os.path.dirname(p))}


def read_levels() -> dict:
    out = {}
    for pci, path in _perf_files().items():
        try:
            with open(path) as f:
                out[pci] = f.read().strip()
        except OSError as e:
            out[pci] = f"error: {e}"
    return out


_forgot = False


def forget() -> None:
    """Someone else (power.py lifting a clock limit) wrote the level: write ours again on the next cycle."""
    global _forgot
    _forgot = True


def _held() -> set:
    """Dirs power.py holds at "manual" for a clock limit: left alone until it lifts it."""
    import power  # imported here: power imports perf
    held = power.held_cards()
    return {name for name, path in _perf_files().items() if power._pci(os.path.dirname(path)) in held} if held else set()


def set_level(level: str) -> bool:
    ok = True
    held = _held()
    for pci, path in _perf_files().items():
        if pci in held:
            continue
        try:
            with open(path) as f:
                if f.read().strip() == level:
                    continue
            with open(path, "w") as f:
                f.write(level)
        except OSError as e:
            ilo_fan.log(f"ERROR setting perf level {level} on {pci}: {e}")
            ok = False
    return ok


class PerfController:
    def __init__(self):
        self.mode = None          # "high" | "auto" | None (not managing)
        self.last_active = None   # timestamp of the last cycle with GPU activity
        atexit.register(self.release)

    def release(self) -> None:
        """Back to always-high, the safe default."""
        if self.mode != "high" and _perf_files():
            set_level("high")
        self.mode = None
        self.last_active = None  # re-enable starts a fresh idle timer

    def update(self, temps: list, settings: dict, now: float, log) -> dict:
        """Call once per loop cycle with the fresh sensor list. Returns UI state."""
        global _forgot
        if _forgot:
            _forgot, self.mode = False, None
        files = _perf_files()
        if not settings.get("perf_control") or not files:
            if self.mode is not None:
                log("Perf control off -> all GPUs back to high.")
                self.release()
            return {"perf_mode": "unmanaged" if not files else "off (always high)", "perf_idle_remaining_sec": 0}

        active = any(
            (t.get("busy_pct") is not None and t["busy_pct"] >= settings.get("perf_busy_pct", 20))
            or (t.get("power_w") is not None and t["power_w"] >= settings.get("perf_power_w", 80))
            for t in temps
        )
        idle_sec = settings.get("perf_idle_sec", 600)
        if active or self.last_active is None:
            self.last_active = now

        want = "high" if active or now - self.last_active < idle_sec else "auto"
        if want != self.mode:
            if set_level(want):
                log(f"GPU perf level -> {want}" + (f" (idle {idle_sec:.0f}s)" if want == "auto" else ""))
                self.mode = want
            else:
                set_level("high")
                self.mode = "high"
        remaining = 0 if self.mode == "auto" else max(0, round(idle_sec - (now - self.last_active)))
        return {"perf_mode": self.mode, "perf_idle_remaining_sec": remaining}
