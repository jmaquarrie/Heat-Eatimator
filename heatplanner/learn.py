"""Daily re-fit of the model from stored observations, and a backtest of the fitted model.

Every fit is computed from ALL stored data and blended with the built-in prior (DEFAULT_PARAMS),
never with yesterday's result, so running it daily does not compound or drift.
"""
import datetime as dt
import statistics as st
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import data, model

LOCAL = ZoneInfo("Europe/London")
PRIOR = model.DEFAULT_PARAMS


def local_dt(ts):
    return dt.datetime.fromisoformat(ts).astimezone(LOCAL).replace(tzinfo=None)


def ols(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return a, b, (1 - ss_res / ss_tot) if ss_tot else 0.0


def ols2(x1, x2, y):
    """y = a + b1*x1 + b2*x2 via normal equations (Cramer). Returns (a, b1, b2, r2) or None."""
    n = len(y)
    m1, m2, my = sum(x1) / n, sum(x2) / n, sum(y) / n
    s11 = sum((a - m1) ** 2 for a in x1)
    s22 = sum((a - m2) ** 2 for a in x2)
    s12 = sum((a - m1) * (b - m2) for a, b in zip(x1, x2))
    s1y = sum((a - m1) * (c - my) for a, c in zip(x1, y))
    s2y = sum((b - m2) * (c - my) for b, c in zip(x2, y))
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-9:
        return None
    b1 = (s1y * s22 - s2y * s12) / det
    b2 = (s2y * s11 - s1y * s12) / det
    a = my - b1 * m1 - b2 * m2
    ss_tot = sum((c - my) ** 2 for c in y)
    ss_res = sum((c - (a + b1 * p + b2 * q)) ** 2 for p, q, c in zip(x1, x2, y))
    return a, b1, b2, (1 - ss_res / ss_tot) if ss_tot else 0.0


def daily_groups(rows):
    days = defaultdict(list)
    for r in rows:
        days[local_dt(r["ts"]).date()].append(r)
    return {d: v for d, v in days.items() if len(v) >= 46}


def row_cop(params, r):
    to = r["outdoor"]
    lwt = r["lwt"] if (r.get("lwt") or 0) > 30 else 41.0
    pub = model.cop_published(to, lwt)
    ha = r.get("cop")
    if ha and 1.5 < ha < 7.5:
        b = params["cop_blend_published"]
        return b * pub + (1 - b) * ha
    return pub


def blend(prior, fit, w):
    return (1 - w) * prior + w * fit


def fit(rows):
    notes = []
    new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in PRIOR.items()}
    today = dt.datetime.now(LOCAL).date()
    days = {d: v for d, v in daily_groups(rows).items() if d < today}
    info = {"days_used": len(days), "rows": len(rows)}

    # ---- heat loss, free gains and solar gain from the daily heat balance
    if len(days) >= 6:
        X, R, Y, TIN = [], [], [], []
        for d, v in sorted(days.items()):
            X.append(st.mean(r["outdoor"] for r in v))
            Y.append(sum(r["heat_kwh"] * row_cop(PRIOR, r) for r in v))
            TIN.append(st.mean(r["room"] for r in v if r["room"] is not None))
            rd = [r["rad"] for r in v if r.get("rad") is not None]
            R.append(st.mean(rd) if len(rd) > 40 else None)
        have_rad = all(x is not None for x in R) and len(days) >= 12 and max(R) - min(R) > 0.02
        ua = g = gs = r2 = None
        if have_rad:
            f = ols2(X, R, Y)
            if f and f[1] < 0 and f[2] < 0 and f[3] >= 0.4:
                a, b1, b2, r2 = f
                ua, gs = -b1 / 24, -b2 / 24
                if 0.0 <= gs <= 8.0:
                    g = ua * st.mean(TIN) + gs * PRIOR["rad_ref_kw_m2"] - a / 24
                    notes.append("heat balance fitted with solar term (gs=%.2f)" % gs)
                else:
                    ua = None
        if ua is None and max(X) - min(X) >= 2.0:
            gs_fixed = PRIOR["gains_solar"]
            ref = PRIOR["rad_ref_kw_m2"]
            Yadj = [y + 24 * gs_fixed * ((rr if rr is not None else ref) - ref) for y, rr in zip(Y, R)]
            f = ols(X, Yadj)
            if f and f[1] < 0:
                a, b, r2 = f
                ua = -b / 24
                g = ua * st.mean(TIN) - a / 24
                gs = None
        if ua is not None:
            info.update({"heat_fit_r2": round(r2, 2), "ua_raw": round(ua, 3), "gains_raw": round(g, 2), "gs_raw": None if gs is None else round(gs, 2)})
            if 0.10 <= ua <= 0.60 and -0.5 <= g <= 4.0 and r2 >= 0.3:
                w = min(0.7, len(days) / 40)
                new["ua_kw_per_k"] = round(blend(PRIOR["ua_kw_per_k"], ua, w), 4)
                new["gains_kw"] = round(blend(PRIOR["gains_kw"], max(0.0, g), w), 3)
                if gs is not None:
                    new["gains_solar"] = round(blend(PRIOR["gains_solar"], gs, w), 3)
                notes.append("heat-loss fit accepted (prior weight %.0f%%)" % (100 * (1 - w)))
            else:
                notes.append("heat-loss fit rejected by sanity checks (ua=%.3f g=%.2f r2=%.2f)" % (ua, g, r2))
        else:
            notes.append("no usable heat-loss fit (need wider temperature range / negative slope)")
    else:
        notes.append("fewer than 6 complete days; heat-loss kept at prior")

    # ---- thermal mass from pump-off, night-time cooling (real heating runtime when available)
    xs, ys = [], []
    for i in range(len(rows) - 2):
        r0, r2_ = rows[i], rows[i + 2]
        if r0["room"] is None or r2_["room"] is None or r0["outdoor"] is None:
            continue
        if dt.datetime.fromisoformat(r2_["ts"]) - dt.datetime.fromisoformat(r0["ts"]) != dt.timedelta(hours=1):
            continue
        seg = rows[i:i + 3]
        off = all((s.get("heating") == 0.0) if s.get("heating") is not None else ((s.get("lwt") or 99) < 25 and s["heat_kwh"] < 0.02) for s in seg)
        night = not any((s.get("rad") or 0) > 0.02 for s in seg)
        if off and night:
            xs.append(r0["room"] - r0["outdoor"])
            ys.append(r2_["room"] - r0["room"])
    if len(xs) >= 25:
        f = ols(xs, ys)
        if f and f[1] < 0:
            c = new["ua_kw_per_k"] / (-f[1])
            info.update({"cooling_points": len(xs), "c_raw": round(c, 2), "cooling_r2": round(f[2], 2)})
            if 4.0 <= c <= 25.0 and f[2] >= 0.1:
                w = min(0.6, len(xs) / 150)
                new["c_kwh_per_k"] = round(blend(PRIOR["c_kwh_per_k"], c, w), 2)
                notes.append("thermal-mass fit accepted (%d pump-off night points)" % len(xs))
            else:
                notes.append("thermal-mass fit rejected (c=%.1f r2=%.2f)" % (c, f[2]))
    else:
        notes.append("only %d pump-off night points; thermal mass kept at prior" % len(xs))

    # ---- measured COP bins (shrunk toward the prior bins)
    bins = defaultdict(list)
    for r in rows:
        if r.get("cop") and 1.5 < r["cop"] < 7.5 and r["outdoor"] is not None and (r.get("lwt") or 0) > 28:
            bins[int(r["outdoor"] // 2 * 2)].append(r["cop"])
    cm = dict(PRIOR["cop_measured"])
    for k, v in bins.items():
        if len(v) >= 8:
            old = cm.get(str(k), st.mean(v))
            cm[str(k)] = round((sum(v) + 10 * old) / (len(v) + 10), 2)
    new["cop_measured"] = cm
    info["cop_bins_updated"] = sorted(k for k, v in bins.items() if len(v) >= 8)

    # ---- heat pump outdoor sensor vs forecast-model bias
    d = [r["outdoor"] - r["om_temp"] for r in rows[-1400:] if r.get("om_temp") is not None and r["outdoor"] is not None]
    if len(d) >= 96:
        info["outdoor_bias_raw"] = round(st.mean(d), 2)
        new["outdoor_bias_c"] = round(max(-3.0, min(3.0, st.mean(d))), 2)
    # ---- weather curve and radiator output (used when the leaving-water offset is planned)
    run = [r for r in rows if (r.get("heating") or 0) >= 0.8 and r.get("lwt") and r.get("outdoor") is not None
           and r.get("room") is not None and r["lwt"] > 22]
    info["lwt_rows"] = len(run)
    if len(run) >= 40:
        xs = [r["outdoor"] for r in run]
        ys = [r["lwt"] - (r.get("lwt_offset") or 0.0) for r in run]
        f = ols(xs, ys)
        if f and -1.2 < f[1] < -0.1 and 25 < f[0] < 50 and f[2] > 0.2:
            w = min(0.8, len(run) / 400)
            new["lwc_a"], new["lwc_b"] = round(blend(PRIOR["lwc_a"], f[0], w), 2), round(blend(PRIOR["lwc_b"], f[1], w), 3)
            notes.append("weather curve fitted: LWT = %.1f %+.2f x outdoor (r2 %.2f, %d running half-hours, weight %.0f%%)" % (f[0], f[1], f[2], len(run), 100 * w))
        elif f:
            notes.append("weather-curve fit rejected (a=%.1f b=%.2f r2=%.2f); kept prior" % f)
        ks = []
        for r in run:
            dT = r["lwt"] - 2.5 - r["room"]
            if dT > 3 and (r.get("heat_kwh") or 0) > 0.05:
                ks.append(r["heat_kwh"] * 2 * row_cop(new, r) / r["heating"] / dT)
        if len(ks) >= 30:
            k = st.median(ks)
            if 0.3 < k < 3.0:
                w = min(0.8, len(ks) / 300)
                new["k_emit_kw_per_k"] = round(blend(PRIOR["k_emit_kw_per_k"], k, w), 3)
                notes.append("radiator output fitted: %.2f kW per K (median of %d, weight %.0f%%)" % (k, len(ks), 100 * w))
            else:
                notes.append("radiator-output fit rejected (k=%.2f); kept prior" % k)
    else:
        notes.append("only %d running half-hours with LWT data; weather curve and radiator output kept at prior" % len(run))
    return new, notes, info


def presence_hint(cfg, rows):
    """Share of recent daytime hours at home, per weekday, from the phone tracker."""
    o = cfg["occupancy"]
    by_day = defaultdict(list)
    for r in rows:
        if r.get("home") is None:
            continue
        ld = local_dt(r["ts"])
        if o["away_from"] <= ld.hour + ld.minute / 60 < o["away_to"]:
            by_day[ld.date()].append(r["home"])
    per = defaultdict(list)
    for d, v in by_day.items():
        if len(v) >= 12:
            per[d.weekday()].append(st.mean(v))
    return {str(k): {"days": len(v), "home_share": round(st.mean(v), 2)} for k, v in sorted(per.items())}


def backtest(cfg, params, rows, max_days=14):
    days = daily_groups(rows)
    today = dt.datetime.now(LOCAL).date()
    out = []
    for d in sorted(days)[-max_days:]:
        if d >= today:
            continue
        v = days[d]
        by_hour = defaultdict(list)
        for r in v:
            by_hour[local_dt(r["ts"]).hour].append(r)
        if len(by_hour) < 24:
            continue
        blocks, offs = [], []
        for h in range(24):
            tg = [r["target"] for r in by_hour[h] if r["target"] is not None]
            blocks.append((h, h + 1, st.mean(tg) if tg else 18.0))
            of = [r["lwt_offset"] for r in by_hour[h] if r.get("lwt_offset") is not None]
            offs.append((h, h + 1, round(st.mean(of)) if of else 0))
        pts = sorted((local_dt(r["ts"]), r["outdoor"]) for r in v if r["outdoor"] is not None)
        rad_pts = sorted((local_dt(r["ts"]), r["rad"]) for r in v if r.get("rad") is not None)
        room0 = next((r["room"] for r in v if r["room"] is not None), 19.5)
        ctx = {"start_dt": dt.datetime.combine(d, dt.time(0)), "hours": 24, "t_room0": room0,
               "soc0_kwh": 0, "outdoor": data.series_fn(pts), "tomorrow": d + dt.timedelta(days=1), "terminal_ref_c": 19.5}
        if len(rad_pts) > 40:
            ctx["radiation"] = data.series_fn(rad_pts)
        plan = {"fixed": blocks, "day": blocks}
        if cfg.get("lwt_offset", {}).get("enabled"):   # backtest in the same mode the planner plans in
            plan.update({"fixed_off": offs, "day_off": offs})
        res = model.simulate(cfg, params, plan, ctx, use_battery=False, trace=True)
        actual_kwh = sum(r["heat_kwh"] for r in v)
        act_room = {}
        for r in v:
            if r["room"] is not None:
                act_room.setdefault(local_dt(r["ts"]).strftime("%H"), []).append(r["room"])
        errs = [row["room"] - st.mean(act_room[row["t"][11:13]]) for row in res["trace"]
                if row["t"][14:16] == "00" and row["t"][11:13] in act_room]
        out.append({"date": d.isoformat(), "pred_kwh": round(res["kwh_hp"], 1), "actual_kwh": round(actual_kwh, 1),
                    "room_rmse": round((sum(e * e for e in errs) / len(errs)) ** 0.5, 2) if errs else None,
                    "room_bias": round(st.mean(errs), 2) if errs else None,
                    "mean_outdoor": round(st.mean(r["outdoor"] for r in v), 1)})
    return out


def _mae(bt):
    e = [abs(x["pred_kwh"] - x["actual_kwh"]) for x in bt]
    return sum(e) / len(e) if e else None


def run_learn(cfg, ha=None):
    rows = data.load_rows()
    prev = data.load_json("params.json", {})
    saved = data.load_params()
    new, notes, info = fit(rows)
    info["presence_hint"] = presence_hint(cfg, rows)
    bt_old, bt_new = backtest(cfg, saved, rows), backtest(cfg, new, rows)
    so, sn = _mae(bt_old), _mae(bt_new)
    accepted = True
    if so is not None and sn is not None and sn > so * 1.25 + 0.5:
        accepted = False
        notes.append("new fit backtests worse (%.1f vs %.1f kWh/day MAE); kept previous parameters" % (sn, so))
        new = saved
    hist = prev.get("history", [])
    hist.append({"at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "accepted": accepted,
                 "params": {k: new[k] for k in ("ua_kw_per_k", "gains_kw", "gains_solar", "c_kwh_per_k", "outdoor_bias_c",
                                                 "lwc_a", "lwc_b", "k_emit_kw_per_k")},
                 "mae_kwh_prev": so, "mae_kwh_new": sn, "days_used": info["days_used"]})
    data.save_json("params.json", {"params": new, "fitted_at": hist[-1]["at"], "info": info, "notes": notes,
                                   "backtest": backtest(cfg, new, rows), "history": hist[-60:]})
    return {"accepted": accepted, "notes": notes, "info": info, "mae_prev": so, "mae_new": sn}
