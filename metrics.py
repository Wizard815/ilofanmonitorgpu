"""
Prometheus text exposition (format 0.0.4) for GET /metrics, written by hand
so the image needs no new package. render() is pure: it takes the same
snapshots /api/state returns, so it is unit-tested in tests/test_metrics.py.

Temperatures are Celsius, power watts, times seconds, fan speeds percent
(iLO reports percent, not rpm) and JBOD fans rpm. A value that is unknown
(no sensor, no iLO readback yet) is left out rather than sent as 0.
"""

import math

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

PHASES = ("starting", "ilo", "boost", "hold", "ramp", "failsafe", "paused")
# fan-bridge holds a minimum above what iLO does on its own in these phases.
TAKEOVER_PHASES = ("boost", "hold", "ramp", "failsafe")
JBOD_PHASES = ("off", "ok", "hold", "ramp", "failsafe", "no-link")


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


class _Out:
    def __init__(self):
        self.lines = []
        self._seen = set()

    def add(self, name, help_, kind, value, labels=None):
        v = _num(value)
        if v is None:
            return
        if name not in self._seen:
            self._seen.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(val)}"' for k, val in labels.items()) + "}"
        self.lines.append(f"{name}{lab} {int(v) if v.is_integer() and abs(v) < 1e15 else repr(v)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(st: dict, jb: dict) -> str:
    """st = loop.get_state_snapshot(), jb = jbod.snapshot()."""
    o = _Out()
    g_ = "gauge"
    o.add("fanbridge_up", "1 while fan-bridge answers.", g_, 1)
    o.add("fanbridge_last_poll_timestamp_seconds", "Unix time of the last GPU sensor poll.", g_, st.get("last_poll_ts"))
    o.add("fanbridge_dry_run", "1 when there are no iLO credentials and nothing is sent.", g_, bool(st.get("dry_run")))

    for g in st.get("gpus") or []:
        lab = {"card": g.get("card") or "", "pci": g.get("pci") or ""}
        o.add("fanbridge_gpu_edge_celsius", "GPU edge temperature.", g_, g.get("edge_c"), lab)
        o.add("fanbridge_gpu_junction_celsius", "GPU junction (hotspot) temperature.", g_, g.get("junction_c"), lab)
        o.add("fanbridge_gpu_hbm_celsius", "GPU HBM (memory) temperature.", g_, g.get("memory_c"), lab)
        o.add("fanbridge_gpu_power_watts", "GPU power draw.", g_, g.get("power_w"), lab)
        o.add("fanbridge_gpu_busy_percent", "GPU busy percent.", g_, g.get("busy_pct"), lab)
        o.add("fanbridge_gpu_mclk_mhz", "GPU memory clock.", g_, g.get("mclk_mhz"), lab)
        o.add("fanbridge_gpu_loaded", "1 while this GPU counts as under load.", g_,
              (g.get("card") in (st.get("loaded_cards") or [])), lab)

    o.add("fanbridge_control_celsius", "Control temperature the curve runs on.", g_, st.get("control_c"))
    o.add("fanbridge_predicted_celsius", "Control temperature looked ahead along the trend.", g_, st.get("predicted_c"))
    o.add("fanbridge_control_rate_celsius_per_second", "Rise rate of the control temperature.", g_, st.get("rate_c_per_s"))
    o.add("fanbridge_target_fan_percent", "Fan minimum the curve and load ask for.", g_, st.get("target_pct"))
    o.add("fanbridge_pending_fan_percent", "Fan minimum sent to iLO, not yet confirmed.", g_, st.get("pending_pct"))
    o.add("fanbridge_applied_fan_percent", "Fan minimum iLO has accepted from fan-bridge.", g_, st.get("floor_pct"))
    o.add("fanbridge_baseline_fan_percent", "What iLO runs the fans at on its own.", g_, st.get("baseline_pct"))

    phase = st.get("phase")
    for p in PHASES:
        o.add("fanbridge_phase", "1 for the current control phase.", g_, p == phase, {"phase": p})
    o.add("fanbridge_takeover", "1 while fan-bridge holds a minimum above iLO's own speed.", g_,
          phase in TAKEOVER_PHASES)
    o.add("fanbridge_countdown_seconds", "Hold: until the first step down; ramp: until the next step.", g_,
          st.get("countdown_sec"))

    lat = st.get("latency") or {}
    if lat.get("patch_ms") is not None:
        o.add("fanbridge_ilo_write_seconds", "Duration of the last fan-minimum raise PATCH to iLO.", g_,
              lat["patch_ms"] / 1000)
    if lat.get("detect_to_applied_ms") is not None:
        o.add("fanbridge_detect_to_applied_seconds", "From detecting the last raise to iLO accepting it.", g_,
              lat["detect_to_applied_ms"] / 1000)

    ilo = st.get("ilo") or {}
    o.add("fanbridge_ilo_floor_percent", "Fan minimum as read back from iLO.", g_, ilo.get("floor_pct"))
    o.add("fanbridge_ilo_ambient_celsius", "Ambient temperature reported by iLO.", g_, ilo.get("ambient_c"))
    o.add("fanbridge_ilo_readback_timestamp_seconds", "Unix time of the last iLO readback.", g_, ilo.get("ts"))
    o.add("fanbridge_ilo_readback_ok", "0 when the last iLO readback failed.", g_,
          not ilo.get("error") if ilo.get("ts") or ilo.get("error") else None)
    for f in ilo.get("fans") or []:
        o.add("fanbridge_ilo_fan_percent", "Actual fan speed reported by iLO.", g_, f.get("pct"),
              {"fan": f.get("name") or "Fan"})

    o.add("fanbridge_jbod_enabled", "1 when the JBOD fan control is on.", g_, bool(jb.get("enabled")))
    if jb.get("enabled"):
        jp = jb.get("phase")
        for p in JBOD_PHASES:
            o.add("fanbridge_jbod_phase", "1 for the current JBOD fan phase.", g_, p == jp, {"phase": p})
        o.add("fanbridge_jbod_target_fan_percent", "JBOD fan speed the drive curve asks for.", g_, jb.get("target_pct"))
        o.add("fanbridge_jbod_sent_fan_percent", "JBOD fan speed sent to the ESP32.", g_, jb.get("sent_pct"))
        o.add("fanbridge_jbod_esp_up", "1 when the last ESP32 request worked.", g_, bool(jb.get("esp_ok")))
        esp = jb.get("esp") or {}
        o.add("fanbridge_jbod_applied_fan_percent", "JBOD fan speed the ESP32 reports.", g_, esp.get("pct"))
        o.add("fanbridge_jbod_probe_celsius", "ESP32 air probe temperature.", g_, esp.get("probe_c"))
        for i, r in enumerate(esp.get("rpm") or []):
            o.add("fanbridge_jbod_fan_rpm", "JBOD fan speed.", g_, r, {"fan": str(i + 1)})
        for i, s in enumerate(esp.get("stalled") or []):
            o.add("fanbridge_jbod_fan_stalled", "1 when the ESP32 sees this fan stalled.", g_, bool(s), {"fan": str(i + 1)})
        for d in jb.get("drives") or []:
            lab = {"drive": d.get("name") or "", "device": d.get("device") or ""}
            o.add("fanbridge_jbod_drive_celsius", "Drive temperature (absent while spun down).", g_, d.get("temp_c"), lab)
            o.add("fanbridge_jbod_drive_used", "1 when the drive counts towards the JBOD curve.", g_,
                  bool(d.get("used")), lab)
    return o.text()
