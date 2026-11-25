"""AI routine: a daily routine set by Hermes for today or tomorrow, always starting from the Optimised plan.

Hermes gets the Optimised base, evaluates candidate setpoints (score, electricity, comfort against the base
and your routine), then saves one with its reasoning, optionally applying it. Stored in ai_routines.json;
the dashboard shows it as "AI routine" with an AI badge."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import data, model, planner, revisions

LOCAL = ZoneInfo("Europe/London")
FILE = "ai_routines.json"
NAME = "AI routine"


def _today():
    return dt.datetime.now(LOCAL).date()


def load():
    t = _today().isoformat()
    return {k: v for k, v in data.load_json(FILE, {}).items() if k >= t}


def _save(store):
    data.save_json(FILE, store)


def allowed_dates():
    t = _today()
    return [t.isoformat(), (t + dt.timedelta(days=1)).isoformat()]


def _kind(plan, date_iso):
    if date_iso not in allowed_dates():
        raise ValueError("an AI routine can only be set for today or tomorrow (%s)" % ", ".join(allowed_dates()))
    return planner.day_kind(plan, date_iso)


def _view(cfg, plan, date_iso):
    kind = _kind(plan, date_iso)
    return kind, (plan if kind == "plan" else planner.day_view(cfg, plan, date_iso))


def base(cfg, plan, date_iso):
    """The starting point: that day's Optimised plan (for the rest of today after 22:00, today's plan in force)."""
    kind, v = _view(cfg, plan, date_iso)
    st = v["strategies"]
    b = st.get("Optimised") or st.get("Current plan") or st[v["chosen"]]
    rt = st["Current routine"]["total"]
    return {"date": date_iso, "kind": kind, "name": "Optimised" if "Optimised" in st else "Today's plan",
            "values": b["values"], "offsets": b.get("offsets"),
            "summary": _summ(b, rt), "routine": _summ(st["Current routine"], rt),
            "in_force": {"name": v["chosen"], "values": [x["temperature"] for x in v["blocks_raw"]],
                         "offsets": [x.get("lwt_offset") for x in v["blocks_raw"]] if "lwt_offset" in v["blocks_raw"][0] else None},
            "ended_blocks": sorted(planner.ended_blocks(cfg, dt.date.fromisoformat(date_iso))),
            "blocks": cfg["blocks"]}


def _summ(r, routine_total):
    return {"score": planner.score_points(r["total"], routine_total), "electricity": round(r["grid_cost"], 2),
            "hours_below": round(r["hours_below"], 2), "kwh": round(r["kwh_hp"], 1), "coldest": round(r["t_min_awake"], 1)}


def evaluate(cfg, plan, date_iso, values, offsets=None):
    b = base(cfg, plan, date_iso)
    res = planner.simulate_for_date(cfg, plan, date_iso, values, offsets)
    kind, v = _view(cfg, plan, date_iso)
    rt = v["strategies"]["Current routine"]["total"]
    out = {"date": date_iso, "values": res["values"], "offsets": res["offsets"], "summary": _summ(res, rt),
           "base": b["summary"], "routine": b["routine"], "blocks": res["blocks"]}
    out["vs_base"] = {"score": (out["summary"]["score"] or 0) - (b["summary"]["score"] or 0),
                      "electricity": round(out["summary"]["electricity"] - b["summary"]["electricity"], 2),
                      "hours_below": round(out["summary"]["hours_below"] - b["summary"]["hours_below"], 2)}
    return out


def set_routine(ha, cfg, plan, date_iso, values, offsets=None, reason="", apply=False, by="hermes"):
    """Save (and optionally apply) an AI routine, within the guard rails."""
    if not reason or len(reason.strip()) < 10:
        raise ValueError("a reason of a sentence or so is required; it is shown to the owner")
    b = base(cfg, plan, date_iso)
    values = planner.check_values(cfg, values)
    if offsets is not None and b["offsets"] is not None:
        offsets = planner.check_offsets(cfg, plan, offsets)
    else:
        offsets = b["offsets"]
    lim = (cfg.get("agent") or {}).get("ai_max_deviation_c", 3.0)
    for i, (x, y) in enumerate(zip(b["values"], values)):
        if abs(x - y) > lim + 1e-9:
            raise ValueError("%02d:00 block is %.1f° from the Optimised plan; the limit is %.1f°" % (cfg["blocks"][i][0], abs(x - y), lim))
    inf = b["in_force"]
    for i in b["ended_blocks"]:     # today: blocks that have ended keep what was in force
        if values[i] != inf["values"][i] or (offsets and inf["offsets"] and offsets[i] != inf["offsets"][i]):
            raise ValueError("%02d:00 block has already ended and can't be changed" % cfg["blocks"][i][0])
    ev = evaluate(cfg, plan, date_iso, values, offsets)
    store = load()
    rec = {"values": values, "offsets": offsets, "reason": reason.strip()[:600], "by": by,
           "at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "applied": False, "evaluation": ev["summary"],
           "base": ev["base"]}
    store[date_iso] = rec
    _save(store)
    pushed = None
    if apply:
        pushed = apply_routine(ha, cfg, plan, date_iso)
        rec = load()[date_iso]
    return {"routine": rec, "evaluation": ev, "pushed": pushed}


def apply_routine(ha, cfg, plan, date_iso):
    store = load()
    rec = store.get(date_iso)
    if not rec:
        raise ValueError("no AI routine saved for %s" % date_iso)
    kind = _kind(plan, date_iso)
    if kind == "plan":
        new = planner.push_values(ha, cfg, plan, rec["values"], rec["offsets"], by="hermes", reason="AI routine: " + rec["reason"],
                                  name=NAME)
        pushed = new["pushed"]
    elif kind == "today_tail":
        _, pushed = planner.push_today(ha, cfg, plan, rec["values"], rec["offsets"], by="hermes", reason="AI routine: " + rec["reason"])
    else:   # tomorrow, before the 22:00 run: write that weekday now; the 22:00 run keeps the AI routine (see build_plan)
        blocks = planner.plan_blocks(cfg, rec["values"], rec["offsets"])
        planner.validate(cfg, blocks)
        before = planner.day_view(cfg, plan, date_iso)
        revisions.record(cfg, date_iso, "hermes", [x["temperature"] for x in before["blocks_raw"]],
                         [x.get("lwt_offset") for x in before["blocks_raw"]] if rec["offsets"] is not None else None,
                         rec["values"], rec["offsets"], "AI routine: " + rec["reason"])
        pushed = planner.push_day(ha, cfg, planner.DAYS[dt.date.fromisoformat(date_iso).weekday()], blocks)
    rec["applied"] = True
    rec["applied_at"] = dt.datetime.now(LOCAL).isoformat(timespec="seconds")
    store[date_iso] = rec
    _save(store)
    return pushed


def clear(date_iso):
    store = load()
    if store.pop(date_iso, None) is None:
        raise ValueError("no AI routine for %s" % date_iso)
    _save(store)


def inject(cfg, plan, view, date_iso):
    """Add the AI routine for date_iso to a plan-shaped object (main plan or day view) for the dashboard."""
    rec = load().get(date_iso)
    if not rec or "strategies" not in view:
        return view
    try:
        res = planner.simulate_for_date(cfg, plan, date_iso, rec["values"], rec["offsets"])
    except Exception:
        return view
    view["strategies"][NAME] = {"values": res["values"], "offsets": res["offsets"],
                                **{k: res[k] for k in planner.SUMMARY_KEYS}, "reason": rec["reason"], "at": rec["at"],
                                "applied": rec.get("applied", False)}
    view["ai"] = {k: rec.get(k) for k in ("reason", "at", "applied", "applied_at", "by")}
    if rec.get("applied") and view.get("kind") in ("outlook", "today_tail") and view["chosen"] != NAME:
        tr = planner.full_trace_for_date(cfg, plan, date_iso, res["values"], res["offsets"])
        view.update({"chosen": NAME, "blocks": res["blocks"], "trace_best": tr,
                     "blocks_raw": planner.plan_blocks(cfg, res["values"], res["offsets"], raw=True),
                     "manual": {"at": rec.get("applied_at"), "previous": "Optimised", "by": "hermes", "reason": rec["reason"]}})
    return view
