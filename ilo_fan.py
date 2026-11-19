"""
GPU sensor reading (sysfs, rocm-smi fallback), the fan curve, and the iLO
Redfish fan-minimum controller.
"""

import glob
import json
import os
import subprocess
import threading
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


def get_gpu_sensors() -> list:
    """sysfs first (fast, has power/busy); rocm-smi only if sysfs gives nothing."""
    return get_gpu_sensors_sysfs() or get_junction_temps()


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

def curve_pct_for_temp(temp_c: float, thresholds: list) -> int:
    """temp -> fan % along the curve (linear between points). 0 below the
    first point (no opinion, iLO in control); last point's % above the top."""
    if not thresholds or temp_c < thresholds[0][0]:
        return 0
    if temp_c >= thresholds[-1][0]:
        return int(thresholds[-1][1])
    for (t0, p0), (t1, p1) in zip(thresholds, thresholds[1:]):
        if t0 <= temp_c < t1:
            frac = (temp_c - t0) / (t1 - t0) if t1 > t0 else 0
            return round(p0 + frac * (p1 - p0))
    return int(thresholds[-1][1])


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
    iLO 5 has exactly one writable fan knob: the chassis-wide minimum fan
    speed (what Administration -> Fan Settings -> Minimum Fan Speed sends).
    It raises the floor of every fan at once; iLO's own thermal logic still
    runs on top and can go higher. Per-fan entries under Thermal#Fans are
    read-only on this firmware, so there is no way to target only the fans
    in front of the GPUs.
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
        self._lock = threading.Lock()  # loop thread and iLO poller share the session
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, **kw):
        """One retry on a fresh session: iLO drops idle keep-alive connections."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._session.request(
                        method, self.base + self.THERMAL_PATH,
                        auth=self.auth, verify=self.verify_ssl, timeout=10, **kw)
                except requests.ConnectionError:
                    self._session.close()
                    self._session = requests.Session()
                    if attempt == 2:
                        raise

    def get_thermal(self) -> dict:
        r = self._request("GET")
        r.raise_for_status()
        return r.json()

    def read_status(self) -> dict:
        """Actual fan speeds and the current minimum as iLO reports them.
        {"fans": [{"name", "pct", "health"}], "floor_pct": int|None,
         "ambient_c": float|None}"""
        thermal = self.get_thermal()
        fans = []
        for f in thermal.get("Fans", []):
            pct = f.get("Reading")
            if pct is None:
                pct = f.get("CurrentReading")  # older schema name
            status = f.get("Status") or {}
            if status.get("State") == "Absent":
                continue
            fans.append({
                "name": f.get("Name") or f.get("FanName") or "Fan",
                "pct": pct,
                "health": status.get("Health"),
            })
        hpe = (thermal.get("Oem") or {}).get("Hpe") or {}
        ambient = None
        for t in thermal.get("Temperatures", []):
            if "ambient" in (t.get("Name") or "").lower() and t.get("ReadingCelsius") is not None:
                ambient = t["ReadingCelsius"]
                break
        return {"fans": fans, "floor_pct": hpe.get("FanPercentMinimum"), "ambient_c": ambient}

    def set_fan_floor_pct(self, pct: int) -> bool:
        body = {"Oem": {"Hpe": {"FanPercentMinimum": int(pct)}}}
        try:
            r = self._request("PATCH", json=body)
        except requests.RequestException as e:
            log(f"ERROR setting fan minimum: {e}")
            return False
        if r.status_code not in (200, 204):
            log(f"PATCH {self.THERMAL_PATH} -> {r.status_code}: {r.text[:200]}")
            return False
        return True
