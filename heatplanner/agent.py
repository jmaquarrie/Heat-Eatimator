"""Hooks for an external agent (Hermes): live projection of today, re-optimised suggestions, guarded
adjustments, and a small chat channel shared with the dashboard.

Hermes never invents setpoints on its own maths: it asks the planner for a projection or a suggestion,
decides, and applies through `adjust()`, which enforces the guard rails below and records a revision and a
written update that the owner sees on the dashboard.
"""
import datetime as dt
import json
import threading
import urllib.request
from zoneinfo import ZoneInfo

from . import data, model, planner, revisions

LOCAL = ZoneInfo("Europe/London")
CHAT = "chat.json"
STATE = "agent_state.json"
DEFAULTS = {
    "max_adjustments_per_day": 8,      # agent pushes per calendar day
    "min_minutes_between": 20,         # between agent pushes
    "max_setpoint_change_c": 2.0,      # vs the values in force when it adjusts
    "webhook_url": "",                 # optional: POSTed on each chat message and at check-in times
    "checkin_times": [],               # e.g. ["07:00", "12:30", "16:30"]: POST {"type": "checkin"} to webhook_url
}


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("agent") or {}))


def now_local():
    return dt.datetime.now(LOCAL).replace(tzinfo=None)


# ------------------------------------------------------------------ chat
def chat_load():
    return data.load_json(CHAT, {"messages": []})


def chat_add(sender, text, kind="message", meta=None):
    store = chat_load()
    msgs = store["messages"]
    m = {"id": (msgs[-1]["id"] + 1) if msgs else 1, "at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"),
         "from": sender, "kind": kind, "text": str(text)[:4000]}
    if meta:
        m["meta"] = meta
    msgs.append(m)
    store["messages"] = msgs[-500:]
    data.save_json(CHAT, store)
    return m


def chat_since(since=0, sender=None):
    return [m for m in chat_load()["messages"] if m["id"] > since and (sender is None or m["from"] == sender)]


def touch(what="seen"):
    st = data.load_json(STATE, {})
    st["last_" + what] = dt.datetime.now(LOCAL).isoformat(timespec="seconds")
    data.save_json(STATE, st)


def status():
    st = data.load_json(STATE, {})
    return {"last_seen": st.get("last_seen"), "last_adjust": st.get("last_adjust")}


# ------------------------------------------------------------------ today from now
def _today_parts(cfg, plan):
    """(today values, today offsets, tomorrow values, tomorrow offsets, source) for the current state."""
    now = now_local()
    today, tomorrow = now.date().isoformat(), (now.date() + dt.timedelta(days=1)).isoformat()
    offs_on = planner.plan_offsets(plan) is not None
    if plan["for_date"] == today:
        tv, to = [b["temperature"] for b in plan["blocks_raw"]], planner.plan_offsets(plan)
        o = next((x for x in plan.get("outlook") or [] if x.get("date") == tomorrow and "error" not in x), None)
        if o:
            nv = planner._sample_blocks(cfg, o["blocks"], "temperature", cfg["comfort"]["target_c"])
            no = [round(x) for x in planner._sample_blocks(cfg, o["blocks"], "lwt_offset", 0)] if offs_on else None
        else:
            nv, no = list(tv), to
        return tv, to, nv, no, "plan"
    if planner.today_tail_available(plan):
        s = plan["sim"]
        tv = [b[2] for b in s["fixed"]]
        to = [b[2] for b in s["fixed_off"]] if s.get("fixed_off") else None
        return tv, to, [b["temperature"] for b in plan["blocks_raw"]], planner.plan_offsets(plan), "today_tail"
    raise ValueError("no plan covers today yet; re-run the model")


def live_ctx(ha, cfg, plan, room=None, soc_kwh=None):
    """Simulation context starting NOW from the measured room temperature and battery level, using the
    forecast stored with the plan (no new weather fetch)."""
    s = plan["sim"]
    params = model.deep_merge(model.DEFAULT_PARAMS, s["params"])
    wx = [(dt.datetime.fromisoformat(t), {"t": c, "rad": r}) for t, c, r in s["wx"]]
    now = now_local()
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    tomorrow = start.date() + dt.timedelta(days=1)
    end = dt.datetime.combine(tomorrow + dt.timedelta(days=1), dt.time(4, 0))
    if room is None:
        try:
            room = float(ha.state(cfg["entities"]["room"])["state"])
        except Exception:
            room = None
    if soc_kwh is None:
        try:
            soc_kwh = float(ha.state(cfg["entities"]["battery_soc"])["state"]) / 100 * cfg["battery"]["capacity_kwh"]
        except Exception:
            soc_kwh = cfg["battery"]["capacity_kwh"] / 2
    predicted_now = None
    for r in plan.get("trace_best") or []:
        if r["t"] <= start.strftime("%Y-%m-%dT%H:%M"):
            predicted_now = r["room"]
    ctx = {"start_dt": start, "hours": (end - start).total_seconds() / 3600,
           "t_room0": room if room is not None else (predicted_now or 19.5),
           "soc0_kwh": max(cfg["battery"]["reserve_kwh"], soc_kwh), "tomorrow": tomorrow, "terminal_ref_c": 19.5,
           "outdoor": data.series_fn(wx, "t", params.get("outdoor_bias_c", 0.0)), "radiation": data.series_fn(wx, "rad")}
    return ctx, params, room, predicted_now


def _summ(r):
    keys = ("grid_cost", "penalty", "total", "kwh_hp", "t_min_awake", "hours_below", "t_end")
    return {k: round(r[k], 3) for k in keys}


def project_today(ha, cfg, plan, values=None, offsets=None, trace=True):
    """Simulate the rest of today (plus tomorrow) from the measured state now."""
    tv, to, nv, no, source = _today_parts(cfg, plan)
    if values is not None:
        tv = planner.check_values(cfg, values)
    if offsets is not None and to is not None:
        to = planner.check_offsets(cfg, plan, offsets)
    ctx, params, room, pred = live_ctx(ha, cfg, plan)
    r = model.evaluate(cfg, params, ctx, model.blocks_from_values(cfg, tv), nv, trace=trace,
                       offsets=no, fixed_off=model.blocks_from_values(cfg, to) if to is not None else None)
    out = {"from": ctx["start_dt"].isoformat(), "room_now": room, "predicted_now": pred, "source": source,
           "values": tv, "offsets": to, "summary": _summ(r)}
    if trace:
        out["trace"] = [{k: x[k] for k in ("t", "room", "target", "el_kw", "soc", "lwt", "off")}
                        for x in r["trace"] if x["t"][:10] == ctx["start_dt"].date().isoformat()]
    return out


def suggest_today(ha, cfg, plan, passes=3):
    """Re-optimise the blocks of today that haven't ended, from the measured state now."""
    tv, to, nv, no, source = _today_parts(cfg, plan)
    ctx, params, room, pred = live_ctx(ha, cfg, plan)
    today = ctx["start_dt"].date()
    locked = planner.ended_blocks(cfg, today, ctx["start_dt"])
    cands = cfg["optimiser"]["candidates"]
    lo, hi = (plan.get("lwt_offset") or {}).get("min", -5), (plan.get("lwt_offset") or {}).get("max", 5)
    ocands = model.offset_candidates(cfg, lo, hi) if to is not None else []

    def ev(v, o):
        return model.evaluate(cfg, params, ctx, model.blocks_from_values(cfg, v), nv, offsets=no,
                              fixed_off=model.blocks_from_values(cfg, o) if o is not None else None)

    base = ev(tv, to)
    vals, offs = list(tv), list(to) if to is not None else None
    best = base["total"]
    free = [i for i in range(len(cfg["blocks"])) if i not in locked]
    for _ in range(passes):
        improved = False
        for i in free:
            for c in cands:
                if c != vals[i]:
                    t = vals[:i] + [c] + vals[i + 1:]
                    s_ = ev(t, offs)["total"]
                    if s_ < best - 1e-6:
                        best, vals, improved = s_, t, True
            for c in ocands:
                if offs is not None and c != offs[i]:
                    t = offs[:i] + [c] + offs[i + 1:]
                    s_ = ev(vals, t)["total"]
                    if s_ < best - 1e-6:
                        best, offs, improved = s_, t, True
        if offs is not None:   # run moves over the free blocks
            for a in range(len(free)):
                for b in range(a + 1, len(free)):
                    idx = free[a:b + 1]
                    for c in ocands:
                        t = [c if k in idx else o for k, o in enumerate(offs)]
                        if t != offs:
                            s_ = ev(vals, t)["total"]
                            if s_ < best - 1e-6:
                                best, offs, improved = s_, t, True
        if not improved:
            break
    new = ev(vals, offs)
    return {"from": ctx["start_dt"].isoformat(), "room_now": room, "predicted_now": pred, "source": source,
            "locked_blocks": sorted(locked),
            "current": {"values": tv, "offsets": to, "summary": _summ(base)},
            "suggested": {"values": vals, "offsets": offs, "summary": _summ(new),
                          "changes": revisions.describe(cfg, {"changed": [i for i in free if vals[i] != tv[i] or (offs and offs[i] != to[i])],
                                                               "before": revisions._raw(cfg, tv, to), "after": revisions._raw(cfg, vals, offs)})},
            "electricity_saving_gbp": round(base["grid_cost"] - new["grid_cost"], 3),
            "score_gain_gbp": round(base["total"] - new["total"], 3)}


# ------------------------------------------------------------------ guarded adjustment
def adjust(ha, cfg, plan, values, offsets=None, reason="", by="hermes", post_update=True):
    """Apply new setpoints/offsets to the rest of today, within the guard rails. Records a revision and
    (by default) posts a written update to the chat."""
    if not reason or len(reason.strip()) < 5:
        raise ValueError("a reason is required (it is shown to the owner)")
    g = settings(cfg)
    tv, to, _, _, source = _today_parts(cfg, plan)
    values = planner.check_values(cfg, values)
    if offsets is not None and to is not None:
        offsets = planner.check_offsets(cfg, plan, offsets)
    else:
        offsets = to
    for i, (a, b) in enumerate(zip(tv, values)):
        if abs(a - b) > g["max_setpoint_change_c"] + 1e-9:
            raise ValueError("%02d:00 block: change of %.1f° exceeds the %.1f° limit for automatic adjustments"
                             % (cfg["blocks"][i][0], abs(a - b), g["max_setpoint_change_c"]))
    today = now_local().date().isoformat()
    mine = [r for r in revisions.for_date(today) if r["by"] == by]
    if len(mine) >= g["max_adjustments_per_day"]:
        raise ValueError("already %d automatic adjustments today (limit %d)" % (len(mine), g["max_adjustments_per_day"]))
    if mine:
        last = dt.datetime.fromisoformat(mine[-1]["at"]).replace(tzinfo=None)
        wait = g["min_minutes_between"] - (now_local() - last).total_seconds() / 60
        if wait > 0:
            raise ValueError("last automatic adjustment was too recent; try again in %d min" % (wait + 1))
    proj_before = project_today(ha, cfg, plan, trace=False)["summary"]
    if source == "plan":
        planner.push_values(ha, cfg, plan, values, offsets, by=by, reason=reason)
    else:
        planner.push_today(ha, cfg, plan, values, offsets, by=by, reason=reason)
    rev = (revisions.for_date(today) or [None])[-1]
    proj_after = project_today(ha, cfg, data.load_json("plan_latest.json", plan), values, offsets, trace=False)["summary"]
    touch("adjust")
    msg = None
    if post_update:
        lines = revisions.describe(cfg, rev) if rev else []
        text = reason.strip() + ("\n\nChanged: " + "; ".join(lines) if lines else "") + \
            "\nProjected rest of today: electricity £%.2f → £%.2f, time below comfort %.1f h → %.1f h." % (
                proj_before["grid_cost"], proj_after["grid_cost"], proj_before["hours_below"], proj_after["hours_below"])
        msg = chat_add(by, text, kind="update", meta={"revision": rev and rev["id"], "date": today})
    return {"revision": rev, "before": proj_before, "after": proj_after, "message": msg}


# ------------------------------------------------------------------ the compact picture Hermes reads first
def state(ha, cfg, plan, live=None):
    now = now_local()
    today = now.date().isoformat()
    out = {"now": now.isoformat(timespec="minutes"), "tariff_now": model.rate_at(cfg, now.hour + now.minute / 60)[1],
           "plan_for": plan["for_date"], "plan_chosen": plan["chosen"], "live": live,
           "comfort": {k: cfg["comfort"][k] for k in ("soft_floor_c", "hard_floor_c", "sleep_floor_c", "away_floor_c")},
           "occupancy_today": model.day_status(cfg, now.date()),
           "guard_rails": settings(cfg), "agent": status()}
    try:
        p = project_today(ha, cfg, plan)
        out["today"] = {k: p[k] for k in ("values", "offsets", "room_now", "predicted_now", "summary", "source")}
        out["today"]["blocks"] = planner.plan_blocks(cfg, p["values"], p["offsets"])
        out["today"]["ended_blocks"] = sorted(planner.ended_blocks(cfg, now.date(), now))
        out["today"]["projection"] = p["trace"][::4]
    except Exception as e:
        out["today"] = {"error": str(e)}
    out["revisions_today"] = [{k: r[k] for k in ("id", "at", "by", "reason")} | {"changes": revisions.describe(cfg, r)}
                              for r in revisions.for_date(today)]
    out["unread_messages"] = chat_since(status_cursor(), "you")
    return out


def status_cursor():
    return data.load_json(STATE, {}).get("read_upto", 0)


def mark_read(upto):
    st = data.load_json(STATE, {})
    st["read_upto"] = max(st.get("read_upto", 0), int(upto))
    data.save_json(STATE, st)


# ------------------------------------------------------------------ webhook
def notify(cfg, payload):
    """Fire-and-forget POST to the agent's webhook (if configured)."""
    url = settings(cfg).get("webhook_url")
    if not url:
        return

    def go():
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def checkin_loop(get_cfg):
    """Background thread in the web service: at each configured check-in time, POST {"type": "checkin"}."""
    import time
    done = set()
    while True:
        try:
            cfg = get_cfg()
            now = now_local()
            key = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") in settings(cfg)["checkin_times"] and key not in done:
                done.add(key)
                notify(cfg, {"type": "checkin", "at": now.isoformat(timespec="minutes"), "state_url": "/api/agent/state"})
        except Exception:
            pass
        time.sleep(20)
