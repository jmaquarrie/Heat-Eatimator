"""Build tomorrow's plan, compare strategies, and push it to the HA schedule helper."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import data, model

LOCAL = ZoneInfo("Europe/London")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def fmt(h):
    h = int(round(h * 60))
    return "%02d:%02d:00" % (h // 60, h % 60)


def blocks_to_helper(blocks):
    return [{"from": fmt(a), "to": fmt(b), "data": {"temperature": v}} for a, b, v in blocks]


def helper_to_values(cfg, blocks, default):
    """Sample a helper day's blocks at each planner block start -> list of setpoints."""
    vals = []
    for a, b in cfg["blocks"]:
        v = default
        for blk in blocks or []:
            f = _hours(blk["from"])
            t = _hours(blk["to"])
            if f <= a < t and blk.get("data", {}).get("temperature") is not None:
                v = float(blk["data"]["temperature"])
        vals.append(v)
    return vals


def _hours(s):
    p = s.split(":")
    return int(p[0]) + int(p[1]) / 60


DEFAULT_ROUTINE = [18, 20, 18, 20.5, 19, 19, 18]


def get_helper(ha, cfg):
    sid = cfg["schedule_entity"].split(".", 1)[1]
    for item in ha.schedule_list():
        if item["id"] == sid:
            return item
    return None


def live_day_values(ha, cfg, date):
    for item in ha.schedule_list():
        if item["id"] == cfg["live_schedule_id"]:
            return helper_to_values(cfg, item.get(DAYS[date.weekday()]), 18.0)
    return list(DEFAULT_ROUTINE)


def ensure_helper(ha, cfg):
    h = get_helper(ha, cfg)
    if h:
        return h, False
    created = ha.schedule_create(cfg["schedule_name"])
    sid = created["id"]
    seed = {d: [] for d in DAYS}
    vals = DEFAULT_ROUTINE
    live = None
    try:
        for item in ha.schedule_list():
            if item["id"] == cfg["live_schedule_id"]:
                live = item
    except Exception:
        pass
    for i, d in enumerate(DAYS):
        v = helper_to_values(cfg, live.get(d), 18.0) if live else vals
        seed[d] = blocks_to_helper(model.tidy(v, cfg))
    ha.schedule_update(sid, {"name": cfg["schedule_name"], "icon": "mdi:heat-pump", **seed})
    return get_helper(ha, cfg), True


def get_weather(ha):
    conf = ha.rest("/api/config")
    return data.fetch_weather(conf["latitude"], conf["longitude"], 0, 8, "Europe/London")


def make_ctx(ha, cfg, params, now=None, wx=None, room=None, soc=None):
    now = now or dt.datetime.now(LOCAL).replace(tzinfo=None)
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    tomorrow = start.date() + dt.timedelta(days=1)
    end = dt.datetime.combine(tomorrow + dt.timedelta(days=1), dt.time(4, 0))
    hours = (end - start).total_seconds() / 3600
    wx = wx or get_weather(ha)
    if room is None:
        room = float(ha.state(cfg["entities"]["room"])["state"])
    if soc is None:
        try:
            soc_pct = float(ha.state(cfg["entities"]["battery_soc"])["state"])
        except Exception:
            soc_pct = 50.0
        soc = soc_pct / 100 * cfg["battery"]["capacity_kwh"]
    ctx = {"start_dt": start, "hours": hours, "t_room0": room, "soc0_kwh": max(cfg["battery"]["reserve_kwh"], soc),
           "tomorrow": tomorrow, "terminal_ref_c": 19.5,
           "outdoor": data.series_fn(wx, "t", params.get("outdoor_bias_c", 0.0)),
           "radiation": data.series_fn(wx, "rad")}
    return ctx, [(t, v["t"]) for t, v in wx]


def strategies(cfg, ctx, current_vals):
    steady = [cfg["comfort"]["target_c"]] * len(cfg["blocks"])
    cosy_only = []
    for (a, b), v in zip(cfg["blocks"], current_vals):
        in_cosy = any(x <= a < y for x, y in cfg["tariff"]["cosy"])
        cosy_only.append(v if in_cosy else cfg["comfort"]["min_setpoint_c"])
    return {"Current routine": current_vals, "Steady comfort temp (battery covers gaps)": steady,
            "Cosy windows only (floors off-peak)": cosy_only}


def build_plan(ha, cfg, params, now=None, wx=None):
    wx = wx or get_weather(ha)
    ctx, fc = make_ctx(ha, cfg, params, now, wx=wx)
    tomorrow = ctx["tomorrow"]
    today = ctx["start_dt"].date()
    # today's remaining schedule from the planner helper (fallback: live routine)
    helper = get_helper(ha, cfg)
    if helper and helper.get(DAYS[today.weekday()]):
        today_blocks = helper[DAYS[today.weekday()]]
        fixed_vals = helper_to_values(cfg, today_blocks, 18.0)
    else:
        fixed_vals = live_day_values(ha, cfg, today)
    fixed = model.blocks_from_values(cfg, fixed_vals)
    current = live_day_values(ha, cfg, tomorrow)
    strat = strategies(cfg, ctx, current)
    results = {}
    for name, vals in strat.items():
        r = model.evaluate(cfg, params, ctx, fixed, vals)
        results[name] = {"values": vals, **{k: r[k] for k in ("total", "grid_cost", "penalty", "terminal", "kwh_hp", "kwh_by_slot", "t_min_awake", "t_end")}}
    starts = list(strat.values())
    best_vals, best_total = model.optimise(cfg, params, ctx, fixed, starts)
    r = model.evaluate(cfg, params, ctx, fixed, best_vals, trace=True)
    results["Optimised"] = {"values": best_vals, **{k: r[k] for k in ("total", "grid_cost", "penalty", "terminal", "kwh_hp", "kwh_by_slot", "t_min_awake", "t_end")}}
    nb = model.evaluate(cfg, params, ctx, fixed, best_vals, use_battery=False)
    results["Optimised (no battery, for reference)"] = {"values": best_vals, "total": nb["total"], "grid_cost": nb["grid_cost"],
                                                        "penalty": nb["penalty"], "terminal": nb["terminal"], "kwh_hp": nb["kwh_hp"],
                                                        "kwh_by_slot": nb["kwh_by_slot"], "t_min_awake": nb["t_min_awake"], "t_end": nb["t_end"]}
    cur_trace = model.evaluate(cfg, params, ctx, fixed, current, trace=True)["trace"]
    chosen_name = "Optimised"
    # stability rule: only deviate from the current routine if it saves at least 2p
    if results["Current routine"]["total"] - results["Optimised"]["total"] < 0.02:
        chosen_name = "Current routine"
    chosen_vals = results[chosen_name]["values"]
    tm = [(t, v) for t, v in fc if t.date() == tomorrow]
    compare = {}
    for name in ("Current routine", "Steady comfort temp (battery covers gaps)", "Cosy windows only (floors off-peak)", "Optimised"):
        rr = model.evaluate(cfg, params, ctx, fixed, results[name]["values"], trace=True)
        tr = rr["trace"][::4]
        compare[name] = {
            "t": [x["t"] for x in tr], "room": [x["room"] for x in tr], "target": [x["target"] for x in tr],
            "cum": [x["cum"] for x in tr], "el": [round(sum(y["el_kw"] for y in rr["trace"][i:i + 4]) * model.DT, 2) for i in range(0, len(rr["trace"]), 4)],
            "breakdown": {"cosy": rr["cost_slot"]["cosy"], "standard": rr["cost_slot"]["standard"], "peak": rr["cost_slot"]["peak"],
                          "penalty": rr["penalty"], "terminal": rr["terminal"], "battery": -rr["batt_value"]},
            "kwh_by_slot": rr["kwh_by_slot"]}
    plan = {
        "compare": compare,
        "created": dt.datetime.now(LOCAL).isoformat(timespec="seconds"),
        "for_date": tomorrow.isoformat(), "weekday": DAYS[tomorrow.weekday()],
        "chosen": chosen_name,
        "blocks": [{"from": a, "to": b, "temperature": v} for a, b, v in model.tidy(chosen_vals, cfg)],
        "blocks_raw": [{"from": a, "to": b, "temperature": v} for (a, b), v in zip(cfg["blocks"], chosen_vals)],
        "forecast_tomorrow": {"min": round(min(v for _, v in tm), 1), "max": round(max(v for _, v in tm), 1),
                              "mean": round(sum(v for _, v in tm) / len(tm), 1)},
        "start": ctx["start_dt"].isoformat(), "t_room0": ctx["t_room0"], "soc0_kwh": round(ctx["soc0_kwh"], 1),
        "fixed_today_tail": [{"from": a, "to": b, "temperature": v} for a, b, v in fixed if b > 20],
        "strategies": results,
        "saving_vs_current_gbp": round(results["Current routine"]["total"] - results[chosen_name]["total"], 3),
        "trace_best": r["trace"], "trace_current": cur_trace,
        "forecast": [{"t": t.isoformat(timespec="minutes"), "c": v} for t, v in fc],
        "params": {k: params[k] for k in ("ua_kw_per_k", "gains_kw", "c_kwh_per_k", "outdoor_bias_c")},
        "pushed": None,
        "occupancy": {"status_tomorrow": model.day_status(cfg, tomorrow),
                      "week": [{"date": (today + dt.timedelta(days=i)).isoformat(), "weekday": DAYS[(today + dt.timedelta(days=i)).weekday()],
                                "status": model.day_status(cfg, today + dt.timedelta(days=i))} for i in range(0, 8)]},
    }
    plan["outlook"] = build_outlook(ha, cfg, params, wx, ctx["start_dt"], chosen_vals)
    # everything needed to re-run the simulation later without Home Assistant (dashboard what-if editor)
    plan["sim"] = {"start": ctx["start_dt"].isoformat(), "hours": ctx["hours"], "t_room0": ctx["t_room0"],
                   "soc0_kwh": ctx["soc0_kwh"], "tomorrow": tomorrow.isoformat(), "terminal_ref_c": ctx["terminal_ref_c"],
                   "fixed": [list(b) for b in fixed], "current": current, "params": params,
                   "wx": [[t.isoformat(timespec="minutes"), v["t"], v.get("rad", 0.0)] for t, v in wx]}
    return plan


SUMMARY_KEYS = ("total", "grid_cost", "penalty", "terminal", "kwh_hp", "kwh_by_slot", "t_min_awake", "t_end")


def sim_context(cfg, plan):
    """Rebuild (ctx, fixed, params, current_values) for a saved plan, so values can be re-simulated offline."""
    s = plan.get("sim")
    if s:
        params = model.deep_merge(model.DEFAULT_PARAMS, s["params"])
        wx = [(dt.datetime.fromisoformat(t), {"t": c, "rad": r}) for t, c, r in s["wx"]]
        ctx = {"start_dt": dt.datetime.fromisoformat(s["start"]), "hours": s["hours"], "t_room0": s["t_room0"],
               "soc0_kwh": s["soc0_kwh"], "tomorrow": dt.date.fromisoformat(s["tomorrow"]),
               "terminal_ref_c": s.get("terminal_ref_c", 19.5),
               "outdoor": data.series_fn(wx, "t", params.get("outdoor_bias_c", 0.0)), "radiation": data.series_fn(wx, "rad")}
        return ctx, [tuple(b) for b in s["fixed"]], params, s.get("current") or plan["strategies"]["Current routine"]["values"]
    # plans written before the "sim" block existed: rebuild from what was stored (no solar term)
    params = model.deep_merge(data.load_params(), plan.get("params", {}))
    fc = [(dt.datetime.fromisoformat(x["t"]), x["c"]) for x in plan["forecast"]]
    start = dt.datetime.fromisoformat(plan["start"])
    end = dt.datetime.combine(dt.date.fromisoformat(plan["for_date"]) + dt.timedelta(days=1), dt.time(4, 0))
    ctx = {"start_dt": start, "hours": (end - start).total_seconds() / 3600, "t_room0": plan["t_room0"],
           "soc0_kwh": plan["soc0_kwh"], "tomorrow": dt.date.fromisoformat(plan["for_date"]), "terminal_ref_c": 19.5,
           "outdoor": data.series_fn(fc, None, params.get("outdoor_bias_c", 0.0))}
    fixed = [(b["from"], b["to"], b["temperature"]) for b in plan.get("fixed_today_tail", [])] or [(0, 24, 18.0)]
    return ctx, fixed, params, plan["strategies"]["Current routine"]["values"]


def check_values(cfg, values):
    c = cfg["comfort"]
    if not isinstance(values, list) or len(values) != len(cfg["blocks"]):
        raise ValueError("need %d setpoints" % len(cfg["blocks"]))
    out = []
    for v in values:
        v = round(float(v) * 2) / 2
        if not (c["min_setpoint_c"] <= v <= c["max_setpoint_c"]):
            raise ValueError("setpoints must be %s-%s C" % (c["min_setpoint_c"], c["max_setpoint_c"]))
        out.append(v)
    return out


def simulate_values(cfg, plan, values):
    """What-if: simulate the planned day with the given block setpoints (no HA access needed)."""
    values = check_values(cfg, values)
    ctx, fixed, params, _ = sim_context(cfg, plan)
    r = model.evaluate(cfg, params, ctx, fixed, values, trace=True)
    out = {k: r[k] for k in SUMMARY_KEYS}
    out.update({"values": values, "trace": [{k: x[k] for k in ("t", "room", "target", "el_kw", "soc", "cum")} for x in r["trace"]],
                "blocks": [{"from": a, "to": b, "temperature": v} for a, b, v in model.tidy(values, cfg)],
                "exact": "sim" in plan})
    return out


def push_values(ha, cfg, plan, values):
    """Push a hand-edited set of setpoints for the plan's day, and record it in the plan."""
    if dt.date.fromisoformat(plan["for_date"]) < dt.datetime.now(LOCAL).date():
        raise ValueError("the latest plan is for %s, which has passed; re-run the model first" % plan["for_date"])
    res = simulate_values(cfg, plan, values)
    plan = dict(plan)
    plan["blocks"] = res["blocks"]
    plan["blocks_raw"] = [{"from": a, "to": b, "temperature": v} for (a, b), v in zip(cfg["blocks"], res["values"])]
    plan["strategies"] = dict(plan["strategies"])
    plan["strategies"]["Your edit"] = {"values": res["values"], **{k: res[k] for k in SUMMARY_KEYS}}
    plan["chosen"] = "Your edit"
    plan["saving_vs_current_gbp"] = round(plan["strategies"]["Current routine"]["total"] - res["total"], 3)
    plan["manual"] = {"at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "values": res["values"], "trace": res["trace"]}
    plan["pushed"] = push(ha, cfg, plan)
    save_plan(plan)
    return plan


def publish_sensors(ha, cfg, plan=None, error=None):
    """Mirror the plan into HA as sensor.heatplanner_* states (dashboards/alerts in HA). Never touches schedules."""
    if not cfg.get("publish_sensors", True):
        return
    if error or not plan:
        ha.rest("/api/states/sensor.heatplanner_status", "POST", {"state": "error", "attributes": {
            "friendly_name": "Heat planner status", "icon": "mdi:alert", "error": str(error)[:250],
            "at": dt.datetime.now(LOCAL).isoformat(timespec="seconds")}}, timeout=10)
        return
    ch = plan["strategies"][plan["chosen"]]
    ps = plan.get("pushed") or {}
    blocks = " · ".join("%02d-%02d %.1f" % (b["from"], b["to"], b["temperature"]) for b in plan["blocks"])
    common = {"for_date": plan["for_date"], "strategy": plan["chosen"]}
    states = {
        "status": ("pushed" if ps.get("verified") else ("push_unverified" if ps else "preview"),
                   {"friendly_name": "Heat planner status", "icon": "mdi:heat-pump", "blocks": blocks,
                    "pushed_at": ps.get("at"), "created": plan["created"], **common}),
        "saving": (round(plan["saving_vs_current_gbp"], 2), {"friendly_name": "Heat planner saving vs routine",
                   "unit_of_measurement": "GBP", "device_class": "monetary", "icon": "mdi:piggy-bank", **common}),
        "cost": (round(ch["grid_cost"], 2), {"friendly_name": "Heat planner predicted grid cost", "unit_of_measurement": "GBP",
                 "device_class": "monetary", "icon": "mdi:cash", **common}),
        "kwh": (round(ch["kwh_hp"], 1), {"friendly_name": "Heat planner predicted pump electricity", "unit_of_measurement": "kWh",
                "icon": "mdi:flash", **common}),
        "min_temp": (round(ch["t_min_awake"], 1), {"friendly_name": "Heat planner lowest awake room temp",
                     "unit_of_measurement": "°C", "device_class": "temperature", **common}),
    }
    for k, (st, attrs) in states.items():
        ha.rest("/api/states/sensor.heatplanner_" + k, "POST", {"state": st, "attributes": attrs}, timeout=10)


def build_outlook(ha, cfg, params, wx, start, chosen_vals):
    """Preview of the optimiser for each of the next days (starting 22:00 the evening before)."""
    out = []
    steady = [cfg["comfort"]["target_c"]] * len(cfg["blocks"])
    base = start.date()
    for i in range(2, 8):
        day = base + dt.timedelta(days=i)
        s_dt = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(22, 0))
        try:
            ctx, _ = make_ctx(ha, cfg, params, now=s_dt, wx=wx, room=19.8, soc=cfg["battery"]["reserve_kwh"] + 3)
            fixed = model.blocks_from_values(cfg, steady)
            vals, total = model.optimise(cfg, params, ctx, fixed, [steady, [18, 20, 18, 20.5, 19, 19, 18]], passes=3)
            r = model.evaluate(cfg, params, ctx, fixed, vals)
            cur = model.evaluate(cfg, params, ctx, fixed, [18, 20, 18, 20.5, 19, 19, 18])
            tm = [v["t"] for t, v in wx if t.date() == day]
            out.append({"date": day.isoformat(), "weekday": DAYS[day.weekday()], "status": model.day_status(cfg, day),
                        "forecast_mean": round(sum(tm) / len(tm), 1) if tm else None,
                        "blocks": [{"from": a, "to": b, "temperature": v} for a, b, v in model.tidy(vals, cfg)],
                        "grid_cost": round(r["grid_cost"], 2), "penalty": round(r["penalty"], 2), "total": round(r["total"], 2),
                        "current_total": round(cur["total"], 2), "kwh_hp": round(r["kwh_hp"], 1)})
        except Exception as e:  # forecast may not cover the day
            out.append({"date": day.isoformat(), "error": str(e)})
    return out


def validate(cfg, blocks):
    c = cfg["comfort"]
    if not blocks or blocks[0]["from"] != 0 or blocks[-1]["to"] != 24:
        raise ValueError("blocks do not cover 00:00-24:00")
    for i, b in enumerate(blocks):
        if not (c["min_setpoint_c"] <= b["temperature"] <= c["max_setpoint_c"]):
            raise ValueError("setpoint out of range: %s" % b)
        if i and blocks[i - 1]["to"] != b["from"]:
            raise ValueError("gap/overlap in blocks")


def push(ha, cfg, plan):
    validate(cfg, plan["blocks"])
    helper, created = ensure_helper(ha, cfg)
    day = plan["weekday"]
    new = {k: v for k, v in helper.items() if k in DAYS}
    new[day] = blocks_to_helper([(b["from"], b["to"], b["temperature"]) for b in plan["blocks"]])
    ha.schedule_update(helper["id"], {"name": helper.get("name", cfg["schedule_name"]), "icon": helper.get("icon", "mdi:heat-pump"), **{d: new.get(d, []) for d in DAYS}})
    back = get_helper(ha, cfg)
    ok = back and back.get(day) == new[day]
    st = ha.state(cfg["schedule_entity"])
    return {"helper_created": created, "verified": bool(ok), "entity": cfg["schedule_entity"], "state": st["state"],
            "at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "day": day}


def save_plan(plan):
    data.save_json("plan_latest.json", plan)
    data.save_json("plan_%s.json" % plan["for_date"], {k: v for k, v in plan.items() if k not in ("trace_best", "trace_current", "forecast")})
    idx = data.load_json("plans_index.json", [])
    idx = [i for i in idx if i["for_date"] != plan["for_date"]]
    idx.append({"for_date": plan["for_date"], "created": plan["created"], "chosen": plan["chosen"],
                "saving_gbp": plan["saving_vs_current_gbp"], "blocks": plan["blocks"], "pushed": plan["pushed"],
                "forecast": plan["forecast_tomorrow"]})
    data.save_json("plans_index.json", idx[-120:])
