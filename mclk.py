"""
Optional memory clock step (off by default): raise each MI50's top MCLK state (smcPPTable/FreqTableUclk/2
and /3) to FAN_BRIDGE_MCLK once per container start, with the GPUs idle.

- Cards are matched by PCI address (FAN_BRIDGE_MCLK_PCI), not cardN.
- A card whose top state already reads the target is skipped, so a restart
  does not rewrite it.
- Each card's pp_table is backed up to /data/pp_backup before the write; on
  any failure that backup is written back and the card is left at stock.
- Runs in its own thread: the fan loop never waits on it.
- FAN_BRIDGE_MCLK=0 or off: never touch clocks.

Only the two UCLK keys change. No timings, voltages, SCLK or power limit.
"""

import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time

import ilo_fan

TARGET_RAW = os.environ.get("FAN_BRIDGE_MCLK", "off").strip().lower()
TARGET_PCI = [p.strip() for p in os.environ.get("FAN_BRIDGE_MCLK_PCI", "").split(",") if p.strip()]
BACKUP_DIR = os.environ.get("FAN_BRIDGE_MCLK_BACKUP_DIR", "/data/pp_backup")
GPU_GLOB = "/gpu/*"                  # writable sysfs device dirs (see docker-compose.yml)
IDLE_BUSY_PCT = 5
IDLE_VRAM_MIB = 512                  # no model loaded
IDLE_HOLD_SEC = 10                   # idle this long before writing
WAIT_MAX_SEC = int(os.environ.get("FAN_BRIDGE_MCLK_WAIT_SEC", "1800"))
UCLK_KEYS = ("smcPPTable/FreqTableUclk/2", "smcPPTable/FreqTableUclk/3")

state = {"target": None, "status": "not started", "cards": {}}   # shown on the dashboard
_lock = threading.Lock()


def _target():
    if TARGET_RAW in ("", "0", "off", "false", "no"):
        return None
    return int(TARGET_RAW)


def _read(path):
    with open(path) as f:
        return f.read()


def _pci_of(dev):
    for line in _read(os.path.join(dev, "uevent")).splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            return line.split("=", 1)[1]
    return None


def _cards():
    """{pci: dev_dir} for the mounted target GPUs."""
    out = {}
    for dev in glob.glob(GPU_GLOB):
        try:
            pci = _pci_of(dev)
        except OSError:
            continue
        if pci in TARGET_PCI:
            out[pci] = dev
    return out


def top_mclk(dev):
    """Top pp_dpm_mclk state in MHz, e.g. '2: 1180Mhz *' -> 1180."""
    last = _read(os.path.join(dev, "pp_dpm_mclk")).strip().splitlines()[-1]
    return int(last.split(":")[1].strip().split("Mhz")[0])


def _idle(dev):
    busy = int(_read(os.path.join(dev, "gpu_busy_percent")).strip())
    vram = int(_read(os.path.join(dev, "mem_info_vram_used")).strip()) // (1 << 20)
    return busy <= IDLE_BUSY_PCT and vram < IDLE_VRAM_MIB


def _set_card(pci, **kw):
    with _lock:
        state["cards"].setdefault(pci, {}).update(kw)


def _write_table(dev, data: bytes):
    with open(os.path.join(dev, "pp_table"), "wb") as f:
        f.write(data)


def _apply(pci, dev, target, log):
    level_path = os.path.join(dev, "power_dpm_force_performance_level")
    level = _read(level_path).strip()
    current = open(os.path.join(dev, "pp_table"), "rb").read()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, f"{pci.replace(':', '_')}-{time.strftime('%Y%m%d-%H%M%S')}.pp_table")
    with open(backup, "wb") as f:
        f.write(current)

    with tempfile.TemporaryDirectory() as tmp:
        new_path = os.path.join(tmp, "new.pp_table")
        shutil.copyfile(backup, new_path)
        cmd = ["upp", "-p", new_path, "set", "--write"] + [f"{k}={target}" for k in UCLK_KEYS]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        new = open(new_path, "rb").read()

    try:
        _write_table(dev, new)
        time.sleep(2)
        with open(level_path, "w") as f:
            f.write(level)      # a pp_table write resets the perf level; keep perf.py in sync
        got = top_mclk(dev)
        readback = open(os.path.join(dev, "pp_table"), "rb").read()
        if got != target or readback != new:
            raise RuntimeError(f"verify failed: top MCLK {got}, readback {'ok' if readback == new else 'differs'}")
    except Exception as e:
        log(f"MCLK {pci}: {e} -> restoring backup {os.path.basename(backup)}")
        try:
            _write_table(dev, current)
            time.sleep(2)
            with open(level_path, "w") as f:
                f.write(level)
        except Exception as e2:
            log(f"MCLK {pci}: RESTORE FAILED: {e2}")
        raise
    return backup


def run(log):
    target = _target()
    with _lock:
        state["target"] = target
    if target is None:
        with _lock:
            state["status"] = "off (FAN_BRIDGE_MCLK)"
        log("MCLK step off (FAN_BRIDGE_MCLK=0/off); not touching clocks.")
        return

    if not TARGET_PCI:
        with _lock:
            state["status"] = "off (FAN_BRIDGE_MCLK_PCI not set)"
        log("MCLK step: FAN_BRIDGE_MCLK_PCI not set; not touching clocks.")
        return

    cards = _cards()
    missing = [p for p in TARGET_PCI if p not in cards]
    if missing:
        log(f"MCLK step: GPU(s) {missing} not mounted under /gpu; skipping them.")
    for pci, dev in cards.items():
        try:
            _set_card(pci, top_mclk=top_mclk(dev))
        except Exception as e:
            _set_card(pci, result=f"read error: {e}")

    todo = {}
    for pci, dev in cards.items():
        try:
            if top_mclk(dev) >= target:
                _set_card(pci, result="skipped (already at target)")
                log(f"MCLK {pci}: top state already {top_mclk(dev)} MHz, skipping.")
            else:
                todo[pci] = dev
        except Exception as e:
            log(f"MCLK {pci}: cannot read pp_dpm_mclk: {e}; skipping.")
    if not todo:
        with _lock:
            state["status"] = "done"
        return

    with _lock:
        state["status"] = "waiting for idle GPUs"
    log(f"MCLK step: waiting for {', '.join(todo)} to be idle (busy <= {IDLE_BUSY_PCT}%, VRAM < {IDLE_VRAM_MIB} MiB) ...")
    deadline = time.time() + WAIT_MAX_SEC
    idle_since = None
    while True:
        try:
            all_idle = all(_idle(d) for d in cards.values())
        except Exception:
            all_idle = False
        now = time.time()
        idle_since = (idle_since or now) if all_idle else None
        if idle_since and now - idle_since >= IDLE_HOLD_SEC:
            break
        if now > deadline:
            with _lock:
                state["status"] = f"gave up: GPUs not idle within {WAIT_MAX_SEC}s (clocks untouched)"
            log(f"MCLK step: GPUs not idle within {WAIT_MAX_SEC}s; leaving clocks untouched.")
            return
        time.sleep(1)

    with _lock:
        state["status"] = "applying"
    for pci, dev in todo.items():
        try:
            backup = _apply(pci, dev, target, log)
            _set_card(pci, top_mclk=top_mclk(dev), result=f"set {target} MHz (backup {os.path.basename(backup)})")
            log(f"MCLK {pci}: top state now {top_mclk(dev)} MHz.")
        except Exception as e:
            try:
                tm = top_mclk(dev)
            except Exception:
                tm = None
            _set_card(pci, top_mclk=tm, result=f"FAILED, left at stock: {e}")
    with _lock:
        state["status"] = "done"


def start_thread(log) -> threading.Thread:
    def _guarded():
        try:
            run(log)
        except Exception as e:
            log(f"MCLK step crashed: {e!r}")
            with _lock:
                state["status"] = f"crashed: {e!r}"
    t = threading.Thread(target=_guarded, daemon=True, name="mclk-step")
    t.start()
    return t


def snapshot():
    with _lock:
        return {"target": state["target"], "status": state["status"],
                "cards": {k: dict(v) for k, v in state["cards"].items()}}
