"""power.py and /api/power: caps and clock limits on mounted MI50 sysfs dirs (fake ones here)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SETTINGS_PATH", os.path.join(tempfile.mkdtemp(), "settings.json"))
os.environ["FAN_BRIDGE_NO_LOOP"] = "1"

import perf   # noqa: E402
import power  # noqa: E402

LEVELS = "0: 925Mhz\n1: 1138Mhz\n2: 1269Mhz\n3: 1348Mhz\n4: 1440Mhz\n5: 1528Mhz\n6: 1600Mhz\n7: 1683Mhz\n8: 1725Mhz *\n"


def make_gpu(root, name, pci, cap=225, device="0x66a1"):
    dev = os.path.join(root, name)
    hw = os.path.join(dev, "hwmon", "hwmon3")
    os.makedirs(hw)
    files = {
        os.path.join(dev, "vendor"): "0x1002", os.path.join(dev, "device"): device,
        os.path.join(dev, "uevent"): f"DRIVER=amdgpu\nPCI_SLOT_NAME={pci}\n",
        os.path.join(dev, "pp_dpm_sclk"): LEVELS, os.path.join(dev, "power_dpm_force_performance_level"): "auto",
        os.path.join(hw, "power1_cap"): str(int(cap * 1e6)), os.path.join(hw, "power1_cap_min"): str(int(50e6)),
        os.path.join(hw, "power1_cap_max"): str(int(300e6)), os.path.join(hw, "power1_cap_default"): str(int(225e6)),
        os.path.join(hw, "power1_average"): str(int(190e6)),
    }
    for p, v in files.items():
        with open(p, "w") as f:
            f.write(v)
    return dev, hw


def _cap(hw):
    with open(os.path.join(hw, "power1_cap")) as f:
        return int(f.read()) / 1e6


def _level(dev):
    with open(os.path.join(dev, "power_dpm_force_performance_level")) as f:
        return f.read()


def setup(monkeypatch, tmp):
    monkeypatch.setattr(power, "GPU_GLOB", os.path.join(tmp, "*"))
    monkeypatch.setattr(perf, "PERF_GLOB", os.path.join(tmp, "*", "power_dpm_force_performance_level"))
    power.limits.clear()


def test_power_lists_only_mi50s(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    make_gpu(str(tmp_path), "card1", "0000:03:00.0", cap=178)
    make_gpu(str(tmp_path), "card9", "0000:09:00.0", device="0x73bf")  # an RX 6800: never touched
    cards = power.cards()
    assert [c["pci"] for c in cards] == ["0000:03:00.0"]
    c = power.public(cards[0])
    assert (c["cap_w"], c["cap_min_w"], c["cap_max_w"], c["cap_default_w"]) == (178, 50, 300, 225)
    assert c["sclk_levels_mhz"][-1] == 1725 and not c["sclk_limited"] and "_dev" not in c


def test_power_sets_cap_and_clock_limit_then_lifts_it(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    dev, hw = make_gpu(str(tmp_path), "card1", "0000:03:00.0")
    applied, errors = power.apply({"0000:03:00.0": {"cap_w": 150, "sclk_mhz": 1300}}, control_c=70, log=lambda m: None)
    assert errors == [] and _cap(hw) == 150 and _level(dev) == "manual"
    with open(os.path.join(dev, "pp_dpm_sclk")) as f:
        assert f.read() == "0 1 2"
    assert power.held_cards() == {"0000:03:00.0"} and power.cards()[0]["sclk_limit_mhz"] == 1269
    applied, errors = power.apply({"0000:03:00.0": {"cap_w": None, "sclk_mhz": None}}, control_c=70, log=lambda m: None)
    assert errors == [] and _cap(hw) == 225 and _level(dev) == "auto" and power.held_cards() == set()


def test_power_refuses_a_raise_when_hot_or_in_failsafe_but_allows_a_cut(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    _, hw = make_gpu(str(tmp_path), "card1", "0000:03:00.0", cap=150)
    _, errors = power.apply({"0000:03:00.0": {"cap_w": 200}}, control_c=97, log=lambda m: None)
    assert "raise refused" in errors[0] and _cap(hw) == 150
    _, errors = power.apply({"0000:03:00.0": {"cap_w": 200}}, control_c=60, failsafe=True, log=lambda m: None)
    assert "failsafe" in errors[0] and _cap(hw) == 150
    _, errors = power.apply({"0000:03:00.0": {"cap_w": 125}}, control_c=97, log=lambda m: None)
    assert errors == [] and _cap(hw) == 125


def test_power_stays_inside_the_cards_range(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    _, hw = make_gpu(str(tmp_path), "card1", "0000:03:00.0")
    _, errors = power.apply({"0000:03:00.0": {"cap_w": 400}, "0000:99:00.0": {"cap_w": 150}}, control_c=50, log=lambda m: None)
    assert "outside" in errors[0] and "not a mounted" in errors[1] and _cap(hw) == 225


def test_perf_on_demand_leaves_a_clock_limited_card_alone(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    dev1, _ = make_gpu(str(tmp_path), "card1", "0000:03:00.0")
    dev2, _ = make_gpu(str(tmp_path), "card2", "0000:04:00.0")
    power.apply({"0000:03:00.0": {"cap_w": 150, "sclk_mhz": 1300}}, control_c=50, log=lambda m: None)
    assert perf.set_level("high")
    assert _level(dev1) == "manual" and _level(dev2) == "high"


def test_api_power(monkeypatch, tmp_path):
    setup(monkeypatch, str(tmp_path))
    _, hw = make_gpu(str(tmp_path), "card1", "0000:03:00.0")
    import app as fan_app
    import loop
    monkeypatch.setattr(loop, "state", {**loop.state, "control_c": 60.0, "phase": "ilo",
                                        "gpus": [{"pci": "0000:03:00.0", "junction_c": 60.0, "edge_c": 45.0, "memory_c": 58.0}]})
    c = fan_app.app.test_client()
    d = c.get("/api/power").get_json()
    assert d["cards"][0]["cap_w"] == 225 and d["cards"][0]["junction_c"] == 60.0 and d["fans"]["phase"] == "ilo"
    r = c.post("/api/power", json={"cards": {"0000:03:00.0": {"cap_w": 160, "sclk_mhz": None}}})
    assert r.status_code == 200 and r.get_json()["ok"] and _cap(hw) == 160
    assert c.post("/api/power", json={"nope": 1}).status_code == 400
    monkeypatch.setattr(power, "TOKEN", "s3cret")
    assert c.post("/api/power", json={"cards": {}}).status_code == 403
    r = c.post("/api/power", json={"cards": {"0000:03:00.0": {"cap_w": 400}}}, headers={"X-Power-Token": "s3cret"})
    assert r.status_code == 409 and "outside" in r.get_json()["errors"][0]
