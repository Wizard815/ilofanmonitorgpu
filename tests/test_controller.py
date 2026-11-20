import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SETTINGS_PATH"] = os.path.join(tempfile.mkdtemp(), "settings.json")

import config  # noqa: E402
import loop    # noqa: E402

S = {**config.DEFAULTS, "hold_sec": 60, "step_down_sec": 15, "step_down_pct": 5, "min_fan_pct": 0, "quantize_pct": 5}


def test_target_rounds_up_and_load_wins():
    t, why = loop.compute_target(72, [], S)          # curve 25..35 between 70 and 78 -> 27.5 -> 30
    assert t == 30 and "curve" in why
    t, why = loop.compute_target(50, ["card1"], S)
    assert t == S["load_floor_pct"] and "load" in why
    assert loop.compute_target(40, [], S) == (0, "below curve")


def test_control_temp_uses_hbm_offset():
    gpus = [{"card": "a", "fan_curve_c": 70, "memory_c": 65}, {"card": "b", "fan_curve_c": 74, "memory_c": 60}]
    assert loop.control_temp(gpus, 11) == (76, "a", "HBM 65 + 11")
    assert loop.control_temp(gpus, None) == (74, "b", "junction")
    assert loop.control_temp([], 11) == (None, None, None)


def test_fast_up_hold_then_ramp():
    c = loop.FloorController()
    d, ph, _ = c.decide(40, 0, S); assert (d, ph) == (40, "boost"); c.commit(d)
    d, ph, _ = c.decide(60, 1, S); assert d == 60; c.commit(d)
    d, ph, cd = c.decide(0, 2, S); assert (d, ph, cd) == (60, "hold", 59)
    d, ph, _ = c.decide(0, 61, S); assert (d, ph) == (55, "ramp"); c.commit(d)
    d, ph, _ = c.decide(0, 70, S); assert d == 55            # waits step_down_sec
    d, ph, _ = c.decide(0, 76, S); assert d == 50; c.commit(d)
    d, ph, _ = c.decide(70, 77, S); assert (d, ph) == (70, "boost")   # load returns: straight back up


def test_ramp_reaches_idle():
    c = loop.FloorController(); c.adopt(10, 0)
    d, ph, _ = c.decide(0, 61, S); assert d == 5; c.commit(d)
    d, ph, _ = c.decide(0, 76, S); assert d == 0; c.commit(d)
    d, ph, _ = c.decide(0, 77, S); assert (d, ph) == (0, "ilo")


def test_settings_validation():
    s = config.save({"step_down_pct": 500, "bogus": 1, "perf_control": "true"})
    assert s["step_down_pct"] == 100 and "bogus" not in s and s["perf_control"] is True
    s = config.save({"thresholds": [[90, 10], [50, 50], [60, 20], [70, 30], [80, 40], [100, 100]]})
    temps = [p[0] for p in s["thresholds"]]; pcts = [p[1] for p in s["thresholds"]]
    assert temps == sorted(temps) and pcts == sorted(pcts)


def test_old_settings_migrate():
    import json
    with open(config.SETTINGS_PATH, "w") as f:
        json.dump({"target_max_c": 78, "cooldown_sec": 60, "thresholds": [[1, 2]] * 7, "load_floor_pct": 45}, f)
    config._cache["mtime"] = None
    s = config.load()
    assert "target_max_c" not in s and s["load_floor_pct"] == 45 and s["thresholds"] == config.DEFAULT_CURVE


def test_hold_paused_while_rising():
    c = loop.FloorController(); c.adopt(60, 0)
    assert c.decide(40, 100, S, rising=True)[1] == "hold"      # 100 s later, but still heating
    d, ph, cd = c.decide(40, 101, S); assert (ph, cd) == ("hold", 59)


def test_handoff_jumps_once_below_baseline():
    c = loop.FloorController(); c.adopt(35, 0)
    d, ph, _ = c.decide(0, 61, S, baseline=20); assert d == 30; c.commit(d)
    d, ph, _ = c.decide(0, 76, S, baseline=20); assert d == 25; c.commit(d)
    d, ph, _ = c.decide(0, 91, S, baseline=20); assert d == 0; c.commit(d)   # 20 would change nothing
    assert c.decide(0, 92, S, baseline=20) == (0, "ilo", 0)


def test_no_takeover_below_baseline():
    c = loop.FloorController(); c.commit(0)
    assert c.decide(15, 0, S, baseline=20) == (0, "ilo", 0)    # iLO already runs 20%
    d, ph, _ = c.decide(30, 1, S, baseline=20); assert (d, ph) == (30, "boost") and c.rise_ts == 1


def test_trend_and_predict():
    tr = loop.Trend()
    for i in range(9):
        rate = tr.add(i * 0.5, 60 + i)                          # 2 C/s
    assert abs(rate - 2) < 1e-6
    assert loop.predict(70, rate, {**S, "predict_sec": 3}) == 76
    assert loop.predict(70, -1, {**S, "predict_sec": 3}) == 70


def test_presets_roundtrip():
    s = config.apply_preset("Quiet")
    assert config.active_preset(s) == "Quiet"
    s = config.save({"load_floor_pct": 33})
    assert config.active_preset(s) is None
    s = config.save_preset("Mine")
    assert config.active_preset(s) == "Mine"
    try:
        config.save_preset("Quiet"); assert False
    except ValueError:
        pass
    s = config.delete_preset("Mine")
    assert "Mine" not in s["presets"]
