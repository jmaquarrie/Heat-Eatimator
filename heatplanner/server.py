"""Dashboard + JSON API for the heat pump planner (stdlib only).

/            new single-page dashboard (heatplanner/static/)
/classic     the original server-rendered page
"""
import datetime as dt
import html
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import agent, data, model, planner, revisions, settings

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CTYPES = {".png": "image/png", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".svg": "image/svg+xml", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
# Optional shared key for actions that change things (push, re-run, occupancy). Unset = open, as before.
WRITE_KEY = os.environ.get("HEATPLANNER_WRITE_KEY", "")
# Key for the agent API (/api/agent/*). Falls back to the write key; if neither is set the API is open (LAN only).
AGENT_KEY = os.environ.get("HEATPLANNER_AGENT_KEY", "")
LIVE_CACHE = {}
STATUS_CACHE = {"at": 0.0, "body": None}

PORT = int(os.environ.get("HEATPLANNER_PORT", "8110"))
RUN = {"state": "idle", "started": None, "finished": None, "message": ""}
RUN_LOCK = threading.Lock()
STATUSES = [("home", "Home all day"), ("away_day", "Out in the daytime"), ("away_all", "Away all day")]
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def esc(x):
    return html.escape(str(x))


def hh(h):
    return "%02d:%02d" % (int(h), int(round((h - int(h)) * 60)))


def chart(plan):
    best, cur = plan.get("trace_best", []), plan.get("trace_current", [])
    if not best:
        return ""
    W, H, L, R, T, B = 900, 280, 42, 12, 14, 30
    n = len(best)
    ys = [r["room"] for r in best] + [r["room"] for r in cur] + [r["outdoor"] for r in best] + [r["target"] for r in best]
    lo, hi = min(ys) - 0.5, max(ys) + 0.5
    X = lambda i: L + (W - L - R) * i / (n - 1)
    Y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    pts = lambda rows, k: " ".join("%.1f,%.1f" % (X(i), Y(r[k])) for i, r in enumerate(rows))
    out = ['<svg viewBox="0 0 %d %d" class="chart">' % (W, H)]
    # tariff background
    for i, r in enumerate(best[:-1]):
        col = {"cosy": "#1d4d2b", "peak": "#5a2323", "standard": "none"}.get(
            "cosy" if r["rate"] < 0.2 else ("peak" if r["rate"] > 0.35 else "standard"))
        if col != "none":
            out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity="0.55"/>' % (X(i), T, X(i + 1) - X(i) + 0.5, H - T - B, col))
    v = int(lo)
    while v <= hi:
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#2a3140"/><text x="4" y="%.1f" fill="#8b95a7" font-size="11">%d°</text>' % (L, W - R, Y(v), Y(v), Y(v) + 4, v))
        v += 2
    for i, r in enumerate(best):
        if r["t"][14:16] == "00" and int(r["t"][11:13]) % 3 == 0:
            out.append('<text x="%.1f" y="%d" fill="#8b95a7" font-size="10" text-anchor="middle">%s</text>' % (X(i), H - 12, r["t"][11:13] + (":00" if r["t"][11:13] != "00" else " " + r["t"][5:10])))
    out.append('<polyline fill="none" stroke="#7aa2f7" stroke-width="1.5" points="%s"/>' % pts(best, "outdoor"))
    out.append('<polyline fill="none" stroke="#e0af68" stroke-width="1.5" stroke-dasharray="4 3" points="%s"/>' % pts(best, "target"))
    out.append('<polyline fill="none" stroke="#8b95a7" stroke-width="1.8" points="%s"/>' % pts(cur, "room"))
    out.append('<polyline fill="none" stroke="#9ece6a" stroke-width="2.2" points="%s"/>' % pts(best, "room"))
    out.append("</svg>")
    out.append('<div class="legend"><span style="color:#9ece6a">&#9632; room (chosen plan)</span> <span style="color:#8b95a7">&#9632; room (current routine)</span> '
               '<span style="color:#e0af68">&#9632; setpoint</span> <span style="color:#7aa2f7">&#9632; outdoor forecast</span> '
               '<span style="color:#3f8f55">&#9632; cosy</span> <span style="color:#a04444">&#9632; peak</span></div>')
    return "".join(out)



JS = """
document.getElementById('occsave').onclick=async()=>{
 const f=document.getElementById('occform'),m=document.getElementById('occmsg');
 const aw=[...f.querySelectorAll('input[name=aw]:checked')].map(x=>+x.value);
 const ov={};[...f.querySelectorAll('input[type=radio]:checked')].forEach(x=>{ov[x.name.slice(2)]=x.value});
 m.textContent='saving…';
 const r=await fetch('/api/occupancy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({away_weekdays:aw,away_from:+f.af.value,away_to:+f.at.value,overrides:ov})});
 if(!r.ok){m.textContent='error: '+await r.text();return}
 m.textContent='re-running model…';
 for(let i=0;i<120;i++){await new Promise(z=>setTimeout(z,1500));const s=await (await fetch('/api/rerun-status')).json();
  if(s.state==='done'){m.textContent='done';location.reload();return}
  if(s.state==='error'){m.textContent='failed: '+s.message;return}}
 m.textContent='still running — refresh in a minute';
};
"""


COLORS = {"Current routine": "#8b95a7", "Steady comfort temp (battery covers gaps)": "#7aa2f7",
          "Cosy windows only (floors off-peak)": "#bb9af7", "Optimised": "#9ece6a"}
COMPONENTS = [("cosy", "Cosy-rate energy"), ("standard", "Standard-rate energy"), ("peak", "Peak-rate energy"),
              ("penalty", "Comfort shortfall"), ("terminal", "Heat left in house"), ("battery", "Battery left over")]


def _axes(W, H, L, R, T, B, lo, hi, step, fmt="%g"):
    Y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    o = []
    v = (int(lo / step) - (1 if lo < 0 else 0)) * step
    while v <= hi + 1e-9:
        if v >= lo - 1e-9:
            o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/><text x="4" y="%.1f" fill="#8b95a7" font-size="11">%s</text>' % (
                L, W - R, Y(v), Y(v), "#4a5468" if abs(v) < 1e-9 else "#2a3140", Y(v) + 4, fmt % v))
        v += step
    return Y, o


def savings_chart(plan):
    cmp_ = plan.get("compare")
    if not cmp_:
        return ""
    base = cmp_["Current routine"]["breakdown"]
    names = [n for n in cmp_ if n != "Current routine"]
    deltas = {n: {k: base[k] - cmp_[n]["breakdown"][k] for k, _ in COMPONENTS} for n in names}
    W, H, L, R, T, B = 900, 300, 46, 12, 16, 58
    vals = [v for d in deltas.values() for v in d.values()] + [0]
    lo, hi = min(vals) - 0.15, max(vals) + 0.15
    step = 0.5 if hi - lo > 2.5 else 0.25
    Y, out = _axes(W, H, L, R, T, B, lo, hi, step, "£%g")
    out.insert(0, '<svg viewBox="0 0 %d %d" class="chart">' % (W, H))
    gw = (W - L - R) / len(COMPONENTS)
    bw = min(34, gw / (len(names) + 1))
    for ci, (k, label) in enumerate(COMPONENTS):
        x0 = L + gw * ci + gw / 2 - bw * len(names) / 2
        for ni, n in enumerate(names):
            v = deltas[n][k]
            y1, y0 = Y(max(v, 0)), Y(min(v, 0))
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s: %s £%.2f</title></rect>' % (x0 + ni * bw, y1, bw - 3, max(1.0, y0 - y1), COLORS.get(n, "#aaa"), esc(label), esc(n), v))
            if abs(v) >= 0.05:
                out.append('<text x="%.1f" y="%.1f" fill="#d7dde8" font-size="10" text-anchor="middle">%+.2f</text>' % (x0 + ni * bw + (bw - 3) / 2, (y1 - 3) if v >= 0 else (y0 + 11), v))
        out.append('<text x="%.1f" y="%d" fill="#8b95a7" font-size="11" text-anchor="middle">%s</text>' % (L + gw * ci + gw / 2, H - 30, esc(label)))
    out.append("</svg>")
    out.append('<div class="legend">%s<span class=muted>&nbsp; Bars above zero = money saved vs your current routine; below zero = costs more.</span></div>' % " ".join(
        '<span style="color:%s">&#9632; %s</span>' % (COLORS.get(n, "#aaa"), esc(n)) for n in names))
    # numbers
    rows = ["<table style='margin-top:8px'><tr><th>Saving vs current (£)</th>" + "".join("<th class=r>%s</th>" % esc(l) for _, l in COMPONENTS) + "<th class=r>Net</th></tr>"]
    for n in names:
        net = sum(deltas[n].values())
        rows.append("<tr><td style='color:%s'>%s</td>%s<td class='r %s'><b>%+.2f</b></td></tr>" % (
            COLORS.get(n, "#aaa"), esc(n), "".join("<td class=r>%+.2f</td>" % deltas[n][k] for k, _ in COMPONENTS), "good" if net > 0 else "bad", net))
    rows.append("</table>")
    return "".join(out) + "".join(rows)


def lines_chart(plan, key, title_fmt, ylabel, step, rel_to=None, setpoint=False):
    cmp_ = plan.get("compare")
    if not cmp_:
        return ""
    W, H, L, R, T, B = 900, 260, 46, 12, 14, 30
    names = list(cmp_)
    n = len(cmp_[names[0]][key])
    series = {}
    for nm in names:
        v = cmp_[nm][key]
        if rel_to:
            v = [a - b for a, b in zip(v, cmp_[rel_to][key])]
        series[nm] = v
    allv = [x for v in series.values() for x in v]
    lo, hi = min(allv) - 0.1 * (max(allv) - min(allv) + 0.5), max(allv) + 0.1 * (max(allv) - min(allv) + 0.5)
    Y, out = _axes(W, H, L, R, T, B, lo, hi, step, title_fmt)
    X = lambda i: L + (W - L - R) * i / (n - 1)
    out.insert(0, '<svg viewBox="0 0 %d %d" class="chart">' % (W, H))
    tmpl = cmp_[names[0]]["t"]
    cfg = data.load_config()
    for i in range(n - 1):
        hour = int(tmpl[i][11:13])
        slot = "cosy" if any(a <= hour < b for a, b in cfg["tariff"]["cosy"]) else ("peak" if any(a <= hour < b for a, b in cfg["tariff"]["peak"]) else "")
        if slot:
            out.insert(1, '<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity="0.45"/>' % (X(i), T, X(i + 1) - X(i) + 0.5, H - T - B, "#1d4d2b" if slot == "cosy" else "#5a2323"))
    for i in range(0, n, 3):
        out.append('<text x="%.1f" y="%d" fill="#8b95a7" font-size="10" text-anchor="middle">%s</text>' % (X(i), H - 12, tmpl[i][11:16] if tmpl[i][11:13] != "00" else tmpl[i][5:10]))
    for nm in names:
        w = 2.6 if nm == "Optimised" else 1.6
        out.append('<polyline fill="none" stroke="%s" stroke-width="%s" points="%s"/>' % (COLORS.get(nm, "#aaa"), w, " ".join("%.1f,%.1f" % (X(i), Y(v)) for i, v in enumerate(series[nm]))))
    out.append("</svg>")
    out.append('<div class="legend">%s <span style="color:#3f8f55">&#9632; cosy</span> <span style="color:#a04444">&#9632; peak</span> <span class=muted>%s</span></div>' % (
        " ".join('<span style="color:%s">&#9632; %s</span>' % (COLORS.get(n_, "#aaa"), esc(n_)) for n_ in names), esc(ylabel)))
    return "".join(out)


def page():
    plan = data.load_json("plan_latest.json", None)
    pj = data.load_json("params.json", {})
    idx = data.load_json("plans_index.json", [])
    cfg = data.load_config()
    logs = []
    try:
        with open(data.path("log.jsonl")) as f:
            logs = f.readlines()[-12:]
    except FileNotFoundError:
        pass
    h = ["<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         "<title>Heat pump planner</title><style>"
         "body{background:#0f1419;color:#d7dde8;font:14px/1.45 system-ui,sans-serif;margin:0;padding:18px;max-width:980px;margin:auto}"
         "h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:#9ece6a}"
         ".card{background:#171d26;border:1px solid #242c39;border-radius:8px;padding:12px 14px;margin:10px 0}"
         "table{border-collapse:collapse;width:100%}td,th{padding:5px 8px;border-bottom:1px solid #242c39;text-align:left}"
         "th{color:#8b95a7;font-weight:500}.r{text-align:right}.good{color:#9ece6a}.bad{color:#f7768e}.muted{color:#8b95a7}"
         ".chart{width:100%;height:auto;background:#12171e;border-radius:6px}.legend{font-size:12px;margin-top:4px}"
         ".pill{display:inline-block;padding:1px 8px;border-radius:10px;background:#242c39;font-size:12px}"
         "code{background:#12171e;padding:1px 5px;border-radius:4px}</style></head><body>"]
    h.append("<h1>Heat pump planner</h1><div class=muted>Daikin Altherma 3 M · Octopus Cosy · plan pushed to <code>%s</code></div>" % esc(cfg["schedule_entity"]))
    if not plan:
        h.append("<div class=card>No plan generated yet.</div></body></html>")
        return "".join(h)
    ps = plan.get("pushed")
    h.append("<div class=card><b>Plan for %s %s</b> &nbsp;<span class=pill>%s</span> &nbsp;<span class=pill>forecast %s° to %s° (mean %s°)</span> "
             "&nbsp;<span class=pill>%s</span><br><span class=muted>Generated %s. Starting room %.1f°, battery %.1f kWh.</span></div>" % (
                 esc(plan["weekday"].title()), esc(plan["for_date"]), esc(plan["chosen"]), plan["forecast_tomorrow"]["min"], plan["forecast_tomorrow"]["max"], plan["forecast_tomorrow"]["mean"],
                 ("pushed %s%s" % (esc(ps["at"][11:16]), " ✓ verified" if ps.get("verified") else " ✗ NOT verified")) if ps else "not pushed (preview)",
                 esc(plan["created"].replace("T", " ")), plan["t_room0"], plan["soc0_kwh"]))
    h.append("<h2>Setpoints</h2><div class=card><table><tr><th>Time</th><th class=r>Room setpoint</th><th>Tariff</th></tr>")
    for b in plan["blocks"]:
        tags = set()
        for x in range(int(b["from"]), int(b["to"])):
            tags.add("cosy" if any(a <= x < c for a, c in cfg["tariff"]["cosy"]) else ("peak" if any(a <= x < c for a, c in cfg["tariff"]["peak"]) else "standard"))
        h.append("<tr><td>%s – %s</td><td class=r><b>%.1f°C</b></td><td class=muted>%s</td></tr>" % (hh(b["from"]), hh(b["to"] if b["to"] < 24 else 24), b["temperature"], ", ".join(sorted(tags))))
    h.append("</table></div><h2>Predicted room temperature</h2><div class=card>%s</div>" % chart(plan))

    occ = cfg["occupancy"]
    today = dt.date.today()
    h.append("<h2>Who is home?</h2><div class=card><div class=muted style='margin-bottom:8px'>Set the usual weekly pattern, then override any day this week. "
             "Saving re-runs the model and re-pushes tomorrow's schedule.</div>")
    h.append("<form id=occform><div style='margin-bottom:10px'><b>Usual pattern:</b> out in the daytime on ")
    for i, n in enumerate(WD):
        h.append("<label style='margin-right:8px'><input type=checkbox name=aw value=%d %s> %s</label>" % (i, "checked" if i in occ["away_weekdays"] else "", n))
    h.append("<br><span class=muted>Away hours</span> <input type=number step=0.5 min=0 max=24 name=af value=%s style='width:64px'> to <input type=number step=0.5 min=0 max=24 name=at value=%s style='width:64px'></div>" % (occ["away_from"], occ["away_to"]))
    h.append("<table><tr><th>Day</th>" + "".join("<th>%s</th>" % l for _, l in STATUSES) + "<th class=muted>Usual</th></tr>")
    for i in range(0, 8):
        d = today + dt.timedelta(days=i)
        cur = occ.get("overrides", {}).get(d.isoformat())
        usual = "away_day" if d.weekday() in occ["away_weekdays"] else "home"
        eff = cur or usual
        h.append("<tr><td>%s %s%s</td>" % (WD[d.weekday()], d.strftime("%d %b"), " <span class=pill>today</span>" if i == 0 else ""))
        for k, _ in STATUSES:
            h.append("<td><input type=radio name='d_%s' value=%s %s></td>" % (d.isoformat(), k, "checked" if eff == k else ""))
        h.append("<td class=muted>%s%s</td></tr>" % (dict(STATUSES)[usual], " (overridden)" if cur and cur != usual else ""))
    h.append("</table><div style='margin-top:10px'><button type=button id=occsave>Save &amp; re-run model</button> <span id=occmsg class=muted></span></div></form>")
    hint = pj.get("info", {}).get("presence_hint", {})
    if hint:
        h.append("<div class=muted style='margin-top:8px'>Phone-presence hint (share of %s–%s spent at home, recent days): %s</div>" % (hh(occ["away_from"]), hh(occ["away_to"]), " · ".join("%s %d%% (%dd)" % (WD[int(k)], round(v["home_share"] * 100), v["days"]) for k, v in hint.items())))
    h.append("</div>")
    if plan.get("outlook"):
        h.append("<h2>Week ahead (preview, re-planned nightly)</h2><div class=card><table><tr><th>Day</th><th>Occupancy</th><th class=r>Mean °C</th><th>Setpoints</th><th class=r>Cost £</th><th class=r>Current routine £</th></tr>")
        for o in plan["outlook"]:
            if "error" in o:
                continue
            h.append("<tr><td>%s %s</td><td class=muted>%s</td><td class=r>%s</td><td class=muted>%s</td><td class=r>%.2f</td><td class=r>%.2f</td></tr>" % (
                o["weekday"][:3].title(), o["date"][5:], dict(STATUSES)[o["status"]], o["forecast_mean"], " · ".join("%s %.1f" % (hh(b["from"]), b["temperature"]) for b in o["blocks"]), o["total"], o["current_total"]))
        h.append("</table><div class=muted style='margin-top:6px'>Costs are per 30 h model window incl. comfort penalty; rough, as the forecast gets less certain further out.</div></div>")
    h.append("<h2>Strategy comparison</h2><div class=card><div class=muted style='margin-bottom:6px'>Horizon: 22:00 tonight to 04:00 the day after tomorrow. "
             "Grid cost = pump + house at Cosy rates with the battery shifting load; penalty = comfort shortfall (priced); total includes end-state heat value.</div>"
             "<table><tr><th>Strategy</th><th class=r>Grid £</th><th class=r>Comfort £</th><th class=r>Total £</th><th class=r>Pump kWh</th><th class=r>Min awake °C</th></tr>")
    best_total = min(r["total"] for n, r in plan["strategies"].items() if "reference" not in n)
    for n, r in plan["strategies"].items():
        cls = "good" if r["total"] == best_total and "reference" not in n else ""
        mark = " ◀ chosen" if n == plan["chosen"] else ""
        h.append("<tr class='%s'><td>%s%s</td><td class=r>%.2f</td><td class=r>%.2f</td><td class=r>%.2f</td><td class=r>%.1f</td><td class=r>%.1f</td></tr>" % (cls, esc(n), mark, r["grid_cost"], r["penalty"], r["total"], r["kwh_hp"], r["t_min_awake"]))
    h.append("</table></div>")

    h.append("<h2>Where the savings come from</h2><div class=card>%s</div>" % savings_chart(plan))
    h.append("<h2>Running cost through the window (cumulative, £)</h2><div class=card>%s</div>" % lines_chart(plan, "cum", "£%g", "Cumulative grid cost + comfort penalty (steeper = spending faster)", 1.0))
    h.append("<h2>Saving vs current routine over time (£)</h2><div class=card>%s</div>" % lines_chart(plan, "cum", "£%g", "Above zero = costs more than current routine at that moment; falling line = saving", 0.25, rel_to="Current routine"))
    h.append("<h2>Room temperature, all plans (°C)</h2><div class=card>%s</div>" % lines_chart(plan, "room", "%g°", "Predicted room temperature", 1.0))
    h.append("<h2>Heat pump electricity per hour, all plans (kWh)</h2><div class=card>%s</div>" % lines_chart(plan, "el", "%g", "Pump electricity used each hour", 0.5))
    p = pj.get("params", {})
    h.append("<h2>Model (re-fitted daily)</h2><div class=card><table>")
    for k, label in (("ua_kw_per_k", "Heat loss (kW/K)"), ("gains_kw", "Free gains (kW)"), ("gains_solar", "Solar gain factor (kW per kW/m²)"), ("c_kwh_per_k", "Thermal mass (kWh/K)"), ("outdoor_bias_c", "Outdoor sensor bias (°C)")):
        h.append("<tr><td>%s</td><td class=r>%s</td></tr>" % (label, esc(p.get(k, plan["params"].get(k)))))
    h.append("<tr><td>Last fit</td><td class=r>%s</td></tr></table>" % esc(pj.get("fitted_at", "never")))
    for n in pj.get("notes", []):
        h.append("<div class=muted>• %s</div>" % esc(n))
    bt = pj.get("backtest", [])
    if bt:
        h.append("<h2 style='font-size:13px'>Backtest: model vs actual</h2><table><tr><th>Date</th><th class=r>Outdoor</th><th class=r>Predicted kWh</th><th class=r>Actual kWh</th><th class=r>Room RMSE</th></tr>")
        for b in bt[-10:]:
            h.append("<tr><td>%s</td><td class=r>%s°</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td></tr>" % (b["date"], b["mean_outdoor"], b["pred_kwh"], b["actual_kwh"], b["room_rmse"]))
        h.append("</table>")
    h.append("</div>")
    if idx:
        h.append("<h2>Recent plans</h2><div class=card><table><tr><th>For</th><th>Plan</th><th class=r>Saving £</th><th>Setpoints</th></tr>")
        for i in reversed(idx[-10:]):
            h.append("<tr><td>%s</td><td>%s</td><td class=r>%.2f</td><td class=muted>%s</td></tr>" % (esc(i["for_date"]), esc(i["chosen"]), i["saving_gbp"], esc(" · ".join("%s-%s %.1f" % (hh(b["from"]), hh(b["to"]), b["temperature"]) for b in i["blocks"]))))
        h.append("</table></div>")
    if logs:
        h.append("<h2>Log</h2><div class='card muted' style='font-family:monospace;font-size:12px'>%s</div>" % "<br>".join(esc(l.strip()[:200]) for l in logs))
    h.append("<p class=muted>Runs at 22:00 (collect → forecast → optimise → push) and re-fits the model daily at 03:30. Read-only page; edit <code>/opt/heatplanner/data/config.json</code> to change comfort, occupancy or battery settings.</p><script>%s</script></body></html>" % JS)
    return "".join(h)



def save_occupancy(body):
    """Validate and persist the weekly pattern + per-day overrides, then re-run the model in the background."""
    cfg_over = data.load_json("config.json", {})
    occ = cfg_over.setdefault("occupancy", {})
    aw = sorted({int(x) for x in body.get("away_weekdays", []) if 0 <= int(x) <= 6})
    af, at = float(body.get("away_from", 8)), float(body.get("away_to", 16))
    if not (0 <= af < at <= 24):
        return 400, "away hours must satisfy 0 <= from < to <= 24", "text/plain"
    today = dt.date.today()
    ov = {}
    for k, v in (body.get("overrides") or {}).items():
        d = dt.date.fromisoformat(k)
        if v not in ("home", "away_day", "away_all") or d < today - dt.timedelta(days=1) or d > today + dt.timedelta(days=60):
            continue
        usual = "away_day" if d.weekday() in aw else "home"
        if v != usual:  # only store real deviations from the usual pattern
            ov[k] = v
    occ.update({"away_weekdays": aw, "away_from": af, "away_to": at, "overrides": ov})
    data.save_json("config.json", cfg_over)
    return start_rerun()


def start_rerun():
    with RUN_LOCK:
        if RUN["state"] == "running":
            return 409, json.dumps({"error": "a run is already in progress"}), "application/json"
        RUN.update({"state": "running", "started": dt.datetime.now().isoformat(timespec="seconds"), "finished": None, "message": ""})

    def work():
        try:
            r = subprocess.run([sys.executable, "-m", "heatplanner.cli", "plan", "--push"], capture_output=True, text=True, timeout=600,
                               env=dict(os.environ), cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            RUN.update({"state": "done" if r.returncode == 0 else "error", "message": (r.stderr or r.stdout)[-400:]})
        except Exception as e:
            RUN.update({"state": "error", "message": str(e)})
        RUN["finished"] = dt.datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=work, daemon=True).start()
    return 200, json.dumps({"started": True}), "application/json"


def _ha():
    from .ha import HA
    return HA()


def live_status():
    """Current readings from HA (cached 60 s). Only HA REST reads: no Daikin cloud calls are made."""
    if STATUS_CACHE["body"] and time.time() - STATUS_CACHE["at"] < 60:
        return STATUS_CACHE["body"]
    cfg = data.load_config()
    e = cfg["entities"]
    out = {"available": False, "at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        ha = _ha()
    except KeyError:
        out["error"] = "HASS_URL / HASS_TOKEN not set for the web service"
        return out

    def get(eid):
        try:
            return ha.rest("/api/states/" + eid, timeout=6)
        except Exception:
            return None

    def num(st):
        try:
            return float(st["state"])
        except Exception:
            return None

    room, outdoor, soc, rate = (get(e[k]) for k in ("room", "outdoor", "battery_soc", "rate"))
    heating, climate, helper, budget = get(e["heating"]), get(e.get("climate", "")), get(cfg["schedule_entity"]), get(e.get("api_budget", ""))
    gridp = get(e["grid_power"]) if e.get("grid_power") else None
    offent = get(e["lwt_offset"]) if e.get("lwt_offset") else None
    rooms = {name: num(get(eid)) for name, eid in (cfg.get("other_rooms") or {}).items()}
    if room is None and outdoor is None:
        out["error"] = "Home Assistant did not answer"
        return out
    out.update({"available": True, "room": num(room), "outdoor": num(outdoor), "battery_pct": num(soc), "rate": num(rate),
                "heating": (heating or {}).get("state"), "api_budget": num(budget), "grid_w": num(gridp) if gridp else None,
                "other_rooms": {k: v for k, v in rooms.items() if v is not None}})
    if climate:
        a = climate.get("attributes", {})
        out["climate"] = {"state": climate.get("state"), "setpoint": a.get("temperature"), "current": a.get("current_temperature")}
    if helper:
        a = helper.get("attributes", {})
        out["helper"] = {"state": helper.get("state"), "temperature": a.get("temperature"), "next_event": a.get("next_event"),
                         "lwt_offset": a.get("lwt_offset")}
    if offent:
        a = offent.get("attributes", {})
        out["offset"] = {"value": a.get("temperature"), "min": a.get("min_temp"), "max": a.get("max_temp"),
                         "lwt": a.get("current_temperature")}
    sp, ht = (out.get("climate") or {}).get("setpoint"), (out.get("helper") or {}).get("temperature")
    out["following"] = None if sp is None or ht is None else abs(float(sp) - float(ht)) < 0.26
    ho, ov = (out.get("helper") or {}).get("lwt_offset"), (out.get("offset") or {}).get("value")
    out["offset_following"] = None if ho is None or ov is None else abs(float(ho) - float(ov)) < 0.5
    STATUS_CACHE.update({"at": time.time(), "body": out})
    return out


def actuals(date_s):
    """Measured hourly room temperature / pump kWh for a local date, beside what that day's plan predicted."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/London")
    d = dt.date.fromisoformat(date_s) if date_s else dt.date.today() - dt.timedelta(days=1)
    if d == dt.date.today():
        live = live_today(tz)
        if live is not None:
            return live
    hours = {}
    for r in data.load_rows():
        t = dt.datetime.fromisoformat(r["ts"]).astimezone(tz)
        if t.date() != d:
            continue
        h = hours.setdefault(t.hour, {"room": [], "outdoor": [], "target": [], "kwh": 0.0, "grid": None})
        if r.get("grid_kwh") is not None:
            h["grid"] = (h["grid"] or 0.0) + r["grid_kwh"]
        for k in ("room", "outdoor", "target"):
            if r.get(k) is not None:
                h[k].append(r[k])
        h["kwh"] += r.get("heat_kwh") or 0.0
    mean = lambda v: round(sum(v) / len(v), 2) if v else None
    act = [{"h": h, "room": mean(v["room"]), "outdoor": mean(v["outdoor"]), "target": mean(v["target"]), "kwh": round(v["kwh"], 2),
            "grid_kwh": None if v["grid"] is None else round(v["grid"], 3)} for h, v in sorted(hours.items())]
    cfg = data.load_config()
    gh = [r for r in act if r["grid_kwh"] is not None]
    grid = None
    if gh:
        by = {"cosy": 0.0, "standard": 0.0, "peak": 0.0}
        cost = 0.0
        for r in gh:
            rate, slot = model.rate_at(cfg, r["h"])
            by[slot] += r["grid_kwh"]
            cost += r["grid_kwh"] * rate
        grid = {"kwh": round(sum(by.values()), 2), "kwh_by_slot": {k: round(v, 2) for k, v in by.items()},
                "cost": round(cost, 2), "hours": len(gh)}
    plan = data.load_json("plan_%s.json" % d.isoformat(), None)
    pred, strat = None, None
    if plan and plan.get("compare"):
        strat = plan["chosen"] if plan["chosen"] in plan["compare"] else "Optimised"
        c = plan["compare"][strat]
        pred = [{"h": int(t[11:13]), "room": room, "kwh": el} for t, room, el in zip(c["t"], c["room"], c["el"]) if t[:10] == d.isoformat()]
    pred_grid = None
    if plan and plan.get("strategies", {}).get(plan.get("chosen")):
        pred_grid = round(plan["strategies"][plan["chosen"]]["grid_cost"], 2)
    return {"date": d.isoformat(), "actual": act, "predicted": pred, "strategy": strat, "grid": grid,
            "predicted_window_grid_cost": pred_grid}


def live_today(tz):
    """Today's measured room temperature and setpoint so far, straight from HA history (cached 5 min).
    The 30-minute store is only filled at 22:00 and 03:30, so today isn't in it yet."""
    key = dt.date.today().isoformat()
    if LIVE_CACHE.get("key") == key and time.time() - LIVE_CACHE.get("at", 0) < 300:
        return LIVE_CACHE["body"]
    try:
        ha = _ha()
    except KeyError:
        return None
    cfg = data.load_config()
    start = dt.datetime.combine(dt.date.today(), dt.time(0), tz)
    end = dt.datetime.now(tz)
    out = {}
    try:
        for k in ("room", "target"):
            pts = [(dt.datetime.fromisoformat(t).astimezone(tz), float(v)) for t, v in
                   ha.history(cfg["entities"][k], start.isoformat(), end.isoformat()) if _isnum(v)]
            hours = {}
            for t, v in pts:
                hours.setdefault(t.hour, []).append(v)
            out[k] = {h: round(sum(v) / len(v), 2) for h, v in hours.items()}
    except Exception:
        return None
    act = [{"h": h, "room": out["room"].get(h), "target": out.get("target", {}).get(h), "kwh": 0.0, "grid_kwh": None}
           for h in sorted(out["room"])]
    body = {"date": key, "actual": act, "predicted": None, "strategy": None, "grid": None, "live": True}
    LIVE_CACHE.update({"key": key, "at": time.time(), "body": body})
    return body


def _isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def public_config():
    cfg = data.load_config()
    return {k: cfg[k] for k in ("schedule_entity", "tariff", "battery", "house_load_kw", "comfort", "occupancy", "fire", "blocks")}


def recent_log(n=40):
    try:
        with open(data.path("log.jsonl")) as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for l in lines:
        try:
            j = json.loads(l)
            j.pop("tb", None)
            out.append(j)
        except ValueError:
            pass
    return out


def do_simulate(body):
    plan = data.load_json("plan_latest.json", None)
    if not plan:
        return 404, json.dumps({"error": "no plan yet"}), "application/json"
    try:
        cfg = data.load_config()
        if body.get("date"):   # any editable day: plan day, rest of today, or a week-ahead day
            return 200, json.dumps(planner.simulate_for_date(cfg, plan, body["date"], body.get("values"), body.get("offsets"))), "application/json"
        return 200, json.dumps(planner.simulate_values(cfg, plan, body.get("values"), body.get("offsets"),
                                                       body.get("today_values"), body.get("today_offsets"))), "application/json"
    except (ValueError, StopIteration) as e:
        return 400, json.dumps({"error": str(e) or "no such day"}), "application/json"


def do_day(date_s):
    plan = data.load_json("plan_latest.json", None)
    if not plan:
        return 404, json.dumps({"error": "no plan yet"}), "application/json"
    try:
        return 200, json.dumps(planner.day_view(data.load_config(), plan, date_s)), "application/json"
    except (ValueError, StopIteration) as e:
        return 400, json.dumps({"error": str(e) or "no such day"}), "application/json"


def _log(event, **kw):
    with open(data.path("log.jsonl"), "a") as f:
        f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "event": event, **kw}) + "\n")


def do_restore(body):
    plan = data.load_json("plan_latest.json", None)
    if not plan:
        return 404, json.dumps({"error": "no plan yet"}), "application/json"
    d = body.get("date")
    try:
        if d and planner.day_kind(plan, d) == "outlook":
            pushed, blocks = planner.restore_future(_ha(), data.load_config(), plan, d)
            _log("manual_undo_future", date=d, verified=pushed["verified"])
            return 200, json.dumps({"pushed": pushed, "blocks": blocks}), "application/json"
        new = planner.restore_plan(_ha(), data.load_config(), plan)
    except (ValueError, StopIteration) as e:
        return 400, json.dumps({"error": str(e) or "no such day"}), "application/json"
    STATUS_CACHE["at"] = 0
    try:
        planner.publish_sensors(_ha(), data.load_config(), new)
    except Exception:
        pass
    with open(data.path("log.jsonl"), "a") as f:
        f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "event": "manual_undo",
                            "for_date": new["for_date"], "restored": new["chosen"], "verified": new["pushed"]["verified"]}) + "\n")
    return 200, json.dumps({"pushed": new["pushed"], "blocks": new["blocks"], "chosen": new["chosen"]}), "application/json"


def do_push(body):
    plan = data.load_json("plan_latest.json", None)
    if not plan:
        return 404, json.dumps({"error": "no plan yet"}), "application/json"
    with RUN_LOCK:
        if RUN["state"] == "running":
            return 409, json.dumps({"error": "the model is re-running; try again in a minute"}), "application/json"
    d = body.get("date")
    try:
        kind = planner.day_kind(plan, d) if d else None
    except (ValueError, StopIteration) as e:
        return 400, json.dumps({"error": str(e) or "no such day"}), "application/json"
    if kind == "outlook":
        try:
            pushed, blocks = planner.push_future(_ha(), data.load_config(), plan, d, body.get("values"), body.get("offsets"))
        except ValueError as e:
            return 400, json.dumps({"error": str(e)}), "application/json"
        _log("manual_push_future", date=d, blocks=blocks, verified=pushed["verified"])
        return 200, json.dumps({"pushed": pushed, "blocks": blocks}), "application/json"
    if kind == "today_tail":
        body = {"day": "today", "today_values": body.get("values"), "today_offsets": body.get("offsets")}
    if body.get("day") == "today":
        try:
            new, pushed = planner.push_today(_ha(), data.load_config(), plan, body.get("today_values"), body.get("today_offsets"))
        except ValueError as e:
            return 400, json.dumps({"error": str(e)}), "application/json"
        STATUS_CACHE["at"] = 0
        with open(data.path("log.jsonl"), "a") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "event": "manual_push_today",
                                "blocks": new["today_edit"]["blocks"], "verified": pushed["verified"]}) + "\n")
        return 200, json.dumps({"pushed": pushed, "blocks": new["today_edit"]["blocks"]}), "application/json"
    try:
        new = planner.push_values(_ha(), data.load_config(), plan, body.get("values"), body.get("offsets"))
    except ValueError as e:
        return 400, json.dumps({"error": str(e)}), "application/json"
    STATUS_CACHE["at"] = 0
    try:
        planner.publish_sensors(_ha(), data.load_config(), new)
    except Exception:
        pass
    with open(data.path("log.jsonl"), "a") as f:
        f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "event": "manual_push",
                            "for_date": new["for_date"], "blocks": new["blocks"], "verified": new["pushed"]["verified"]}) + "\n")
    return 200, json.dumps({"pushed": new["pushed"], "blocks": new["blocks"]}), "application/json"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def static(self, name):
        fp = os.path.realpath(os.path.join(STATIC, name))
        if not fp.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(fp):
            return self.send(404, "not found", "text/plain")
        with open(fp, "rb") as f:
            return self.send(200, f.read(), CTYPES.get(os.path.splitext(fp)[1], "application/octet-stream"))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.static("index.html")
            if p.startswith("/static/"):
                return self.static(p[len("/static/"):])
            if p in ("/sw.js", "/manifest.webmanifest"):   # served from the root so the worker can control "/"
                return self.static(p[1:])
            if p == "/classic":
                return self.send(200, page(), "text/html; charset=utf-8")
            if p == "/api/status":
                return self.send(200, json.dumps(live_status()), "application/json")
            if p == "/api/actuals":
                from urllib.parse import parse_qs
                return self.send(200, json.dumps(actuals((parse_qs(u.query).get("date") or [None])[0])), "application/json")
            if p == "/api/config":
                return self.send(200, json.dumps(public_config()), "application/json")
            if p == "/api/log":
                return self.send(200, json.dumps(recent_log()), "application/json")
            if p == "/api/settings":
                return self.send(200, json.dumps(settings.get_settings()), "application/json")
            if p == "/api/revisions":
                from urllib.parse import parse_qs
                return self.send(200, json.dumps(revisions.for_date((parse_qs(u.query).get("date") or [dt.date.today().isoformat()])[0])), "application/json")
            if p == "/api/chat":
                from urllib.parse import parse_qs
                since = int((parse_qs(u.query).get("since") or ["0"])[0] or 0)
                return self.send(200, json.dumps({"messages": agent.chat_since(since), "agent": agent.status()}), "application/json")
            if p.startswith("/api/agent/"):
                return self.send(*self.agent_get(p, u))
            if p == "/api/plan":
                plan = data.load_json("plan_latest.json", {})
                if plan.get("outlook"):
                    planner.annotate_outlook(data.load_config(), plan)
                return self.send(200, json.dumps(plan), "application/json")
            if p == "/api/day":
                from urllib.parse import parse_qs
                return self.send(*do_day((parse_qs(u.query).get("date") or [""])[0]))
            m = {"/api/params": "params.json", "/api/plans": "plans_index.json"}
            if p in m:
                return self.send(200, json.dumps(data.load_json(m[p], {})), "application/json")
            if p == "/api/rerun-status":
                return self.send(200, json.dumps(RUN), "application/json")
            if p == "/healthz":
                return self.send(200, "ok", "text/plain")
            return self.send(404, "not found", "text/plain")
        except Exception as e:  # pragma: no cover
            return self.send(500, "error: %s" % e, "text/plain")


    # ---------------- agent API (Hermes) ----------------
    def agent_ok(self):
        k = AGENT_KEY or WRITE_KEY
        return not k or self.headers.get("X-Agent-Key", "") == k

    def agent_get(self, p, u):
        if not self.agent_ok():
            return 403, json.dumps({"error": "agent key required"}), "application/json"
        from urllib.parse import parse_qs
        q = parse_qs(u.query)
        plan = data.load_json("plan_latest.json", None)
        agent.touch("seen")
        if p == "/api/agent/state":
            if not plan:
                return 404, json.dumps({"error": "no plan yet"}), "application/json"
            return 200, json.dumps(agent.state(_ha_or_none(), data.load_config(), plan, live=live_status())), "application/json"
        if p == "/api/agent/inbox":
            since = int((q.get("since") or [agent.status_cursor()])[0] or 0)
            msgs = agent.chat_since(since, "you")
            if msgs and not q.get("peek"):
                agent.mark_read(msgs[-1]["id"])
            return 200, json.dumps({"messages": msgs, "cursor": msgs[-1]["id"] if msgs else since}), "application/json"
        return 404, json.dumps({"error": "unknown agent endpoint"}), "application/json"

    def agent_post(self, p, body):
        if not self.agent_ok():
            return 403, json.dumps({"error": "agent key required"}), "application/json"
        cfg = data.load_config()
        agent.touch("seen")
        if p == "/api/agent/message":
            text = str(body.get("text", "")).strip()
            if not text:
                return 400, json.dumps({"error": "empty message"}), "application/json"
            kind = body.get("kind") if body.get("kind") in ("message", "update") else "message"
            if body.get("read_upto"):
                agent.mark_read(body["read_upto"])
            return 200, json.dumps(agent.chat_add("hermes", text, kind=kind, meta={"title": body.get("title")} if body.get("title") else None)), "application/json"
        plan = data.load_json("plan_latest.json", None)
        if not plan:
            return 404, json.dumps({"error": "no plan yet"}), "application/json"
        try:
            if p == "/api/agent/project":
                return 200, json.dumps(agent.project_today(_ha_or_none(), cfg, plan, body.get("values"), body.get("offsets"))), "application/json"
            if p == "/api/agent/suggest":
                return 200, json.dumps(agent.suggest_today(_ha_or_none(), cfg, plan)), "application/json"
            if p == "/api/agent/adjust":
                with RUN_LOCK:
                    if RUN["state"] == "running":
                        return 409, json.dumps({"error": "the model is re-running; try again in a minute"}), "application/json"
                res = agent.adjust(_ha(), cfg, plan, body.get("values"), body.get("offsets"), body.get("reason", ""),
                                   post_update=body.get("post_update", True))
                STATUS_CACHE["at"] = 0
                _log("agent_adjust", reason=body.get("reason", "")[:200], revision=(res["revision"] or {}).get("id"))
                return 200, json.dumps(res), "application/json"
        except (ValueError, StopIteration, KeyError) as e:
            return 400, json.dumps({"error": str(e) or "bad request"}), "application/json"
        return 404, json.dumps({"error": "unknown agent endpoint"}), "application/json"

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/simulate":  # read-only what-if
                return self.send(*do_simulate(body))
            if p.startswith("/api/agent/"):
                return self.send(*self.agent_post(p, body))
            if p == "/api/settings/preview":   # read-only: re-score the plan under proposed settings
                try:
                    return self.send(200, json.dumps(settings.preview(body.get("changes"), body.get("resets") or [])), "application/json")
                except (ValueError, KeyError) as e:
                    return self.send(400, json.dumps({"error": str(e)}), "application/json")
            if WRITE_KEY and self.headers.get("X-Planner-Key", "") != WRITE_KEY:
                return self.send(403, json.dumps({"error": "write key required", "need_key": True}), "application/json")
            if p == "/api/settings":
                try:
                    res = settings.save(body.get("changes"), body.get("resets") or [])
                except ValueError as e:
                    return self.send(400, json.dumps({"error": str(e)}), "application/json")
                _log("settings_saved", changes=list((body.get("changes") or {}).keys()), reset=body.get("resets") or [])
                if body.get("replan") or res["needs_replan"]:
                    code, out, _ = start_rerun()
                    res["replan"] = json.loads(out)
                return self.send(200, json.dumps(res), "application/json")
            if p == "/api/chat":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self.send(400, json.dumps({"error": "empty message"}), "application/json")
                m = agent.chat_add("you", text)
                cfg = data.load_config()
                agent.notify(cfg, {"type": "chat", "message": m, "state_url": "/api/agent/state", "reply_url": "/api/agent/message"})
                return self.send(200, json.dumps(m), "application/json")
            if p == "/api/push":
                return self.send(*do_push(body))
            if p == "/api/restore":
                return self.send(*do_restore(body))
            if p == "/api/occupancy":
                return self.send(*save_occupancy(body))
            if p == "/api/rerun":
                return self.send(*start_rerun())
            return self.send(404, "not found", "text/plain")
        except Exception as e:
            return self.send(400, "bad request: %s" % e, "text/plain")


def _ha_or_none():
    try:
        return _ha()
    except KeyError:
        return None


def main():
    threading.Thread(target=agent.checkin_loop, args=(data.load_config,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
