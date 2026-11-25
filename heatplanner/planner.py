"""Build tomorrow's plan, compare strategies, and push it to the HA schedule helper."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import data, model, revisions

LOCAL = ZoneInfo("Europe/London")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def fmt(h):
    h = int(round(h * 60))
    return "%02d:%02d:00" % (h // 60, h % 60)


def blocks_to_helper(blocks):
    """(a, b, T) or (a, b, T, offset) tuples -> helper blocks. The offset rides along as block data."""
    out = []
    for blk in blocks:
        d = {"temperature": blk[2]}
        if len(blk) > 3 and blk[3] is not None:
            d["lwt_offset"] = blk[3]
        out.append({"from": fmt(blk[0]), "to": fmt(blk[1]), "data": d})
    return out


def helper_to_values(cfg, blocks, default, key="temperature"):
    """Sample a helper day's blocks at each planner block start -> list of values for one data key."""
    vals = []
    for a, b in cfg["blocks"]:
        v = default
        for blk in blocks or []:
            f = _hours(blk["from"])
            t = _hours(blk["to"])
            if f <= a < t and blk.get("data", {}).get(key) is not None:
                v = float(blk["data"][key])
        vals.append(v)
    return vals


def score_points(total, routine_total):
    """Plan score in points: your routine = 100; each 1% cheaper (model total: electricity + priced comfort
    shortfall + end-of-day heat) is +1. Higher is better. None if the routine total is too small to compare."""
    if routine_total is None or routine_total <= 0.1:
        return None
    return round(100 + 100 * (routine_total - total) / routine_total)


def offsets_on(cfg):
    return bool(cfg.get("lwt_offset", {}).get("enabled"))


def _num(v):
    v = round(float(v), 2)
    return int(v) if v == int(v) else v


def plan_blocks(cfg, values, offsets=None, raw=False):
    """Blocks for the plan JSON / helper: merged neighbours unless raw."""
    if offsets is not None:
        offsets = [_num(o) for o in offsets]
    if raw:
        rows = [(a, b, v) + ((offsets[i],) if offsets is not None else ()) for i, ((a, b), v) in enumerate(zip(cfg["blocks"], values))]
    else:
        rows = model.tidy(values, cfg, offsets)
    return [dict({"from": r[0], "to": r[1], "temperature": r[2]}, **({"lwt_offset": r[3]} if len(r) > 3 else {})) for r in rows]


def current_offset(ha, cfg):
    """(current offset, allowed min, allowed max) from the offset climate entity; falls back to (0, cfg range)."""
    o = cfg.get("lwt_offset", {})
    lo, hi = o.get("min", -5), o.get("max", 5)
    try:
        st = ha.state(cfg["entities"]["lwt_offset"])
        a = st.get("attributes", {})
        cur = float(a.get("temperature", 0) or 0)
        if a.get("min_temp") is not None:
            lo = max(lo, float(a["min_temp"]))
        if a.get("max_temp") is not None:
            hi = min(hi, float(a["max_temp"]))
        return cur, lo, hi
    except Exception:
        return 0.0, lo, hi


def _hours(s):
    p = s.split(":")
    return int(p[0]) + int(p[1]) / 60


DEFAULT_ROUTINE = [18, 20, 18, 20.5, 19, 19, 18]
SUMMARY_KEYS = ("total", "grid_cost", "penalty", "terminal", "kwh_hp", "kwh_by_slot", "t_min_awake", "t_end", "hours_below", "writes")


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
           "tomorrow": tomorrow, "terminal_ref_c": cfg["comfort"].get("end_of_day_c", 19.5),
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


def battery_limits(ha, cfg):
    """Read the battery's live discharge limit (W) so the model doesn't assume more cover than it gets."""
    bat = dict(cfg["battery"])
    ent = bat.get("discharge_limit_entity")
    if ent:
        try:
            w = float(ha.state(ent)["state"])
            if w > 0:
                bat["max_discharge_kw"] = round(min(bat["max_kw"], w / 1000.0), 3)
                bat["discharge_limit_source"] = ent
        except Exception:
            pass
    return bat


def build_plan(ha, cfg, params, now=None, wx=None):
    wx = wx or get_weather(ha)
    cfg = dict(cfg, battery=battery_limits(ha, cfg))
    ctx, fc = make_ctx(ha, cfg, params, now, wx=wx)
    tomorrow = ctx["tomorrow"]
    today = ctx["start_dt"].date()
    use_off = offsets_on(cfg)
    cur_off, off_lo, off_hi = current_offset(ha, cfg) if use_off else (0.0, 0, 0)
    ocands = model.offset_candidates(cfg, off_lo, off_hi) if use_off else []
    # today's remaining schedule from the planner helper (fallback: live routine)
    helper = get_helper(ha, cfg)
    if helper and helper.get(DAYS[today.weekday()]):
        today_blocks = helper[DAYS[today.weekday()]]
        fixed_vals = helper_to_values(cfg, today_blocks, 18.0)
        fixed_offs = helper_to_values(cfg, today_blocks, cur_off, key="lwt_offset")
    else:
        fixed_vals = live_day_values(ha, cfg, today)
        fixed_offs = [cur_off] * len(cfg["blocks"])
    fixed = model.blocks_from_values(cfg, fixed_vals)
    fixed_off = model.blocks_from_values(cfg, fixed_offs) if use_off else None
    current = live_day_values(ha, cfg, tomorrow)
    cur_off = _num(cur_off)
    cur_offs = [cur_off] * len(cfg["blocks"]) if use_off else None

    def ev(vals, offs, **kw):
        return model.evaluate(cfg, params, ctx, fixed, vals, offsets=offs, fixed_off=fixed_off, **kw)

    def summary(vals, offs, r):
        return {"values": vals, "offsets": offs, **{k: r[k] for k in SUMMARY_KEYS}}

    strat = strategies(cfg, ctx, current)
    results = {}
    for name, vals in strat.items():
        results[name] = summary(vals, cur_offs, ev(vals, cur_offs))
    # 1) setpoints only (offset left where it is), 2) then setpoints and offsets together
    passes = cfg.get("optimiser", {}).get("passes", 4)
    sp_vals, _, _ = model.optimise(cfg, params, ctx, fixed, list(strat.values()), offsets=cur_offs, fixed_off=fixed_off, passes=passes)
    if use_off:
        best_vals, best_offs, _ = model.optimise(cfg, params, ctx, fixed, [sp_vals], offsets=cur_offs, fixed_off=fixed_off,
                                                 tune_offsets=True, offset_cands=ocands, passes=passes)
        results["Setpoints only (offset unchanged, for reference)"] = summary(sp_vals, cur_offs, ev(sp_vals, cur_offs))
    else:
        best_vals, best_offs = sp_vals, None
    r = ev(best_vals, best_offs, trace=True)
    results["Optimised"] = summary(best_vals, best_offs, r)
    nb = ev(best_vals, best_offs, use_battery=False)
    results["Optimised (no battery, for reference)"] = summary(best_vals, best_offs, nb)
    cur_trace = ev(current, cur_offs, trace=True)["trace"]
    chosen_name = "Optimised"
    # stability rule: only move away from your routine if the optimised plan scores clearly better
    rt = results["Current routine"]["total"]
    gain = 100 * (rt - results["Optimised"]["total"]) / rt if rt > 0.1 else (rt - results["Optimised"]["total"]) * 100
    if gain < cfg.get("optimiser", {}).get("min_gain_points", 0.5):
        chosen_name = "Current routine"
    # a day the owner edited in advance (week ahead) keeps their edit instead of being re-optimised
    pinned = load_overrides().get(tomorrow.isoformat())
    manual = None
    if pinned:
        p_vals = check_values(cfg, pinned["values"])
        p_offs = None
        if use_off:
            p_offs = [round(float(o)) for o in (pinned.get("offsets") or cur_offs)]
        results["Your edit"] = summary(p_vals, p_offs, ev(p_vals, p_offs))
        manual = {"at": pinned.get("at"), "values": p_vals, "previous": chosen_name, "pinned": True}
        chosen_name = "Your edit"
    from . import ai as ai_mod                      # set by Hermes for this day (based on the Optimised plan)
    air = ai_mod.load().get(tomorrow.isoformat())
    if air:
        a_vals = check_values(cfg, air["values"])
        a_offs = [round(float(o)) for o in (air.get("offsets") or cur_offs)] if use_off else None
        results["AI routine"] = dict(summary(a_vals, a_offs, ev(a_vals, a_offs)), reason=air.get("reason"), at=air.get("at"),
                                     applied=air.get("applied", False))
        if air.get("applied") and not pinned:
            manual = {"at": air.get("applied_at") or air.get("at"), "values": a_vals, "previous": chosen_name, "by": "hermes",
                      "reason": air.get("reason")}
            chosen_name = "AI routine"
    chosen_vals, chosen_offs = results[chosen_name]["values"], results[chosen_name]["offsets"]
    tm = [(t, v) for t, v in fc if t.date() == tomorrow]
    compare = {}
    for name in ("Current routine", "Steady comfort temp (battery covers gaps)", "Cosy windows only (floors off-peak)", "Optimised"):
        rr = ev(results[name]["values"], results[name]["offsets"], trace=True)
        tr = rr["trace"][::4]
        compare[name] = {
            "t": [x["t"] for x in tr], "room": [x["room"] for x in tr], "target": [x["target"] for x in tr],
            "lwt": [x["lwt"] for x in tr],
            "cum": [x["cum"] for x in tr], "el": [round(sum(y["el_kw"] for y in rr["trace"][i:i + 4]) * model.DT, 2) for i in range(0, len(rr["trace"]), 4)],
            "breakdown": {"cosy": rr["cost_slot"]["cosy"], "standard": rr["cost_slot"]["standard"], "peak": rr["cost_slot"]["peak"],
                          "penalty": rr["penalty"], "terminal": rr["terminal"], "battery": -rr["batt_value"]},
            "kwh_by_slot": rr["kwh_by_slot"]}
    plan = {
        "compare": compare,
        "created": dt.datetime.now(LOCAL).isoformat(timespec="seconds"),
        "for_date": tomorrow.isoformat(), "weekday": DAYS[tomorrow.weekday()],
        "chosen": chosen_name,
        "blocks": plan_blocks(cfg, chosen_vals, chosen_offs),
        "blocks_raw": plan_blocks(cfg, chosen_vals, chosen_offs, raw=True),
        "forecast_tomorrow": {"min": round(min(v for _, v in tm), 1), "max": round(max(v for _, v in tm), 1),
                              "mean": round(sum(v for _, v in tm) / len(tm), 1)},
        "start": ctx["start_dt"].isoformat(), "t_room0": ctx["t_room0"], "soc0_kwh": round(ctx["soc0_kwh"], 1),
        "fixed_today_tail": [{"from": a, "to": b, "temperature": v} for a, b, v in fixed if b > 20],
        "strategies": results,
        "saving_vs_current_gbp": round(results["Current routine"]["total"] - results[chosen_name]["total"], 3),
        "trace_best": r["trace"], "trace_current": cur_trace,
        "forecast": [{"t": t.isoformat(timespec="minutes"), "c": v} for t, v in fc],
        "params": {k: params[k] for k in ("ua_kw_per_k", "gains_kw", "c_kwh_per_k", "outdoor_bias_c")},
        "battery": {k: cfg["battery"].get(k) for k in ("capacity_kwh", "reserve_kwh", "max_charge_kw", "max_discharge_kw", "discharge_limit_source")},
        "lwt_offset": {"enabled": use_off, "current": cur_off, "min": off_lo, "max": off_hi} if use_off else {"enabled": False},
        "pushed": None,
        "occupancy": {"status_tomorrow": model.day_status(cfg, tomorrow),
                      "week": [{"date": (today + dt.timedelta(days=i)).isoformat(), "weekday": DAYS[(today + dt.timedelta(days=i)).weekday()],
                                "status": model.day_status(cfg, today + dt.timedelta(days=i))} for i in range(0, 8)]},
    }
    plan["original_blocks_raw"] = plan["blocks_raw"]          # as planned at 22:00, before any edits through the day
    if manual:
        plan["manual"] = manual
    plan["outlook"] = build_outlook(ha, cfg, params, wx, ctx["start_dt"], chosen_vals, cur_off if use_off else None, ocands)
    # everything needed to re-run the simulation later without Home Assistant (dashboard what-if editor)
    plan["sim"] = {"start": ctx["start_dt"].isoformat(), "hours": ctx["hours"], "t_room0": ctx["t_room0"],
                   "soc0_kwh": ctx["soc0_kwh"], "tomorrow": tomorrow.isoformat(), "terminal_ref_c": ctx["terminal_ref_c"],
                   "fixed": [list(b) for b in fixed], "current": current, "params": params,
                   "fixed_off": [list(b) for b in fixed_off] if use_off else None, "current_off": cur_offs,
                   "wx": [[t.isoformat(timespec="minutes"), v["t"], v.get("rad", 0.0)] for t, v in wx]}
    return plan


def sim_context(cfg, plan):
    """Rebuild (ctx, fixed, params, current_values, fixed_off) for a saved plan, so values can be re-simulated
    offline. Uses the battery limits the plan was made with (cfg["battery"] is updated in place)."""
    if plan.get("battery"):
        cfg["battery"] = dict(cfg["battery"], **{k: v for k, v in plan["battery"].items() if v is not None})
    s = plan.get("sim")
    if s:
        params = model.deep_merge(model.DEFAULT_PARAMS, s["params"])
        wx = [(dt.datetime.fromisoformat(t), {"t": c, "rad": r}) for t, c, r in s["wx"]]
        ctx = {"start_dt": dt.datetime.fromisoformat(s["start"]), "hours": s["hours"], "t_room0": s["t_room0"],
               "soc0_kwh": s["soc0_kwh"], "tomorrow": dt.date.fromisoformat(s["tomorrow"]),
               "terminal_ref_c": s.get("terminal_ref_c", 19.5),
               "outdoor": data.series_fn(wx, "t", params.get("outdoor_bias_c", 0.0)), "radiation": data.series_fn(wx, "rad")}
        fo = [tuple(b) for b in s["fixed_off"]] if s.get("fixed_off") else None
        return ctx, [tuple(b) for b in s["fixed"]], params, s.get("current") or plan["strategies"]["Current routine"]["values"], fo
    # plans written before the "sim" block existed: rebuild from what was stored (no solar term)
    params = model.deep_merge(data.load_params(), plan.get("params", {}))
    fc = [(dt.datetime.fromisoformat(x["t"]), x["c"]) for x in plan["forecast"]]
    start = dt.datetime.fromisoformat(plan["start"])
    end = dt.datetime.combine(dt.date.fromisoformat(plan["for_date"]) + dt.timedelta(days=1), dt.time(4, 0))
    ctx = {"start_dt": start, "hours": (end - start).total_seconds() / 3600, "t_room0": plan["t_room0"],
           "soc0_kwh": plan["soc0_kwh"], "tomorrow": dt.date.fromisoformat(plan["for_date"]), "terminal_ref_c": cfg["comfort"].get("end_of_day_c", 19.5),
           "outdoor": data.series_fn(fc, None, params.get("outdoor_bias_c", 0.0))}
    fixed = [(b["from"], b["to"], b["temperature"]) for b in plan.get("fixed_today_tail", [])] or [(0, 24, 18.0)]
    return ctx, fixed, params, plan["strategies"]["Current routine"]["values"], None


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


def check_offsets(cfg, plan, offsets):
    lo = plan.get("lwt_offset", {}).get("min", cfg["lwt_offset"]["min"])
    hi = plan.get("lwt_offset", {}).get("max", cfg["lwt_offset"]["max"])
    if not isinstance(offsets, list) or len(offsets) != len(cfg["blocks"]):
        raise ValueError("need %d offsets" % len(cfg["blocks"]))
    out = []
    for v in offsets:
        v = round(float(v))
        if not (lo <= v <= hi):
            raise ValueError("leaving-water offsets must be %g to %g" % (lo, hi))
        out.append(v)
    return out


def plan_offsets(plan):
    """The chosen plan's per-block offsets, or None when the plan has no offset planning."""
    raw = plan.get("blocks_raw") or []
    if raw and "lwt_offset" in raw[0]:
        return [b["lwt_offset"] for b in raw]
    return None


def ended_blocks(cfg, date, now=None):
    """Indices of cfg blocks that have already finished on `date` (all of them for a past date)."""
    now = now or dt.datetime.now(LOCAL).replace(tzinfo=None)
    if date > now.date():
        return set()
    if date < now.date():
        return set(range(len(cfg["blocks"])))
    h = now.hour + now.minute / 60
    return {i for i, (a, b) in enumerate(cfg["blocks"]) if b <= h}


def check_unchanged(cfg, ended, old_vals, new_vals, old_offs=None, new_offs=None):
    for i in sorted(ended):
        a, b = cfg["blocks"][i]
        if abs(old_vals[i] - new_vals[i]) > 1e-9 or (old_offs is not None and new_offs is not None and old_offs[i] != new_offs[i]):
            raise ValueError("%02d:00-%02d:00 has already ended and can't be changed" % (a, b))


def today_tail_available(plan):
    """True when the plan is for tomorrow and was made today, so the rest of today is held in sim.fixed."""
    s = plan.get("sim") or {}
    today = dt.datetime.now(LOCAL).date().isoformat()
    return bool(s.get("fixed")) and s.get("start", "")[:10] == today and plan["for_date"] != today


def simulate_values(cfg, plan, values, offsets=None, today_values=None, today_offsets=None):
    """What-if: simulate the planned day with the given block setpoints (and offsets). No HA access needed.
    today_values / today_offsets replace the rest of today (the start-day blocks) instead."""
    values = check_values(cfg, values)
    ctx, fixed, params, _, fixed_off = sim_context(cfg, plan)
    if fixed_off is not None:
        offsets = check_offsets(cfg, plan, offsets) if offsets is not None else (plan_offsets(plan) or [0] * len(values))
    else:
        offsets = None
    if today_values is not None:
        fixed = model.blocks_from_values(cfg, check_values(cfg, today_values))
        if fixed_off is not None and today_offsets is not None:
            fixed_off = model.blocks_from_values(cfg, check_offsets(cfg, plan, today_offsets))
    r = model.evaluate(cfg, params, ctx, fixed, values, trace=True, offsets=offsets, fixed_off=fixed_off)
    out = {k: r[k] for k in SUMMARY_KEYS}
    out.update({"values": values, "offsets": offsets,
                "trace": [{k: x[k] for k in ("t", "room", "target", "el_kw", "soc", "cum", "lwt", "off")} for x in r["trace"]],
                "blocks": plan_blocks(cfg, values, offsets), "exact": "sim" in plan})
    return out


def full_trace(cfg, plan, values, offsets, today_values=None, today_offsets=None):
    """Full simulation trace (all fields the charts use) for the plan window with these values."""
    ctx, fixed, params, _, fixed_off = sim_context(cfg, plan)
    if today_values is not None:
        fixed = model.blocks_from_values(cfg, today_values)
        if fixed_off is not None and today_offsets is not None:
            fixed_off = model.blocks_from_values(cfg, today_offsets)
    return model.evaluate(cfg, params, ctx, fixed, values, trace=True, offsets=offsets if fixed_off is not None else None,
                          fixed_off=fixed_off)["trace"]


EDITS = ("Your edit", "AI routine")


def full_trace_for_date(cfg, plan, date_iso, values, offsets=None):
    kind = day_kind(plan, date_iso)
    if kind == "plan":
        return full_trace(cfg, plan, values, offsets)
    if kind == "today_tail":
        return full_trace(cfg, plan, [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan), values, offsets)
    ctx, fixed, fixed_off, params, cur, _ = outlook_ctx(cfg, plan, date_iso)
    return model.evaluate(cfg, params, ctx, fixed, values, trace=True, offsets=offsets if fixed_off is not None else None,
                          fixed_off=fixed_off)["trace"]


def push_values(ha, cfg, plan, values, offsets=None, by="you", reason=None, name="Your edit"):
    """Push a hand-edited set of setpoints (and offsets) for the plan's day, and record it in the plan."""
    if dt.date.fromisoformat(plan["for_date"]) < dt.datetime.now(LOCAL).date():
        raise ValueError("the latest plan is for %s, which has passed; re-run the model first" % plan["for_date"])
    res = simulate_values(cfg, plan, values, offsets)
    # blocks of the plan's day that have already ended (when the plan is for today) can't change
    check_unchanged(cfg, ended_blocks(cfg, dt.date.fromisoformat(plan["for_date"])), [b["temperature"] for b in plan["blocks_raw"]],
                    res["values"], plan_offsets(plan), res["offsets"])
    previous = (plan.get("manual") or {}).get("previous") if plan["chosen"] in EDITS else plan["chosen"]
    plan = dict(plan)
    plan["blocks"] = res["blocks"]
    plan["strategies"] = dict(plan["strategies"])
    plan["strategies"][name] = {"values": res["values"], "offsets": res["offsets"], **{k: res[k] for k in SUMMARY_KEYS}}
    if name == "AI routine":
        plan["strategies"][name]["reason"] = reason
    plan["chosen"] = name
    plan["saving_vs_current_gbp"] = round(plan["strategies"]["Current routine"]["total"] - res["total"], 3)
    before_v, before_o = [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan)
    plan["blocks_raw"] = plan_blocks(cfg, res["values"], res["offsets"], raw=True)
    plan["manual"] = {"at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "values": res["values"], "trace": res["trace"],
                      "previous": previous, "by": by, "reason": reason}
    plan["trace_best"] = full_trace(cfg, plan, res["values"], res["offsets"])   # the chart now shows what's in force
    plan["pushed"] = push(ha, cfg, plan)
    save_plan(plan)
    revisions.record(cfg, plan["for_date"], by, before_v, before_o, res["values"], res["offsets"], reason)
    return plan


def restore_plan(ha, cfg, plan, by="you"):
    """Undo a hand edit: push the strategy the planner had chosen before, and drop the edit."""
    prev = (plan.get("manual") or {}).get("previous")
    if plan["chosen"] not in EDITS or not prev or prev not in plan["strategies"]:
        raise ValueError("there is no hand edit to undo")
    plan = dict(plan)
    vals, offs = plan["strategies"][prev]["values"], plan["strategies"][prev].get("offsets")
    revisions.record(cfg, plan["for_date"], by, [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan), vals, offs,
                     "Undo: back to the planner's choice", kind="undo")
    plan["blocks"] = plan_blocks(cfg, vals, offs)
    plan["blocks_raw"] = plan_blocks(cfg, vals, offs, raw=True)
    plan["trace_best"] = full_trace(cfg, plan, vals, offs)
    plan["strategies"] = {k: v for k, v in plan["strategies"].items() if k != "Your edit"}
    plan["chosen"] = prev
    plan["saving_vs_current_gbp"] = round(plan["strategies"]["Current routine"]["total"] - plan["strategies"][prev]["total"], 3)
    plan.pop("manual", None)
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
    blocks = " · ".join("%02d-%02d %.1f" % (b["from"], b["to"], b["temperature"]) + (" (%+g)" % b["lwt_offset"] if "lwt_offset" in b else "")
                        for b in plan["blocks"])
    common = {"for_date": plan["for_date"], "strategy": plan["chosen"]}
    states = {
        "status": ("pushed" if ps.get("verified") else ("push_unverified" if ps else "preview"),
                   {"friendly_name": "Heat planner status", "icon": "mdi:heat-pump", "blocks": blocks,
                    "pushed_at": ps.get("at"), "created": plan["created"], **common}),
        "saving": (round(plan["strategies"]["Current routine"]["grid_cost"] - ch["grid_cost"], 2),
                   {"friendly_name": "Heat planner electricity saving vs routine", "unit_of_measurement": "GBP",
                    "device_class": "monetary", "icon": "mdi:piggy-bank", **common}),
        "score": (score_points(ch["total"], plan["strategies"]["Current routine"]["total"]),
                  {"friendly_name": "Heat planner score (routine = 100)", "unit_of_measurement": "pts", "icon": "mdi:scoreboard", **common}),
        "cost": (round(ch["grid_cost"], 2), {"friendly_name": "Heat planner predicted grid cost", "unit_of_measurement": "GBP",
                 "device_class": "monetary", "icon": "mdi:cash", **common}),
        "kwh": (round(ch["kwh_hp"], 1), {"friendly_name": "Heat planner predicted pump electricity", "unit_of_measurement": "kWh",
                "icon": "mdi:flash", **common}),
        "min_temp": (round(ch["t_min_awake"], 1), {"friendly_name": "Heat planner lowest awake room temp",
                     "unit_of_measurement": "°C", "device_class": "temperature", **common}),
    }
    for k, (st, attrs) in states.items():
        ha.rest("/api/states/sensor.heatplanner_" + k, "POST", {"state": st, "attributes": attrs}, timeout=10)


def build_outlook(ha, cfg, params, wx, start, chosen_vals, cur_off=None, ocands=None):
    """Preview of the optimiser for each of the next days (starting 22:00 the evening before)."""
    out = []
    steady = [cfg["comfort"]["target_c"]] * len(cfg["blocks"])
    routine = [18, 20, 18, 20.5, 19, 19, 18]
    base = start.date()
    use_off = cur_off is not None
    offs0 = [cur_off] * len(cfg["blocks"]) if use_off else None
    for i in range(2, 8):
        day = base + dt.timedelta(days=i)
        s_dt = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(22, 0))
        try:
            ol = cfg.get("outlook") or {}
            ctx, _ = make_ctx(ha, cfg, params, now=s_dt, wx=wx, room=ol.get("start_room_c", 19.8),
                              soc=ol.get("start_battery_kwh", cfg["battery"]["reserve_kwh"] + 3))
            fixed = model.blocks_from_values(cfg, steady)
            fixed_off = model.blocks_from_values(cfg, offs0) if use_off else None
            vals, offs, _ = model.optimise(cfg, params, ctx, fixed, [steady], passes=3, offsets=offs0, fixed_off=fixed_off,
                                           tune_offsets=use_off, offset_cands=ocands, offset_starts=2)
            r = model.evaluate(cfg, params, ctx, fixed, vals, offsets=offs, fixed_off=fixed_off)
            cur = model.evaluate(cfg, params, ctx, fixed, routine, offsets=offs0, fixed_off=fixed_off)
            tm = [v["t"] for t, v in wx if t.date() == day]
            out.append({"date": day.isoformat(), "weekday": DAYS[day.weekday()], "status": model.day_status(cfg, day),
                        "forecast_mean": round(sum(tm) / len(tm), 1) if tm else None,
                        "blocks": plan_blocks(cfg, vals, offs),
                        "grid_cost": round(r["grid_cost"], 2), "penalty": round(r["penalty"], 2), "total": round(r["total"], 2),
                        "current_total": round(cur["total"], 2), "current_grid_cost": round(cur["grid_cost"], 2),
                        "hours_below": round(r["hours_below"], 2), "current_hours_below": round(cur["hours_below"], 2),
                        "kwh_hp": round(r["kwh_hp"], 1)})
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
        if "lwt_offset" in b and not (-10 <= b["lwt_offset"] <= 10):
            raise ValueError("leaving-water offset out of range: %s" % b)
        if i and blocks[i - 1]["to"] != b["from"]:
            raise ValueError("gap/overlap in blocks")


# ------------------------------------------------------------------ any day: today, plan day, week ahead
OVERRIDES = "day_overrides.json"


def load_overrides():
    """Hand edits pinned to future dates: {date: {values, offsets, at}}. Past dates are dropped."""
    today = dt.datetime.now(LOCAL).date().isoformat()
    return {k: v for k, v in data.load_json(OVERRIDES, {}).items() if k >= today}


def save_overrides(ov):
    data.save_json(OVERRIDES, ov)


def _sample_blocks(cfg, blocks, key, default):
    out = []
    for a, b in cfg["blocks"]:
        v = default
        for blk in blocks:
            if blk["from"] <= a < blk["to"] and blk.get(key) is not None:
                v = blk[key]
        out.append(v)
    return out


def day_kind(plan, date_iso):
    today = dt.datetime.now(LOCAL).date().isoformat()
    if date_iso == plan["for_date"]:
        return "plan"
    if date_iso == today and today_tail_available(plan):
        return "today_tail"
    if plan.get("sim") and any(o.get("date") == date_iso and "error" not in o for o in plan.get("outlook") or []):
        if date_iso < today:
            raise ValueError("%s has passed" % date_iso)
        return "outlook"
    raise ValueError("no plan for %s" % date_iso)


def outlook_ctx(cfg, plan, date_iso):
    """Rebuild the week-ahead simulation context for one day (same assumptions as build_outlook)."""
    s = plan["sim"]
    params = model.deep_merge(model.DEFAULT_PARAMS, s["params"])
    wx = [(dt.datetime.fromisoformat(t), {"t": c, "rad": r}) for t, c, r in s["wx"]]
    day = dt.date.fromisoformat(date_iso)
    start = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(22, 0))
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(4, 0))
    ol = cfg.get("outlook") or {}
    ctx = {"start_dt": start, "hours": (end - start).total_seconds() / 3600, "t_room0": ol.get("start_room_c", 19.8),
           "soc0_kwh": max(cfg["battery"]["reserve_kwh"], ol.get("start_battery_kwh", cfg["battery"]["reserve_kwh"] + 3)),
           "tomorrow": day, "terminal_ref_c": cfg["comfort"].get("end_of_day_c", 19.5),
           "outdoor": data.series_fn(wx, "t", params.get("outdoor_bias_c", 0.0)), "radiation": data.series_fn(wx, "rad")}
    steady = [cfg["comfort"]["target_c"]] * len(cfg["blocks"])
    fixed = model.blocks_from_values(cfg, steady)
    use_off = bool(s.get("fixed_off"))
    cur = (plan.get("lwt_offset") or {}).get("current", 0) if use_off else None
    fixed_off = model.blocks_from_values(cfg, [cur] * len(cfg["blocks"])) if use_off else None
    tm = [v["t"] for t, v in wx if t.date() == day]
    fc = {"min": round(min(tm), 1), "max": round(max(tm), 1), "mean": round(sum(tm) / len(tm), 1)} if tm else {"min": 0, "max": 0, "mean": 0}
    return ctx, fixed, fixed_off, params, cur, fc


def _sim_out(cfg, r, values, offsets, exact=True):
    out = {k: r[k] for k in SUMMARY_KEYS}
    out.update({"values": values, "offsets": offsets,
                "trace": [{k: x[k] for k in ("t", "room", "target", "el_kw", "soc", "cum", "lwt", "off")} for x in r["trace"]],
                "blocks": plan_blocks(cfg, values, offsets), "exact": exact})
    return out


def day_view(cfg, plan, date_iso):
    """A plan-shaped object for any editable day, so the dashboard can show and edit it like the main plan."""
    kind = day_kind(plan, date_iso)
    if kind == "plan":
        return {"kind": "plan", "for_date": date_iso}
    routine = (plan.get("sim") or {}).get("current") or plan["strategies"]["Current routine"]["values"]
    if kind == "today_tail":
        ctx, fixed, params, _, fixed_off = sim_context(cfg, plan)
        vals = [b[2] for b in fixed]
        offs = [b[2] for b in fixed_off] if fixed_off else None
        pv, po = [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan)
        r = model.evaluate(cfg, params, ctx, fixed, pv, trace=True, offsets=po, fixed_off=fixed_off)
        rr = model.evaluate(cfg, params, ctx, model.blocks_from_values(cfg, routine), pv, trace=True, offsets=po,
                            fixed_off=model.blocks_from_values(cfg, [offs[0] if offs else 0] * len(routine)) if offs else None)
        strategies = {"Current plan": {"values": vals, "offsets": offs, **{k: r[k] for k in SUMMARY_KEYS}},
                      "Current routine": {"values": routine, "offsets": [offs[0]] * len(routine) if offs else None, **{k: rr[k] for k in SUMMARY_KEYS}}}
        today = dt.date.fromisoformat(date_iso)
        fc = plan["forecast_tomorrow"]
        return {"kind": kind, "for_date": date_iso, "chosen": "Current plan", "strategies": strategies,
                "blocks": plan_blocks(cfg, vals, offs), "blocks_raw": plan_blocks(cfg, vals, offs, raw=True),
                "trace_best": r["trace"], "trace_current": rr["trace"], "start": plan["start"], "t_room0": plan["t_room0"],
                "soc0_kwh": plan["soc0_kwh"], "forecast_tomorrow": fc, "occupancy": {"status_tomorrow": model.day_status(cfg, today)},
                "lwt_offset": plan.get("lwt_offset"), "battery": plan.get("battery"), "pushed": plan.get("today_edit"), "manual": None,
                "note": "The rest of today. Tomorrow's plan is left as it is."}
    # week ahead
    ctx, fixed, fixed_off, params, cur, fc = outlook_ctx(cfg, plan, date_iso)
    o = next(x for x in plan["outlook"] if x.get("date") == date_iso)
    use_off = fixed_off is not None
    ov = load_overrides().get(date_iso)
    opt_v = _sample_blocks(cfg, o["blocks"], "temperature", cfg["comfort"]["target_c"])
    opt_o = [round(x) for x in _sample_blocks(cfg, o["blocks"], "lwt_offset", cur)] if use_off else None
    cur_o = [cur] * len(routine) if use_off else None
    ev = lambda v, of: model.evaluate(cfg, params, ctx, fixed, v, trace=True, offsets=of, fixed_off=fixed_off)
    r_opt, r_cur = ev(opt_v, opt_o), ev(routine, cur_o)
    strategies = {"Optimised": {"values": opt_v, "offsets": opt_o, **{k: r_opt[k] for k in SUMMARY_KEYS}},
                  "Current routine": {"values": routine, "offsets": cur_o, **{k: r_cur[k] for k in SUMMARY_KEYS}}}
    chosen, best = "Optimised", r_opt
    manual = None
    if ov:
        ev_v = check_values(cfg, ov["values"])
        ev_o = [round(float(x)) for x in (ov.get("offsets") or opt_o)] if use_off else None
        r_ed = ev(ev_v, ev_o)
        strategies["Your edit"] = {"values": ev_v, "offsets": ev_o, **{k: r_ed[k] for k in SUMMARY_KEYS}}
        chosen, best = "Your edit", r_ed
        manual = {"at": ov.get("at"), "previous": "Optimised", "pinned": True}
    sv, so = strategies[chosen]["values"], strategies[chosen]["offsets"]
    return {"kind": kind, "for_date": date_iso, "chosen": chosen, "strategies": strategies,
            "blocks": plan_blocks(cfg, sv, so), "blocks_raw": plan_blocks(cfg, sv, so, raw=True),
            "trace_best": best["trace"], "trace_current": r_cur["trace"], "start": ctx["start_dt"].isoformat(),
            "t_room0": ctx["t_room0"], "soc0_kwh": ctx["soc0_kwh"], "forecast_tomorrow": fc,
            "occupancy": {"status_tomorrow": model.day_status(cfg, dt.date.fromisoformat(date_iso))},
            "lwt_offset": plan.get("lwt_offset"), "battery": plan.get("battery"),
            "pushed": (ov or {}).get("pushed"), "manual": manual,
            "note": "Week-ahead preview: assumes the room is 19.8° at 22:00 the night before. The planner re-plans this day at 22:00 the night before, and keeps a pinned edit if you push one."}


def simulate_for_date(cfg, plan, date_iso, values, offsets=None):
    kind = day_kind(plan, date_iso)
    if kind == "plan":
        return simulate_values(cfg, plan, values, offsets)
    if kind == "today_tail":
        return simulate_values(cfg, plan, [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan),
                               today_values=values, today_offsets=offsets)
    ctx, fixed, fixed_off, params, cur, _ = outlook_ctx(cfg, plan, date_iso)
    values = check_values(cfg, values)
    offsets = (check_offsets(cfg, plan, offsets) if offsets is not None else [cur] * len(values)) if fixed_off is not None else None
    r = model.evaluate(cfg, params, ctx, fixed, values, trace=True, offsets=offsets, fixed_off=fixed_off)
    return _sim_out(cfg, r, values, offsets)


def push_future(ha, cfg, plan, date_iso, values, offsets=None, by="you", reason=None):
    """Pin a hand edit to a week-ahead day and write that weekday in the helper now."""
    if day_kind(plan, date_iso) != "outlook":
        raise ValueError("not a week-ahead day")
    before = day_view(cfg, plan, date_iso)
    res = simulate_for_date(cfg, plan, date_iso, values, offsets)
    blocks = res["blocks"]
    validate(cfg, blocks)
    revisions.record(cfg, date_iso, by, [b["temperature"] for b in before["blocks_raw"]],
                     [b.get("lwt_offset") for b in before["blocks_raw"]] if res["offsets"] is not None else None,
                     res["values"], res["offsets"], reason)
    day = dt.date.fromisoformat(date_iso)
    pushed = push_day(ha, cfg, DAYS[day.weekday()], blocks)
    ov = load_overrides()
    ov[date_iso] = {"values": res["values"], "offsets": res["offsets"], "at": pushed["at"], "pushed": pushed}
    save_overrides(ov)
    return pushed, blocks


def restore_future(ha, cfg, plan, date_iso):
    """Drop a pinned edit for a week-ahead day and write the planner's preview back to that weekday."""
    ov = load_overrides()
    if date_iso not in ov:
        raise ValueError("there is no edit to undo for %s" % date_iso)
    old = ov.pop(date_iso)
    save_overrides(ov)
    o0 = next(x for x in plan["outlook"] if x.get("date") == date_iso)
    revisions.record(cfg, date_iso, "you", old["values"], old.get("offsets"),
                     _sample_blocks(cfg, o0["blocks"], "temperature", 20.0),
                     _sample_blocks(cfg, o0["blocks"], "lwt_offset", 0) if old.get("offsets") is not None else None,
                     "Undo: back to the planner's preview", kind="undo")
    o = next(x for x in plan["outlook"] if x.get("date") == date_iso)
    day = dt.date.fromisoformat(date_iso)
    pushed = push_day(ha, cfg, DAYS[day.weekday()], o["blocks"])
    return pushed, o["blocks"]


def annotate_outlook(cfg, plan):
    """Mark week-ahead days that carry a pinned edit (for the dashboard's week cards)."""
    ov = load_overrides()
    for o in plan.get("outlook") or []:
        if o.get("date") in ov and "error" not in o:
            try:
                v = day_view(cfg, plan, o["date"])
                ed = v["strategies"]["Your edit"]
                o.update({"edited": True, "blocks": v["blocks"], "grid_cost": round(ed["grid_cost"], 2),
                          "total": round(ed["total"], 2), "hours_below": round(ed["hours_below"], 2), "kwh_hp": round(ed["kwh_hp"], 1)})
            except Exception:
                pass
    return plan


def push_today(ha, cfg, plan, values, offsets=None, by="you", reason=None):
    """After 22:00 the plan is for tomorrow; this changes the rest of TODAY in the helper (blocks not yet
    ended), leaving tomorrow's plan as it is. Records the change in the plan's sim block."""
    if not today_tail_available(plan):
        raise ValueError("today is the plan's own day: edit it in the normal editor")
    s = plan["sim"]
    old_vals = [b[2] for b in s["fixed"]]
    old_offs = [b[2] for b in s["fixed_off"]] if s.get("fixed_off") else None
    res = simulate_values(cfg, plan, [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan),
                          today_values=values, today_offsets=offsets if old_offs is not None else None)
    new_vals = check_values(cfg, values)
    new_offs = check_offsets(cfg, plan, offsets) if (old_offs is not None and offsets is not None) else old_offs
    today = dt.datetime.now(LOCAL).date()
    check_unchanged(cfg, ended_blocks(cfg, today), old_vals, new_vals, old_offs, new_offs)
    blocks = plan_blocks(cfg, new_vals, new_offs)
    validate(cfg, blocks)
    pushed = push_day(ha, cfg, DAYS[today.weekday()], blocks)
    revisions.record(cfg, today.isoformat(), by, old_vals, old_offs, new_vals, new_offs, reason)
    plan = dict(plan)
    plan["sim"] = dict(s, fixed=[[a, b, v] for (a, b), v in zip(cfg["blocks"], new_vals)],
                       fixed_off=[[a, b, o] for (a, b), o in zip(cfg["blocks"], new_offs)] if new_offs is not None else s.get("fixed_off"))
    plan["fixed_today_tail"] = [{"from": a, "to": b, "temperature": v} for (a, b), v in zip(cfg["blocks"], new_vals) if b > 20]
    plan["trace_best"] = full_trace(cfg, plan, [b["temperature"] for b in plan["blocks_raw"]], plan_offsets(plan))
    plan["today_edit"] = {"at": pushed["at"], "date": today.isoformat(), "blocks": blocks, "verified": pushed["verified"],
                          "total": res["total"], "grid_cost": res["grid_cost"]}
    save_plan(plan)
    return plan, pushed


def push(ha, cfg, plan):
    validate(cfg, plan["blocks"])
    return push_day(ha, cfg, plan["weekday"], plan["blocks"])


def push_day(ha, cfg, day, blocks):
    """Write one weekday of the planner helper (all seven days are sent, as the websocket API needs) and verify."""
    helper, created = ensure_helper(ha, cfg)
    new = {k: v for k, v in helper.items() if k in DAYS}
    new[day] = blocks_to_helper([(b["from"], b["to"], b["temperature"], b.get("lwt_offset")) for b in blocks])
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
    st = plan.get("strategies") or {}
    idx.append({"for_date": plan["for_date"], "created": plan["created"], "chosen": plan["chosen"],
                "saving_gbp": plan["saving_vs_current_gbp"],
                "score": score_points(st.get(plan["chosen"], {}).get("total", 0), st.get("Current routine", {}).get("total")),
                "electricity_vs_routine_gbp": round(st.get("Current routine", {}).get("grid_cost", 0) - st.get(plan["chosen"], {}).get("grid_cost", 0), 2) if st else None, "blocks": plan["blocks"], "pushed": plan["pushed"],
                "forecast": plan["forecast_tomorrow"]})
    data.save_json("plans_index.json", idx[-120:])
