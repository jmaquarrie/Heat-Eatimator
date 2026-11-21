"""Offline checks for the what-if and push paths (no Home Assistant needed).

    python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev"))

import make_demo_data as demo  # noqa: E402
from heatplanner import data, planner  # noqa: E402


class Offline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        demo.main(cls.tmp)

    def setUp(self):
        data.DATA_DIR = self.tmp
        self.cfg = data.load_config()
        self.plan = data.load_json("plan_latest.json", None)

    def fake(self):
        ha = demo.FakeHA()
        planner.ensure_helper(ha, self.cfg)
        ha.posted = []
        ha.rest = lambda path, *a, **k: ha.posted.append((path, a[1] if len(a) > 1 else None))
        ha.state = lambda e: {"state": "on"}
        return ha

    def test_simulate_matches_stored_plan(self):
        for name in ("Optimised", "Current routine"):
            r = planner.simulate_values(self.cfg, self.plan, self.plan["strategies"][name]["values"])
            self.assertAlmostEqual(r["total"], self.plan["strategies"][name]["total"], places=6)
            self.assertTrue(r["exact"])

    def test_simulate_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            planner.simulate_values(self.cfg, self.plan, [18] * 6)
        with self.assertRaises(ValueError):
            planner.simulate_values(self.cfg, self.plan, [18, 20, 19, 25, 19, 20, 16])

    def test_legacy_plan_without_sim_block(self):
        legacy = {k: v for k, v in self.plan.items() if k != "sim"}
        r = planner.simulate_values(self.cfg, legacy, self.plan["strategies"]["Optimised"]["values"])
        self.assertFalse(r["exact"])
        self.assertLess(abs(r["total"] - self.plan["strategies"]["Optimised"]["total"]), 1.0)

    def test_push_edit_writes_only_planner_helper(self):
        ha = self.fake()
        live_before = [dict(b) for b in ha.live["saturday"]]
        vals = [18, 20, 19, 21, 19, 20, 16]
        new = planner.push_values(ha, self.cfg, self.plan, vals)
        self.assertEqual(new["chosen"], "Your edit")
        self.assertTrue(new["pushed"]["verified"])
        day = ha.helper[new["weekday"]]
        self.assertEqual([b["data"]["temperature"] for b in day], [18, 20, 19, 21, 19, 20, 16])
        self.assertEqual(ha.live["saturday"], live_before)
        self.assertEqual(data.load_json("plan_latest.json", None)["chosen"], "Your edit")
        planner.publish_sensors(ha, self.cfg, new)
        self.assertTrue(all(p.startswith("/api/states/sensor.heatplanner_") for p, _ in ha.posted))
        # restore for other tests
        data.save_json("plan_latest.json", self.plan)


if __name__ == "__main__":
    unittest.main()
