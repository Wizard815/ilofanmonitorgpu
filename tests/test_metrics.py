"""GET /metrics: Prometheus text format, open like the other reads."""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SETTINGS_PATH", os.path.join(tempfile.mkdtemp(), "settings.json"))
os.environ["FAN_BRIDGE_NO_LOOP"] = "1"

import app as fan_app  # noqa: E402
import metrics         # noqa: E402

STATE = {
    "last_poll_ts": 1700000000.5, "dry_run": False, "phase": "hold", "countdown_sec": 42,
    "gpus": [{"card": "card0", "pci": "0000:03:00.0", "edge_c": 61.0, "junction_c": 78.0, "memory_c": 80.0,
              "power_w": 212.5, "busy_pct": 97, "mclk_mhz": None}],
    "loaded_cards": ["card0"],
    "control_c": 91.0, "predicted_c": 92.4, "rate_c_per_s": 0.3,
    "target_pct": 45, "pending_pct": None, "floor_pct": 50, "baseline_pct": 22,
    "latency": {"pct": 50, "patch_ms": 640, "detect_to_applied_ms": 910, "ts": 1700000000.0},
    "ilo": {"fans": [{"name": "Fan 1", "pct": 51}, {"name": 'Odd "fan"', "pct": None}],
            "floor_pct": 50, "ambient_c": 24.0, "ts": 1700000000.0, "error": None},
}
JBOD = {"enabled": True, "phase": "ok", "target_pct": 35, "sent_pct": 35, "esp_ok": True,
        "esp": {"pct": 35, "rpm": [900, 0, 910], "stalled": [False, True, False], "probe_c": 31.5},
        "drives": [{"name": "disk1", "device": "sdc", "temp_c": 38, "used": True},
                   {"name": "disk2", "device": "sdd", "temp_c": None, "used": True}]}

LINE = re.compile(r'^[a-z_]+(\{[a-z_]+="(?:[^"\\]|\\.)*"(,[a-z_]+="(?:[^"\\]|\\.)*")*\})? -?[0-9.e+-]+$')


def test_metrics_render_values_and_format():
    text = metrics.render(STATE, JBOD)
    for line in text.splitlines():
        assert line.startswith("# HELP ") or line.startswith("# TYPE ") or LINE.match(line), line
    assert 'fanbridge_gpu_junction_celsius{card="card0",pci="0000:03:00.0"} 78' in text
    assert 'fanbridge_gpu_power_watts{card="card0",pci="0000:03:00.0"} 212.5' in text
    assert "fanbridge_target_fan_percent 45" in text
    assert "fanbridge_applied_fan_percent 50" in text
    assert "fanbridge_takeover 1" in text
    assert 'fanbridge_phase{phase="hold"} 1' in text and 'fanbridge_phase{phase="ilo"} 0' in text
    assert "fanbridge_ilo_write_seconds 0.64" in text
    assert 'fanbridge_ilo_fan_percent{fan="Fan 1"} 51' in text
    assert 'fanbridge_jbod_fan_rpm{fan="3"} 910' in text
    assert 'fanbridge_jbod_drive_celsius{drive="disk1",device="sdc"} 38' in text
    # unknown values are left out, not sent as 0
    assert "fanbridge_pending_fan_percent" not in text
    assert "fanbridge_gpu_mclk_mhz" not in text
    assert 'drive="disk2",device="sdd"} ' not in text.split("fanbridge_jbod_drive_used")[0]
    assert 'Odd \\"fan\\"' not in text   # pct None -> no sample at all
    # one HELP/TYPE per metric
    assert text.count("# TYPE fanbridge_phase gauge") == 1


def test_metrics_without_jbod_or_ilo():
    text = metrics.render({"phase": "starting", "gpus": []}, {"enabled": False})
    assert "fanbridge_up 1" in text and "fanbridge_jbod_enabled 0" in text
    assert "fanbridge_jbod_fan_rpm" not in text and "fanbridge_ilo_fan_percent" not in text
    assert "fanbridge_takeover 0" in text


def test_metrics_endpoint_is_open_even_with_a_token(monkeypatch):
    monkeypatch.setattr(fan_app, "TOKEN", "s3cret")
    r = fan_app.app.test_client().get("/metrics")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    assert b"fanbridge_up 1" in r.data
