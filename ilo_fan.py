"""
iLO Redfish fan control + rocm-smi temperature reading.
Pulled out of the old monolithic fan_bridge.py so both the polling loop
(loop.py) and the Flask UI (app.py) can share it.
"""

import glob
import json
import os
import subprocess
from dataclasses import dataclass

import requests
from requests.auth import HTTPBasicAuth

ILO_HOST = os.environ.get("ILO_HOST", "")  # required: your iLO address
ILO_VERIFY_SSL = os.environ.get("ILO_VERIFY_SSL", "false").lower() == "true"
SECRETS_PATH = os.environ.get("ILO_SECRETS_PATH", "/run/secrets/ilo_creds.json")

LOG_PREFIX = "[fan-bridge]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Temperature reading
# ---------------------------------------------------------------------------

def _read_num(path: str):
    try:
        with open(path, "r") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _top_mclk(dev: str):
    """Top pp_dpm_mclk state in MHz ('2: 1180Mhz *' -> 1180), None if unreadable."""
    try:
        with open(os.path.join(dev, "pp_dpm_mclk")) as f:
            last = f.read().strip().splitlines()[-1]
        return int(last.split(":")[1].strip().split("Mhz")[0])
    except (OSError, IndexError, ValueError):
        return None


def get_gpu_sensors_sysfs() -> list:
    """
    Reads every amdgpu card straight from sysfs (~1 ms total, vs ~160 ms
    for a rocm-smi subprocess). Same dict shape as get_junction_temps(),
    plus power_w and busy_pct, which jump the instant a GPU starts work --
    seconds before the heatsink catches up. Returns [] if sysfs has no
    amdgpu hwmon (caller then falls back to rocm-smi).
    """
    results = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if "-" in os.path.basename(card):
            continue  # connectors like card1-DP-3
        dev = os.path.join(card, "device")
        hwmons = glob.glob(os.path.join(dev, "hwmon", "hwmon*"))
        if not hwmons:
            continue
        hw = hwmons[0]
        try:
            with open(os.path.join(hw, "name")) as f:
                if f.read().strip() != "amdgpu":
                    continue
        except OSError:
            continue
        temps = {}
        for inp in glob.glob(os.path.join(hw, "temp*_input")):
            try:
                with open(inp.replace("_input", "_label")) as f:
                    label = f.read().strip().lower()
            except OSError:
                continue
            val = _read_num(inp)
            if val is not None:
                temps[label] = val / 1000.0
        edge_val, junction_val, memory_val = temps.get("edge"), temps.get("junction"), temps.get("mem")
        sensors_present = [v for v in (junction_val, edge_val, memory_val) if v is not None]
        if not sensors_present:
            continue
        power = _read_num(os.path.join(hw, "power1_average"))
        if power is None:
            power = _read_num(os.path.join(hw, "power1_input"))
        busy = _read_num(os.path.join(dev, "gpu_busy_percent"))
        results.append({
            "card": os.path.basename(card),
            "pci": os.path.basename(os.path.realpath(dev)),
            "temp_c": max(sensors_present),
            "fan_curve_c": junction_val if junction_val is not None else edge_val,
            "fan_curve_kind": "junction" if junction_val is not None else "edge(fallback)",
            "edge_c": edge_val, "junction_c": junction_val, "memory_c": memory_val,
            "power_w": power / 1e6 if power is not None else None,
            "busy_pct": busy,
            "mclk_mhz": (lambda v: v / 1e6 if v is not None else None)(_read_num(os.path.join(hw, "freq2_input"))),
            "mclk_top_mhz": _top_mclk(dev),
        })
    return results


GPU_VENDOR = os.environ.get("GPU_VENDOR", "auto").strip().lower()   # auto | nvidia | amd
NV_FIELDS = "index,pci.bus_id,temperature.gpu,temperature.memory,power.draw,utilization.gpu,clocks.mem"


def _nv_num(s: str):
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None   # "N/A", "[N/A]", "[Not Supported]"


def get_gpu_sensors_nvidia() -> list:
    """
    NVIDIA GPUs via nvidia-smi (one call, ~50-100 ms). NVML exposes no
    hotspot/junction temperature, so the fan curve runs on the core temp
    (fan_curve_kind "gpu"). temperature.memory is only reported by HBM
    data-center cards (A100, V100, H100); GeForce/Quadro return N/A.
    Returns [] if nvidia-smi is missing or fails.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={NV_FIELDS}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    results = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 7:
            continue
        idx, bus, t_gpu, t_mem, power, util, mclk = parts
        t_gpu, t_mem = _nv_num(t_gpu), _nv_num(t_mem)
        if t_gpu is None and t_mem is None:
            continue
        results.append({
            "card": f"nvidia{idx}",
            "pci": bus.lower().replace("00000000:", "0000:"),
            "temp_c": max(v for v in (t_gpu, t_mem) if v is not None),
            "fan_curve_c": t_gpu,
            "fan_curve_kind": "gpu",
            "edge_c": t_gpu, "junction_c": None, "memory_c": t_mem,
            "power_w": _nv_num(power),
            "busy_pct": _nv_num(util),
            "mclk_mhz": _nv_num(mclk),
            "mclk_top_mhz": None,
        })
    return results


def get_gpu_sensors() -> list:
    """GPU_VENDOR=nvidia: nvidia-smi. amd: sysfs, then rocm-smi. auto: NVIDIA if present, else AMD."""
    if GPU_VENDOR == "nvidia":
        return get_gpu_sensors_nvidia()
    if GPU_VENDOR == "amd":
        return get_gpu_sensors_sysfs() or get_junction_temps()
    return get_gpu_sensors_nvidia() or get_gpu_sensors_sysfs() or get_junction_temps()


def get_junction_temps() -> list:
    """
    Returns per-GPU temp readings for all three sensors rocm-smi reports
    (edge, junction/hotspot, memory), confirmed against real hardware
    output showing all three per GPU.

    Two different values are provided per card, for two different jobs:
      - "fan_curve_c" / "fan_curve_kind": junction temp (falling back to
        edge if junction is absent) -- this is what the fan-control loop
        acts on, since junction/hotspot is the figure tied to throttling
        risk for these GPUs.
      - "temp_c": the MAX of whichever sensors were reported for that
        card (edge/junction/memory) -- this is the single "hottest spot"
        number shown in the simplified UI table, to
        just see each GPU's hottest reading rather than a 3-column
        breakdown.

    Returns a list of dicts:
      [{"card": "card0", "temp_c": 83.0,
        "fan_curve_c": 79.0, "fan_curve_kind": "junction",
        "edge_c": 68.0, "junction_c": 79.0, "memory_c": 83.0}, ...]
    Any *_c field is None if that sensor wasn't reported.
    """
    try:
        out = subprocess.run(
            ["rocm-smi", "--showtemp", "--json"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"ERROR reading rocm-smi: {e}")
        return []

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        log(f"ERROR parsing rocm-smi output: {out.stdout[:200]}")
        return []

    results = []
    for card, readings in data.items():
        if not isinstance(readings, dict):
            continue
        junction_val = None
        edge_val = None
        memory_val = None
        for key, val in readings.items():
            lk = key.lower()
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if "junction" in lk or "hotspot" in lk:
                junction_val = fval
            elif "memory" in lk:
                memory_val = fval
            elif "edge" in lk:
                edge_val = fval

        sensors_present = [v for v in (junction_val, edge_val, memory_val) if v is not None]
        if not sensors_present:
            continue

        hottest_spot = max(sensors_present)
        fan_curve_val = junction_val if junction_val is not None else edge_val
        fan_curve_kind = "junction" if junction_val is not None else "edge(fallback)"

        results.append({
            "card": card,
            "temp_c": hottest_spot,
            "fan_curve_c": fan_curve_val,
            "fan_curve_kind": fan_curve_kind,
            "edge_c": edge_val, "junction_c": junction_val, "memory_c": memory_val,
        })
    return results

# ---------------------------------------------------------------------------
# Fan curve
# ---------------------------------------------------------------------------

def curve_pct_for_temp(temp_c: float, settings: dict) -> int:
    """
    Pure temp->fan% lookup along the ramp (interpolated between the
    threshold points derived by config.derive_thresholds), independent of
    any timer/hand-back state. Below the lowest point, returns 0 (meaning
    "script has no opinion, iLO is in control"); above the highest point,
    clamps to max_fan_pct.
    """
    thresholds = settings["thresholds"]  # sorted [temp_c, pct] pairs
    if not thresholds:
        return 0
    if temp_c < thresholds[0][0]:
        return 0
    if temp_c >= thresholds[-1][0]:
        return thresholds[-1][1]
    for (t0, p0), (t1, p1) in zip(thresholds, thresholds[1:]):
        if t0 <= temp_c < t1:
            if t1 == t0:
                return p0
            frac = (temp_c - t0) / (t1 - t0)
            return round(p0 + frac * (p1 - p0))
    return thresholds[-1][1]


# ---------------------------------------------------------------------------
# iLO / Redfish
# ---------------------------------------------------------------------------

@dataclass
class IloCreds:
    username: str
    password: str


def load_creds(path: str = SECRETS_PATH) -> IloCreds:
    with open(path, "r") as f:
        data = json.load(f)
    return IloCreds(username=data["username"], password=data["password"])


class IloFanController:
    """
    Sets the GLOBAL minimum fan speed via HPE iLO Redfish -- confirmed
    working against this hardware by capturing the exact PATCH the iLO
    web UI's own "Fan Settings -> Minimum Fan Speed" dialog sends.

    iLO 5 (tested on v3.14, ML350 Gen10) does NOT support per-fan
    overrides -- the per-fan Thermal#Fans/N resources are read-only
    (HotPluggable/Location/Redundant only, confirmed by direct
    inspection). The only writable fan-speed control is this single
    chassis-wide floor, which applies to ALL fans at once -- there is no
    way to target just Fan 1 on this hardware. The "which fan(s) to
    control" checkboxes in the UI are consequently a no-op on this
    firmware; left in place in case a future firmware/box exposes
    per-fan control, but effectively ignored right now.
    """

    THERMAL_PATH = "/redfish/v1/Chassis/1/Thermal"

    def __init__(self, host: str = ILO_HOST, creds: IloCreds = None, verify_ssl: bool = ILO_VERIFY_SSL):
        if not host:
            raise KeyError("ILO_HOST is not set")
        self.base = f"https://{host}"
        self.creds = creds or load_creds()
        self.auth = HTTPBasicAuth(self.creds.username, self.creds.password)
        self.verify_ssl = verify_ssl
        self._session = requests.Session()
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def get_thermal(self) -> dict:
        r = self._session.get(
            self.base + self.THERMAL_PATH,
            auth=self.auth, verify=self.verify_ssl, timeout=10,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _fan_number(odata_id: str) -> str:
        return odata_id.rstrip("/").rsplit("/", 1)[-1]

    def list_fans(self) -> list:
        """Returns raw Fans[] entries from the Thermal resource (for the
        dashboard's fan list / diagnostics -- read-only on this firmware)."""
        try:
            thermal = self.get_thermal()
        except requests.RequestException as e:
            log(f"ERROR fetching thermal data from iLO: {e}")
            return []
        return thermal.get("Fans", [])

    def set_fan_floor_pct(self, pct: int, fan_ids=None) -> bool:
        """
        PATCHes the confirmed-working global minimum fan speed field.
        fan_ids is accepted for API-compatibility with the rest of the
        app but has NO EFFECT on this firmware -- there is no per-fan
        write path, only this one global floor covering every fan.
        """
        body = {"Oem": {"Hpe": {"FanPercentMinimum": int(pct)}}}
        r = None
        for attempt in (1, 2):
            try:
                r = self._session.patch(
                    self.base + self.THERMAL_PATH,
                    json=body,
                    auth=self.auth, verify=self.verify_ssl, timeout=10,
                )
                break
            except requests.ConnectionError as e:
                # iLO drops idle keep-alive connections; retry once on a fresh session
                self._session.close()
                self._session = requests.Session()
                if attempt == 2:
                    log(f"ERROR patching global fan minimum: {e}")
                    return False
            except requests.RequestException as e:
                log(f"ERROR patching global fan minimum: {e}")
                return False

        if r.status_code not in (200, 204):
            log(f"PATCH {self.THERMAL_PATH} -> {r.status_code}: {r.text[:200]}")
            return False

        log(f"Global fan minimum set to {pct}%")
        return True
