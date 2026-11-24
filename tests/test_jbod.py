import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SETTINGS_PATH", os.path.join(tempfile.mkdtemp(), "settings.json"))

import config  # noqa: E402
import jbod  # noqa: E402

INI = '''["parity"]
idx="0"
name="parity"
device="sdb"
id="WDC_WD80EFAX_VAK111"
type="Parity"
temp="36"
["disk1"]
name="disk1"
device="sdc"
id="ST8000_ZA222"
type="Data"
temp="*"
["disk2"]
name="disk2"
device="sdd"
id="HGST_HUH721010_7PG333"
type="Data"
temp="42"
["flash"]
name="flash"
device="sda"
type="Flash"
temp="*"
'''


def S(**kw):
    s = dict(config.DEFAULTS)
    s.update(kw)
    return s


class JbodTest(unittest.TestCase):
    def test_parse_disks_ini(self):
        d = jbod.parse_disks_ini(INI)
        self.assertEqual([x["name"] for x in d], ["parity", "disk1", "disk2"])  # flash skipped
        self.assertEqual(d[0]["temp_c"], 36.0)
        self.assertTrue(d[1]["standby"])
        self.assertIsNone(d[1]["temp_c"])

    def test_select_explicit(self):
        d = jbod.select_drives(jbod.parse_disks_ini(INI), ["disk1", "sdd"])
        self.assertEqual([x["used"] for x in d], [False, True, True])
        d = jbod.select_drives(jbod.parse_disks_ini(INI), ["8000_ZA222"])   # serial substring
        self.assertEqual([x["used"] for x in d], [False, True, False])

    def test_select_auto_all_when_no_expander(self):
        d = jbod.select_drives(jbod.parse_disks_ini(INI), [])
        self.assertTrue(all(x["used"] for x in d))

    def test_auto_fallback_skips_nvme_and_ssd(self):
        ini = INI + '["cache"]\nname="cache"\ndevice="nvme0n1"\ntype="Cache"\ntemp="48"\n' \
                    '["ssd"]\nname="ssd"\ndevice="sdz"\nrotational="0"\ntype="Cache"\ntemp="45"\n'
        d = jbod.select_drives(jbod.parse_disks_ini(ini), [])
        self.assertEqual({x["name"] for x in d if x["used"]}, {"parity", "disk1", "disk2"})
        self.assertEqual(jbod.compute_target(d, S())[2]["name"], "disk2")

    def test_target_hottest_rounded_up(self):
        d = jbod.select_drives(jbod.parse_disks_ini(INI), [])
        pct, why, hot = jbod.compute_target(d, S())
        self.assertEqual(hot["name"], "disk2")
        # 42 C on the default curve: between 41->55 and 45->80 = 61.25 -> 65
        self.assertEqual(pct, 65)

    def test_target_never_below_min(self):
        d = [{"name": "a", "device": "sdx", "temp_c": 25.0, "used": True}]
        self.assertEqual(jbod.compute_target(d, S(jbod_min_pct=30))[0], 30)
        d = [{"name": "a", "device": "sdx", "temp_c": None, "used": True}]
        pct, why, hot = jbod.compute_target(d, S(jbod_min_pct=30))
        self.assertEqual((pct, hot), (30, None))
        self.assertIn("spun down", why)

    def test_slew_up_fast_down_slow(self):
        s = S(jbod_hold_sec=60, jbod_step_down_pct=10, jbod_step_down_sec=30)
        w = jbod.Slew()
        self.assertEqual(w.decide(70, 0, s)[:2], (70, "ok"))
        self.assertEqual(w.decide(40, 10, s)[:2], (70, "hold"))
        self.assertEqual(w.decide(40, 59, s)[:2], (70, "hold"))
        self.assertEqual(w.decide(40, 60, s)[:2], (60, "ramp"))
        self.assertEqual(w.decide(40, 75, s)[:2], (60, "ramp"))
        self.assertEqual(w.decide(40, 90, s)[:2], (50, "ramp"))
        self.assertEqual(w.decide(90, 91, s)[:2], (90, "ok"))      # back up at once
        self.assertEqual(w.decide(85, 92, s)[:2], (90, "hold"))
        self.assertEqual(w.decide(85, 152, s)[:2], (85, "ok"))     # step stops at target

    def test_settings_jbod_curve_validated(self):
        out = config.save({"jbod_curve": [[30, 25], [35, 30], [38, 40], [41, 55], [45, 80], [48, 100]],
                           "jbod_min_pct": 5, "jbod_source": "smartctl"})
        self.assertEqual(out["jbod_min_pct"], 20)                   # clamped: never below 20
        self.assertEqual(out["jbod_source"], "smartctl")
        with self.assertRaises(ValueError):
            config.save({"jbod_curve": [[30, 25]]})


if __name__ == "__main__":
    unittest.main()
