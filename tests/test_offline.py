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

    def keep_ended(self, vals, base):
        """Edits may only touch blocks that haven't ended yet (the demo plan is for today)."""
        import datetime as dt
        ended = planner.ended_blocks(self.cfg, dt.date.fromisoformat(self.plan["for_date"]))
        return [base[i] if i in ended else v for i, v in enumerate(vals)]

    def fake(self):
        ha = demo.FakeHA()
        planner.ensure_helper(ha, self.cfg)
        ha.posted = []
        ha.rest = lambda path, *a, **k: ha.posted.append((path, a[1] if len(a) > 1 else None))
        ha.state = lambda e: {"state": "on"}
        return ha

    def test_simulate_matches_stored_plan(self):
        for name in ("Optimised", "Current routine"):
            st = self.plan["strategies"][name]
            r = planner.simulate_values(self.cfg, self.plan, st["values"], st.get("offsets"))
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
        vals = self.keep_ended([18, 20, 19, 21, 19, 20, 16], [b["temperature"] for b in self.plan["blocks_raw"]])
        new = planner.push_values(ha, self.cfg, self.plan, vals)
        self.assertEqual(new["chosen"], "Your edit")
        self.assertTrue(new["pushed"]["verified"])
        day = ha.helper[new["weekday"]]
        self.assertEqual([b["data"]["temperature"] for b in day], vals)
        self.assertEqual(ha.live["saturday"], live_before)
        self.assertEqual(data.load_json("plan_latest.json", None)["chosen"], "Your edit")
        planner.publish_sensors(ha, self.cfg, new)
        self.assertTrue(all(p.startswith("/api/states/sensor.heatplanner_") for p, _ in ha.posted))
        # undo puts the optimiser's plan back and drops the edit
        back = planner.restore_plan(ha, self.cfg, data.load_json("plan_latest.json", None))
        self.assertEqual(back["chosen"], self.plan["chosen"])
        self.assertNotIn("Your edit", back["strategies"])
        self.assertEqual([b["data"]["temperature"] for b in ha.helper[back["weekday"]]], [b["temperature"] for b in self.plan["blocks"]])
        with self.assertRaises(ValueError):
            planner.restore_plan(ha, self.cfg, back)
        data.save_json("plan_latest.json", self.plan)

    def test_offsets_planned_pushed_and_bounded(self):
        opt = self.plan["strategies"]["Optimised"]
        self.assertEqual(len(opt["offsets"]), len(self.cfg["blocks"]))
        ref = self.plan["strategies"]["Setpoints only (offset unchanged, for reference)"]
        self.assertLessEqual(opt["total"], ref["total"] + 1e-9)          # tuning offsets can only help the score
        self.assertTrue(all(-5 <= o <= 5 for o in opt["offsets"]))
        with self.assertRaises(ValueError):
            planner.simulate_values(self.cfg, self.plan, opt["values"], [0, 0, -8, 0, 0, 0, 0])
        ha = self.fake()
        offs = self.keep_ended([-3, -3, -3, 0, 0, 0, 3], opt["offsets"])
        new = planner.push_values(ha, self.cfg, self.plan, opt["values"], offs)
        day = ha.helper[new["weekday"]]
        self.assertEqual(day[-1]["data"].get("lwt_offset"), 3)
        self.assertTrue(all("lwt_offset" in b["data"] for b in day))
        data.save_json("plan_latest.json", self.plan)

    def test_lower_offset_raises_cop_and_caps_output(self):
        from heatplanner import model
        p = model.DEFAULT_PARAMS
        self.assertGreater(model.cop_lwt(p, 5, model.lwt_curve(p, 5) - 3), model.cop_lwt(p, 5, model.lwt_curve(p, 5)))
        self.assertLess(model.emitter_max(p, 30, 20), model.emitter_max(p, 35, 20))

    def test_learn_fits_weather_curve_and_radiators(self):
        from heatplanner import learn, model
        rows = []
        for i in range(200):
            to = -2 + (i % 12)
            lwt = 38.0 - 0.6 * to
            room = 20.0
            q = 0.9 * (lwt - 2.5 - room)                        # true k_emit 0.9
            cop = model.cop_published(to, lwt)
            rows.append({"ts": "2026-11-%02dT%02d:%02d:00+00:00" % (1 + i // 48, (i % 48) // 2, 30 * (i % 2)),
                         "outdoor": to, "lwt": lwt, "room": room, "heating": 1.0, "heat_kwh": q / cop / 2, "cop": cop, "target": 20})
        new, notes, info = learn.fit(rows)
        self.assertLess(abs(new["lwc_b"] - (0.5 * -0.55 + 0.5 * -0.6)), 0.05)   # blended with prior at weight 50%
        self.assertGreater(new["k_emit_kw_per_k"], 0.9)                           # pulled from prior 1.0 toward 0.9..
        self.assertLess(new["k_emit_kw_per_k"], 1.0)

    def test_ended_blocks_rule(self):
        import datetime as dt
        d = dt.date(2026, 11, 21)
        at = lambda h, m=0: dt.datetime(2026, 11, 21, h, m)
        self.assertEqual(planner.ended_blocks(self.cfg, d, at(15, 30)), {0, 1, 2})     # 00-04, 04-07, 07-13 done
        self.assertEqual(planner.ended_blocks(self.cfg, d, at(13, 0)), {0, 1, 2})      # 13:00 exactly: 07-13 has ended
        self.assertEqual(planner.ended_blocks(self.cfg, d + dt.timedelta(days=1), at(23)), set())
        self.assertEqual(len(planner.ended_blocks(self.cfg, d - dt.timedelta(days=1), at(1))), 7)
        with self.assertRaises(ValueError):
            planner.check_unchanged(self.cfg, {0}, [18] * 7, [19] + [18] * 6)
        planner.check_unchanged(self.cfg, {0}, [18] * 7, [18] * 6 + [20])            # future change is fine

    def test_push_today_after_2200_leaves_tomorrow_alone(self):
        import copy, datetime as dt
        now = dt.datetime.now(planner.LOCAL).replace(tzinfo=None)
        plan = copy.deepcopy(self.plan)
        # pretend tonight's plan was made at 22:00 today, for tomorrow
        plan["sim"]["start"] = now.replace(hour=22, minute=0, second=0, microsecond=0).isoformat()
        plan["sim"]["tomorrow"] = plan["for_date"] = (now.date() + dt.timedelta(days=1)).isoformat()
        plan["weekday"] = planner.DAYS[(now.date() + dt.timedelta(days=1)).weekday()]
        self.assertTrue(planner.today_tail_available(plan))
        ha = self.fake()
        old_today = [b[2] for b in plan["sim"]["fixed"]]
        tv = list(old_today); tv[6] = 20.5                          # 22:00-24:00 never ends before midnight
        offs = [b[2] for b in plan["sim"]["fixed_off"]]; offs[6] = -2
        new, pushed = planner.push_today(ha, self.cfg, plan, tv, offs)
        self.assertTrue(pushed["verified"])
        today_name = planner.DAYS[now.weekday()]
        self.assertEqual(ha.helper[today_name][-1]["data"], {"temperature": 20.5, "lwt_offset": -2})
        self.assertEqual(new["blocks"], plan["blocks"])                 # tomorrow untouched
        self.assertEqual(new["sim"]["fixed"][6][2], 20.5)
        if now.hour >= 4:                                               # 00:00-04:00 has ended: refuse
            bad = list(tv); bad[0] = old_today[0] + 1
            with self.assertRaises(ValueError):
                planner.push_today(ha, self.cfg, new, bad, offs)
        with self.assertRaises(ValueError):                             # plan for today: use the normal editor
            planner.push_today(ha, self.cfg, self.plan, tv, offs)
        data.save_json("plan_latest.json", self.plan)

    def test_comfort_hours_and_grid_meter(self):
        for r in self.plan["strategies"].values():
            self.assertIn("hours_below", r)
        self.assertIn("current_grid_cost", self.plan["outlook"][0])
        import datetime as dt
        g0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        grid = [g0 + dt.timedelta(minutes=30 * i) for i in range(4)]
        ser = [(g0 + dt.timedelta(minutes=10 * i), 100 + 0.1 * i) for i in range(13)]
        inc = data._dense_increments(ser, grid)
        self.assertAlmostEqual(sum(inc.values()), 1.2, places=6)
        self.assertAlmostEqual(inc[grid[0]], 0.3, places=6)


if __name__ == "__main__":
    unittest.main()
