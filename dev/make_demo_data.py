"""Build a realistic demo data folder with no Home Assistant, for working on the dashboard.

    python3 dev/make_demo_data.py demo-data
    HEATPLANNER_DATA=demo-data python3 -m heatplanner.server

A fake HA object answers the few calls build_plan() makes; the weather is synthetic (a cold
UK late-November spell). Nothing here talks to the network.
"""
import datetime as dt
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from heatplanner import data, model, planner  # noqa: E402

LIVE = [("00:00:00", "04:00:00", 18), ("04:00:00", "07:00:00", 20), ("07:00:00", "13:00:00", 18),
        ("13:00:00", "16:00:00", 20.5), ("16:00:00", "22:00:00", 19), ("22:00:00", "24:00:00", 18)]


class FakeHA:
    def __init__(self):
        day = [{"from": a, "to": b, "data": {"temperature": t}} for a, b, t in LIVE]
        self.live = {"id": "heat_pump_schedule_2", "name": "Heat pump schedule", **{d: day for d in planner.DAYS}}
        self.helper = None

    def rest(self, path, *a, **k):
        if path == "/api/config":
            return {"latitude": 52.6, "longitude": -1.1}
        return None

    def state(self, eid):
        return {"state": {"sensor.heat_pump_climate_control_room_temperature": "19.6",
                          "number.marstek_m1_max_discharge_power": "900"}.get(eid, "62"),
                "attributes": {"temperature": 0, "min_temp": -10, "max_temp": 10} if eid.endswith("leaving_water_offset") else {}}

    def schedule_list(self):
        return [self.live] + ([self.helper] if self.helper else [])

    def schedule_create(self, name):
        self.helper = {"id": "heat_pump_planner", "name": name}
        return self.helper

    def schedule_update(self, sid, fields):
        self.helper.update(fields)


def weather(start, days=9):
    rows = []
    t0 = dt.datetime.combine(start, dt.time(0))
    for h in range(days * 24):
        t = t0 + dt.timedelta(hours=h)
        d = h / 24
        mean = 4.5 - 2.0 * math.sin(d / 2.2) + random.uniform(-0.4, 0.4)
        temp = mean + 2.6 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        rad = max(0.0, 0.22 * math.sin((t.hour - 8) / 8.5 * math.pi)) * random.uniform(0.3, 1.0) if 8 <= t.hour <= 16 else 0.0
        rows.append((t, {"t": round(temp, 1), "rad": round(rad, 3), "wind": 4.0, "rh": 85}))
    return rows


def main(out):
    out = os.path.abspath(out)
    data.DATA_DIR = out
    os.makedirs(out, exist_ok=True)
    random.seed(4)
    now = dt.datetime.now(planner.LOCAL).replace(tzinfo=None)
    plan_now = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now.hour < 22:
        plan_now -= dt.timedelta(days=1)   # latest plan was made last night, for today
    wx = weather(plan_now.date() - dt.timedelta(days=1))
    cfg = data.load_config()
    params = data.load_params()
    params.update({"ua_kw_per_k": 0.283, "gains_kw": 1.42, "outdoor_bias_c": 0.58})
    ha = FakeHA()
    planner.ensure_helper(ha, cfg)
    plan = planner.build_plan(ha, cfg, params, now=plan_now, wx=wx)
    plan["pushed"] = planner.push(ha, cfg, plan)
    plan["created"] = plan_now.replace(minute=1).isoformat(timespec="seconds")
    plan["pushed"]["at"] = plan_now.replace(minute=2).isoformat(timespec="seconds")

    # earlier plans (index + one dated file for "yesterday" so the predicted-vs-actual view has something)
    idx = []
    for i in range(9, 0, -1):
        d = plan["for_date"]
        fd = (dt.date.fromisoformat(d) - dt.timedelta(days=i)).isoformat()
        idx.append({"for_date": fd, "created": fd + "T22:01:00", "chosen": "Optimised" if i % 4 else "Current routine",
                    "saving_gbp": round(random.uniform(0.08, 0.55), 3) if i % 4 else 0.0, "blocks": plan["blocks"],
                    "pushed": {"verified": True}, "forecast": plan["forecast_tomorrow"]})
    data.save_json("plans_index.json", idx)
    planner.save_plan(plan)
    y = (dt.date.fromisoformat(plan["for_date"]) - dt.timedelta(days=1)).isoformat()
    yplan = {k: v for k, v in plan.items() if k not in ("trace_best", "trace_current", "forecast", "sim")}
    yplan["for_date"] = y
    yplan["compare"] = json.loads(json.dumps(plan["compare"]).replace(plan["for_date"], y))
    data.save_json("plan_%s.json" % y, yplan)

    # observations for yesterday: the predicted curve plus noise
    rows = {}
    c = yplan["compare"][yplan["chosen"] if yplan["chosen"] in yplan["compare"] else "Optimised"]
    for t, room, el in zip(c["t"], c["room"], c["el"]):
        if t[:10] != y:
            continue
        lt = dt.datetime.fromisoformat(t).replace(tzinfo=planner.LOCAL)
        for half in (0, 30):
            ts = (lt + dt.timedelta(minutes=half)).astimezone(dt.timezone.utc)
            rows[ts.isoformat()] = {"ts": ts.isoformat(), "room": round(room + random.uniform(-0.35, 0.45) + 0.2, 2),
                                    "outdoor": 4.0, "target": 19.0, "heat_kwh": round(max(0, el / 2 * random.uniform(0.7, 1.4)), 2),
                                        "grid_kwh": round((el / 2 + 0.125) * (2.4 if lt.hour in (4, 5, 6, 13, 14, 15, 22, 23) else 0.3), 3)}
    data.save_json("obs.json", {"rows": rows})

    bt = []
    for i in range(9, 0, -1):
        d = (dt.date.fromisoformat(plan["for_date"]) - dt.timedelta(days=i)).isoformat()
        o = round(random.uniform(1.5, 8.5), 1)
        pred = round(max(3, 26 - 2.4 * o + random.uniform(-1, 1)), 1)
        bt.append({"date": d, "mean_outdoor": o, "pred_kwh": pred, "actual_kwh": round(pred + random.uniform(-4, 3.5), 1),
                   "room_rmse": round(random.uniform(0.3, 0.8), 2), "room_bias": round(random.uniform(-0.3, 0.3), 2)})
    hist = [{"at": (dt.date.fromisoformat(plan["for_date"]) - dt.timedelta(days=i)).isoformat() + "T03:30:00", "accepted": True,
             "params": {"ua_kw_per_k": round(0.30 - 0.002 * (9 - i) + random.uniform(-0.004, 0.004), 3), "gains_kw": 1.4,
                        "gains_solar": 2.0, "c_kwh_per_k": 9.8, "outdoor_bias_c": 0.58}, "days_used": 9 - i} for i in range(9, 0, -1)]
    data.save_json("params.json", {"params": params, "fitted_at": hist[-1]["at"],
                                   "info": {"days_used": 9, "heat_loss_r2": 0.39,
                                            "presence_hint": {"0": {"home_share": 0.18, "days": 2}, "1": {"home_share": 0.25, "days": 2},
                                                              "2": {"home_share": 0.92, "days": 1}}},
                                   "notes": ["heat-loss fit accepted (prior weight 55%)", "thermal-mass fit rejected (c=23.0 r2=0.00)",
                                             "fewer than 14 days: solar factor kept at default"],
                                   "backtest": bt, "history": hist})
    with open(data.path("log.jsonl"), "w") as f:
        for e in ({"event": "learn"}, {"event": "plan", "for_date": plan["for_date"], "chosen": plan["chosen"],
                                       "saving": plan["saving_vs_current_gbp"], "pushed": True}):
            f.write(json.dumps({"at": plan["created"], **e}) + "\n")
    print("demo data in", out, "plan for", plan["for_date"], plan["chosen"], plan["blocks"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-data")
