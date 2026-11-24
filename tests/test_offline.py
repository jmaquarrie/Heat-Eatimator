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


def json_copy(x):
    import json
    return json.loads(json.dumps(x))


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
        self.assertEqual([b["data"]["temperature"] for b in day], [b["temperature"] for b in planner.plan_blocks(self.cfg, vals, new["strategies"]["Your edit"]["offsets"])])
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

    def test_week_ahead_view_pin_and_undo(self):
        day = next(o["date"] for o in self.plan["outlook"] if "error" not in o)
        v = planner.day_view(self.cfg, self.plan, day)
        self.assertEqual(v["kind"], "outlook")
        self.assertEqual(v["chosen"], "Optimised")
        self.assertEqual(len(v["blocks_raw"]), len(self.cfg["blocks"]))
        self.assertTrue(v["trace_best"] and v["trace_current"])
        # simulating the preview's own values reproduces the view's numbers
        opt = v["strategies"]["Optimised"]
        r = planner.simulate_for_date(self.cfg, self.plan, day, opt["values"], opt["offsets"])
        self.assertAlmostEqual(r["total"], opt["total"], places=6)
        # pin an edit: written to that weekday now, and remembered for the nightly run
        ha = self.fake()
        vals = list(opt["values"]); vals[3] = 21.5
        pushed, blocks = planner.push_future(ha, self.cfg, self.plan, day, vals, opt["offsets"])
        self.assertTrue(pushed["verified"])
        import datetime as dt
        wd = planner.DAYS[dt.date.fromisoformat(day).weekday()]
        self.assertIn(21.5, [b["data"]["temperature"] for b in ha.helper[wd]])
        self.assertIn(day, planner.load_overrides())
        v2 = planner.day_view(self.cfg, self.plan, day)
        self.assertEqual(v2["chosen"], "Your edit")
        self.assertTrue(planner.annotate_outlook(self.cfg, json_copy(self.plan))["outlook"][[o.get("date") for o in self.plan["outlook"]].index(day)]["edited"])
        # undo puts the preview back and forgets the pin
        planner.restore_future(ha, self.cfg, self.plan, day)
        self.assertNotIn(day, planner.load_overrides())
        with self.assertRaises(ValueError):
            planner.day_view(self.cfg, self.plan, "2001-01-01")

    def test_nightly_keeps_a_pinned_edit(self):
        import datetime as dt
        tmp = tempfile.mkdtemp()
        now = dt.datetime.now(planner.LOCAL)
        plan_day = (now.date() if now.hour < 22 else now.date() + dt.timedelta(days=1)).isoformat()
        data.DATA_DIR = tmp
        pinned = [19.0, 20.0, 19.0, 21.0, 20.0, 19.5, 17.0]
        data.save_json("day_overrides.json", {plan_day: {"values": pinned, "offsets": [0, -1, -1, 0, 0, 0, 0], "at": "x"}})
        demo.main(tmp)
        p = data.load_json("plan_latest.json", None)
        self.assertEqual(p["chosen"], "Your edit")
        self.assertEqual([b["temperature"] for b in p["blocks_raw"]], pinned)
        self.assertTrue(p["manual"]["pinned"])
        self.assertIn("Optimised", p["strategies"])                      # still compared against the optimiser
        data.DATA_DIR = self.tmp

    def test_agent_projection_suggest_and_guard_rails(self):
        from heatplanner import agent, revisions
        ha = self.fake()
        ha.state = lambda e: {"state": {"sensor.heat_pump_climate_control_room_temperature": "19.2",
                                        "sensor.marstek_m1_battery_state_of_charge": "60"}.get(e, "0"), "attributes": {}}
        cfg = self.cfg
        p = planner.project_today(ha, cfg, self.plan) if hasattr(planner, "project_today") else agent.project_today(ha, cfg, self.plan)
        self.assertEqual(p["room_now"], 19.2)                         # starts from the measured room, not last night's guess
        self.assertTrue(p["trace"])
        sg = agent.suggest_today(ha, cfg, self.plan)
        self.assertGreaterEqual(sg["score_gain_gbp"], -1e-9)          # never worse than what's in force
        self.assertGreaterEqual(sg["score_gain_points"], 0)
        for i in sg["locked_blocks"]:                                 # ended blocks untouched
            self.assertEqual(sg["suggested"]["values"][i], sg["current"]["values"][i])
        with self.assertRaises(ValueError):                           # reason required
            agent.adjust(ha, cfg, self.plan, sg["suggested"]["values"], sg["suggested"]["offsets"], "")
        big = list(sg["current"]["values"]); big[6] = min(22, big[6] + 3) if big[6] <= 19 else big[6] - 3
        with self.assertRaises(ValueError):                           # more than 2 °C in one go
            agent.adjust(ha, cfg, self.plan, big, sg["current"]["offsets"], "testing a big jump")
        vals = list(sg["current"]["values"]); vals[6] = vals[6] + 0.5 if vals[6] < 22 else vals[6] - 0.5
        res = agent.adjust(ha, cfg, self.plan, vals, sg["current"]["offsets"], "House is running cool, warming the evening")
        self.assertEqual(res["revision"]["by"], "hermes")
        self.assertEqual(res["message"]["kind"], "update")
        self.assertIn("warming the evening", res["message"]["text"])
        with self.assertRaises(ValueError):                           # too soon after the last adjustment
            agent.adjust(ha, cfg, data.load_json("plan_latest.json", None), sg["current"]["values"], sg["current"]["offsets"], "and back again")
        self.assertTrue(any(r["by"] == "hermes" for r in revisions.for_date(self.plan["for_date"])))
        data.save_json("plan_latest.json", self.plan)

    def test_points_score(self):
        self.assertEqual(planner.score_points(5.0, 5.0), 100)          # routine = 100
        self.assertEqual(planner.score_points(4.0, 5.0), 120)          # 20% better
        self.assertEqual(planner.score_points(6.0, 5.0), 80)
        self.assertIsNone(planner.score_points(1.0, 0.0))
        idx = data.load_json("plans_index.json", [])
        self.assertIn("score", idx[-1])
        self.assertGreaterEqual(idx[-1]["score"], 100)                  # the chosen plan never scores below the routine

    def test_chat_channel(self):
        from heatplanner import agent
        m1 = agent.chat_add("you", "Can you warm the snug for 7pm?")
        agent.chat_add("hermes", "Will do.")
        self.assertEqual([m["from"] for m in agent.chat_since(m1["id"] - 1)], ["you", "hermes"])
        self.assertEqual(len(agent.chat_since(0, "you")), len([m for m in agent.chat_load()["messages"] if m["from"] == "you"]))
        agent.mark_read(m1["id"])
        self.assertEqual(agent.status_cursor(), m1["id"])

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
