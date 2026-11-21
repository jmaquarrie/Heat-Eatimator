/* Heat Planner dashboard. Vanilla JS, no dependencies; charts are hand-built SVG. */
"use strict";

const $ = (s) => document.querySelector(s);
const S = { plan: null, params: null, idx: [], cfg: null, status: null, log: [], edit: null, editRes: null, actuals: null };
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WDL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const NAMES = {
  "Optimised": "Optimised",
  "Current routine": "Your routine",
  "Steady comfort temp (battery covers gaps)": "Steady 20°",
  "Cosy windows only (floors off-peak)": "Cosy windows only",
  "Your edit": "Your edit",
};
// colour follows the entity everywhere on the page
const COLOR = {
  "Optimised": "var(--plan)", "Your edit": "var(--edit)", "Steady comfort temp (battery covers gaps)": "var(--s3)",
  "Cosy windows only (floors off-peak)": "var(--s4)", "Current routine": "var(--muted)",
};
const ORDER = ["Optimised", "Current routine", "Steady comfort temp (battery covers gaps)", "Cosy windows only (floors off-peak)", "Your edit"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gbp = (v) => (v < 0 ? "−£" : "£") + Math.abs(v).toFixed(2);
const deg = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d) + "°");
const hh = (h) => { const m = Math.round(h * 60); return String(Math.floor(m / 60) % 25).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
const rng = (a, b) => hh(a) + "–" + hh(b);
const parseT = (s) => { const [d, t] = s.split("T"); const [Y, M, D] = d.split("-").map(Number); const [h, m] = t.split(":").map(Number); return new Date(Y, M - 1, D, h, m); };
const dateOf = (iso) => { const [Y, M, D] = iso.split("-").map(Number); return new Date(Y, M - 1, D); };
const wdIdx = (d) => (d.getDay() + 6) % 7;
const fmtDate = (d) => WD[wdIdx(d)] + " " + d.getDate() + " " + MON[d.getMonth()];
const isoDate = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function tariff(h) {
  const t = S.cfg.tariff, x = ((h % 24) + 24) % 24;
  if (t.cosy.some(([a, b]) => a <= x && x < b)) return { slot: "cosy", rate: t.rate_cosy, label: "Cosy" };
  if (t.peak.some(([a, b]) => a <= x && x < b)) return { slot: "peak", rate: t.rate_peak, label: "Peak" };
  return { slot: "standard", rate: t.rate_standard, label: "Standard" };
}
const pence = (r) => Math.round(r * 1000) / 10 + "p";

/* ---------------------------------------------------------------- API */
function key() { try { return localStorage.getItem("hp-key") || ""; } catch (e) { return ""; } }
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Planner-Key": key() }, body: JSON.stringify(body) };
  let r = await fetch(path, opt);
  if (r.status === 403 && body !== undefined) {
    const k = prompt("This action needs the planner write key (HEATPLANNER_WRITE_KEY):");
    if (!k) throw new Error("write key needed");
    try { localStorage.setItem("hp-key", k); } catch (e) { /* private mode */ }
    opt.headers["X-Planner-Key"] = k;
    r = await fetch(path, opt);
  }
  const txt = await r.text();
  let j; try { j = JSON.parse(txt); } catch (e) { j = { error: txt }; }
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

async function loadAll() {
  const [plan, params, idx, cfg, log] = await Promise.all(["/api/plan", "/api/params", "/api/plans", "/api/config", "/api/log"].map((p) => api(p).catch(() => null)));
  S.plan = plan && plan.for_date ? plan : null; S.params = params || {}; S.idx = idx || []; S.cfg = cfg; S.log = log || [];
  S.edit = null; S.editRes = null;
  if (S.plan && S.plan.manual && S.plan.chosen === "Your edit") S.editRes = null;
  render();
  loadStatus();
  api("/api/actuals").then((a) => { S.actuals = a; renderActuals(); }).catch(() => {});
}
async function loadStatus() {
  try { S.status = await api("/api/status"); } catch (e) { S.status = { available: false, error: String(e.message) }; }
  renderChips();
}

/* ---------------------------------------------------------------- helpers */
function planValues() { return S.plan.blocks_raw.map((b) => b.temperature); }
function routineValues() { return S.plan.strategies["Current routine"].values; }
function sameVals(a, b) { return a && b && a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-9); }
function editing() { return S.edit && S.editRes && !sameVals(S.edit, planValues()); }
function chosenName() { return S.plan.chosen; }

function seqColor(v) {
  // sequential one-hue ramp for setpoints 16..22
  const lo = css("--seq-lo"), hi = css("--seq-hi");
  const t = Math.max(0, Math.min(1, (v - 16) / 6));
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const a = p(lo), b = p(hi);
  const c = a.map((x, i) => Math.round(x + (b[i] - x) * t));
  const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const lite = dark ? t > 0.55 : t < 0.45;
  return { bg: "rgb(" + c.join(",") + ")", lite };
}
function strip(blocks, labels = true) {
  return '<div class="strip" role="img" aria-label="' + esc(blocks.map((b) => rng(b.from, b.to) + " " + b.temperature + "°").join(", ")) + '">' +
    blocks.map((b) => { const c = seqColor(b.temperature); return '<div class="' + (c.lite ? "lite" : "") + '" style="flex:' + (b.to - b.from) + ";background:" + c.bg + '" title="' + rng(b.from, b.to) + " " + b.temperature + '°">' + (labels && b.to - b.from >= 3 ? b.temperature : "") + "</div>"; }).join("") + "</div>";
}
function tidy(values) {
  const out = [];
  S.cfg.blocks.forEach(([a, b], i) => { const v = values[i]; if (out.length && out[out.length - 1].temperature === v) out[out.length - 1].to = b; else out.push({ from: a, to: b, temperature: v }); });
  return out;
}

/** Plain-English reasons a strategy differs from the routine. */
function explain(vals, base) {
  const parts = [];
  S.cfg.blocks.forEach(([a, b], i) => {
    const d = vals[i] - base[i];
    if (Math.abs(d) < 0.25) return;
    const t = tariff(a);
    const r = rng(a, b);
    const w = vals[i] + "° instead of " + base[i] + "° " + r;
    if (d > 0) parts.push((t.slot === "cosy" ? "pre-heats to " + w + " while power is cheap" : "runs warmer at " + w));
    else parts.push((t.slot === "peak" ? "eases off to " + w + " through the peak" : "runs cooler at " + w));
  });
  return parts;
}

/* ---------------------------------------------------------------- render: all */
function render() {
  $("#loading").hidden = !!S.plan;
  if (!S.plan) { $("#loading").textContent = "No plan yet. The first one is made at 22:00, or run “heatplanner.cli plan”."; return; }
  document.querySelectorAll(".sec").forEach((s) => (s.hidden = false));
  renderHero(); renderTimeline(); renderEditor(); renderStrategies(); renderWeek(); renderOcc(); renderModel(); renderHistory(); renderChips();
  $("#foot-entity").textContent = S.cfg.schedule_entity;
}

/* ---------------------------------------------------------------- chips */
function chip(cls, html, title) { return '<span class="chip ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : "") + '><span class="dot" aria-hidden="true"></span><span>' + html + "</span></span>"; }
function renderChips() {
  if (!S.plan || !S.cfg) return;
  const out = [];
  const ps = S.plan.pushed;
  if (ps) out.push(ps.verified ? chip("good", "Plan pushed <b>" + esc(ps.at.slice(11, 16)) + "</b> · verified", "Written to " + ps.entity + " and read back") : chip("crit", "Push <b>not verified</b>", "The helper did not read back the same blocks"));
  else out.push(chip("warn", "Preview only, <b>not pushed</b>"));
  const st = S.status;
  if (!st) out.push(chip("", "Checking Home Assistant…"));
  else if (!st.available) out.push(chip("warn", "Live data unavailable", st.error));
  else {
    if (st.following === true) out.push(chip("good", "Heat pump <b>following plan</b>", "Thermostat setpoint matches the planner helper"));
    else if (st.following === false) out.push(chip("warn", "Heat pump at <b>" + deg(st.climate.setpoint) + "</b>, plan says <b>" + deg(st.helper.temperature) + "</b>", "Expected until the HA automation linking the helper to the thermostat is in place"));
    else out.push(chip("", "Pump link: not set up yet"));
    if (st.api_budget != null) out.push(chip(st.api_budget < 15 ? "warn" : "", "Daikin API left <b>" + Math.round(st.api_budget) + "</b>", "Daikin cloud calls left today (about 200 a day)"));
    if (st.room != null) out.push(chip("", "Room <b>" + deg(st.room) + "</b>"));
    if (st.outdoor != null) out.push(chip("", "Outside <b>" + deg(st.outdoor) + "</b>"));
    if (st.battery_pct != null) out.push(chip("", "Battery <b>" + Math.round(st.battery_pct) + "%</b>"));
    if (st.rate != null) { const t = st.rate < 0.2 ? "Cosy" : st.rate > 0.35 ? "Peak" : "Standard"; out.push(chip(t === "Peak" ? "crit" : t === "Cosy" ? "good" : "", t + " <b>" + pence(st.rate) + "</b>")); }
    if (st.heating) out.push(chip(st.heating === "on" ? "good" : "", "Heating <b>" + esc(st.heating) + "</b>"));
  }
  $("#chips").innerHTML = out.join("");
}

/* ---------------------------------------------------------------- hero */
function renderHero() {
  const p = S.plan, d = dateOf(p.for_date);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const rel = Math.round((d - today) / 864e5);
  const when = rel === 0 ? "Today" : rel === 1 ? "Tomorrow" : rel === -1 ? "Yesterday" : WDL[wdIdx(d)];
  const ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const fc = p.forecast_tomorrow;
  $("#hero-eyebrow").innerHTML = "<span>" + when + " · " + fmtDate(d) + "</span>" + '<span class="tag">' + deg(fc.min, 0) + " to " + deg(fc.max, 0) + " outside</span>" +
    '<span class="tag">' + ({ home: "Home all day", away_day: "Out " + rng(S.cfg.occupancy.away_from, S.cfg.occupancy.away_to), away_all: "Away all day" }[p.occupancy?.status_tomorrow] || "") + "</span>";
  const saving = p.saving_vs_current_gbp;
  let title, why;
  if (p.chosen === "Current routine") { title = "Your routine is already the best plan"; why = "Nothing beat it by more than 2p, so the planner kept your usual setpoints."; }
  else if (p.chosen === "Your edit") { title = "Running your edited plan"; why = "Pushed by hand at " + esc((p.manual?.at || "").slice(11, 16)) + ". Tonight's 22:00 run plans the next day as normal."; }
  else {
    const money = cur.grid_cost - ch.grid_cost, comfort = cur.penalty - ch.penalty;
    if (money >= 0.01) title = "Saves about " + gbp(money) + " on electricity" + (comfort > 0.05 ? " and keeps you warmer" : "");
    else if (comfort > 0.05) title = "Keeps you warmer than your routine for " + gbp(-money) + " more";
    else title = "Scores " + gbp(saving) + " better than your routine";
    const ex = explain(ch.values, cur.values);
    why = ex.length ? "It " + (ex.length > 1 ? ex.slice(0, Math.min(3, ex.length) - 1).join(", ") + " and " + ex[Math.min(3, ex.length) - 1] : ex[0]) + "." : "";
    if (comfort > 0.05) why += " Your routine would let the room fall below the comfort floor while you're home (priced at " + gbp(cur.penalty) + ").";
  }
  $("#hero-title").textContent = title;
  $("#hero-why").innerHTML = why;
  const tiles = [
    ["Electricity cost", gbp(ch.grid_cost), "Routine " + gbp(cur.grid_cost) + " · incl. house load"],
    ["Pump electricity", ch.kwh_hp.toFixed(1) + " kWh", "Routine " + cur.kwh_hp.toFixed(1) + " kWh"],
    ["Coldest while awake", deg(ch.t_min_awake), "Comfort floor " + deg(S.cfg.comfort.soft_floor_c)],
    ["Comfort shortfall", gbp(ch.penalty), ch.penalty < 0.05 ? "Floors held" : "Priced time below the floors"],
  ];
  $("#hero-tiles").innerHTML = tiles.map(([k, v, d]) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + d + "</div></div>").join("");
}

/* ---------------------------------------------------------------- timeline (small multiples, shared x) */
function renderTimeline() {
  const box = $("#timeline");
  const p = S.plan, rows = p.trace_best || [];
  if (!rows.length) { box.innerHTML = '<p class="foot">No trace stored with this plan.</p>'; return; }
  const cur = p.trace_current || [];
  const ed = editing() ? S.editRes.trace : null;
  const n = rows.length;
  const W = Math.max(320, box.clientWidth || 900);
  const narrow = W < 600;
  const L = narrow ? 34 : 42, R = narrow ? 50 : 64;
  const lanes = [
    { id: "room", title: "Room °C", h: narrow ? 190 : 250 },
    { id: "out", title: "Outside °C", h: 58 },
    { id: "el", title: "Pump kWh per hour", h: 70 },
    { id: "soc", title: "Battery kWh", h: 56 },
  ];
  const RIB = 20, GAP = 22, XA = 22;
  let y = RIB + 6;
  lanes.forEach((ln) => { ln.y0 = y + 14; y = ln.y0 + ln.h + GAP; });
  const H = y - GAP + XA;
  const x = (i) => L + ((W - L - R) * i) / n;
  const o = [];
  o.push('<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Predicted room temperature, outside temperature, pump electricity and battery through the plan window">');

  // tariff washes across all lanes + ribbon labels
  let segStart = 0;
  for (let i = 1; i <= n; i++) {
    const prev = tariff(parseT(rows[i - 1].t).getHours());
    const nxt = i < n ? tariff(parseT(rows[i].t).getHours()) : null;
    if (!nxt || nxt.slot !== prev.slot) {
      const x0 = x(segStart), x1 = x(i);
      if (prev.slot !== "standard") o.push('<rect x="' + x0 + '" y="' + 0 + '" width="' + (x1 - x0) + '" height="' + (H - XA) + '" fill="var(--' + prev.slot + ')"/>');
      if (x1 - x0 > 34) o.push('<text x="' + (x0 + 6) + '" y="14" class="ax" style="fill:' + (prev.slot === "standard" ? "var(--muted)" : "var(--" + prev.slot + "-ink)") + ';font-weight:600">' + prev.label + (x1 - x0 > 70 ? " " + pence(prev.rate) : "") + "</text>");
      segStart = i;
    }
  }

  // "now" marker
  const t0 = parseT(rows[0].t), tN = new Date(t0.getTime() + n * 9e5), now = new Date();
  if (now > t0 && now < tN) {
    const xn = x(((now - t0) / 9e5));
    o.push('<line x1="' + xn + '" x2="' + xn + '" y1="' + RIB + '" y2="' + (H - XA) + '" stroke="var(--ink)" stroke-width="1" stroke-dasharray="2 3" opacity=".5"/><text x="' + (xn + 4) + '" y="' + (RIB + 12) + '" class="ax" style="fill:var(--ink)">now</text>');
  }

  const laneScale = (ln, lo, hi) => (v) => ln.y0 + ln.h * (1 - (v - lo) / (hi - lo));
  const gridY = (ln, lo, hi, step, fmt) => {
    const Y = laneScale(ln, lo, hi);
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) o.push('<line class="gl" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + fmt(v) + "</text>");
    o.push('<text class="ax-lane" x="' + L + '" y="' + (ln.y0 - 6) + '">' + ln.title + "</text>");
    return Y;
  };
  const path = (vals, X, Y) => vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1)).join("");
  const stepPath = (vals, Y) => vals.map((v, i) => (i ? "L" + x(i).toFixed(1) + "," + Y(v).toFixed(1) : "M" + x(0) + "," + Y(v).toFixed(1)) + "L" + x(i + 1).toFixed(1) + "," + Y(v).toFixed(1)).join("");

  // ---- room lane
  const c = S.cfg.comfort;
  const floorOf = (cls) => (cls === "sleep" ? c.sleep_floor_c : cls === "away" ? c.away_floor_c : c.soft_floor_c);
  const roomPts = (tr) => [p.t_room0].concat(tr.map((r) => r.room));
  const plan = roomPts(rows), rout = cur.length ? roomPts(cur) : null, edt = ed ? roomPts(ed) : null;
  const allR = plan.concat(rout || [], edt || [], rows.map((r) => r.target), rows.map((r) => floorOf(r.cls)), (ed || []).map((r) => r.target));
  const rlo = Math.floor(Math.min(...allR) - 0.3), rhi = Math.ceil(Math.max(...allR) + 0.3);
  const RL = lanes[0];
  const Yr = gridY(RL, rlo, rhi, rhi - rlo > 6 ? 2 : 1, (v) => v + "°");
  // below-floor wash
  o.push('<path d="' + rows.map((r, i) => (i ? "L" : "M") + x(i) + "," + Yr(floorOf(r.cls)) + "L" + x(i + 1) + "," + Yr(floorOf(r.cls))).join("") + "L" + x(n) + "," + (RL.y0 + RL.h) + "L" + x(0) + "," + (RL.y0 + RL.h) + 'Z" fill="var(--below)"/>');
  o.push('<path d="' + rows.map((r, i) => (i ? "L" : "M") + x(i) + "," + Yr(floorOf(r.cls)) + "L" + x(i + 1) + "," + Yr(floorOf(r.cls))).join("") + '" fill="none" stroke="var(--muted)" stroke-width="1"/>');
  const Xr = (i) => x(i);
  o.push('<path d="' + stepPath(rows.map((r) => r.target), Yr) + '" fill="none" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"/>');
  if (ed) o.push('<path d="' + stepPath(ed.map((r) => r.target), Yr) + '" fill="none" stroke="var(--edit)" stroke-width="1.5" stroke-dasharray="5 4" opacity=".8"/>');
  if (rout) o.push('<path d="' + path(rout, Xr, Yr) + '" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="2 3"/>');
  o.push('<path d="' + path(plan, Xr, Yr) + '" fill="none" stroke="var(--plan)" stroke-width="2.25" stroke-linejoin="round"/>');
  if (edt) o.push('<path d="' + path(edt, Xr, Yr) + '" fill="none" stroke="var(--edit)" stroke-width="2.25" stroke-linejoin="round"/>');
  // direct labels at the right end, nudged apart
  const dls = [{ t: "Plan", v: plan[n], col: "var(--plan)" }];
  if (rout && p.chosen !== "Current routine") dls.push({ t: "Routine", v: rout[n], col: "var(--muted)" });
  if (edt) dls.push({ t: "Edit", v: edt[n], col: "var(--edit)" });
  dls.forEach((d) => (d.y = Yr(d.v)));
  dls.sort((a, b) => a.y - b.y);
  for (let i = 1; i < dls.length; i++) if (dls[i].y - dls[i - 1].y < 13) dls[i].y = dls[i - 1].y + 13;
  dls.forEach((d) => o.push('<circle cx="' + x(n) + '" cy="' + Yr(d.v) + '" r="3" fill="' + d.col + '" stroke="var(--surface)" stroke-width="2"/><text class="dl" x="' + (x(n) + 7) + '" y="' + (d.y + 4) + '">' + d.t + "</text>"));

  // ---- outside lane
  const OL = lanes[1];
  const outs = rows.map((r) => r.outdoor);
  const olo = Math.floor(Math.min(...outs) - 0.5), ohi = Math.ceil(Math.max(...outs) + 0.5);
  const ostep = Math.max(1, Math.ceil((ohi - olo) / 2));
  const Yo = gridY(OL, olo, ohi, ostep, (v) => v + "°");
  if (olo < 0 && ohi > 0) o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Yo(0) + '" y2="' + Yo(0) + '"/>');
  o.push('<path d="' + path(outs, (i) => x(i + 0.5), Yo) + '" fill="none" stroke="var(--ink-2)" stroke-width="2" stroke-linecap="round"/>');

  // ---- electricity lane: hourly bars
  const EL = lanes[2];
  const hourly = (tr) => { const a = []; for (let i = 0; i < tr.length; i += 4) a.push(tr.slice(i, i + 4).reduce((s, r) => s + r.el_kw * 0.25, 0)); return a; };
  const hp = hourly(rows), he = ed ? hourly(ed) : null;
  const ehi = Math.max(0.5, Math.ceil(Math.max(...hp, ...(he || [0])) * 2) / 2);
  const Ye = gridY(EL, 0, ehi, ehi > 2 ? 1 : 0.5, (v) => v);
  o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Ye(0) + '" y2="' + Ye(0) + '"/>');
  const bw = x(4) - x(0);
  const bar = (x0, w, v, col) => { if (v <= 0.005) return ""; const y0 = Ye(v), h = Ye(0) - y0, r = Math.min(3, w / 2, h); return '<path d="M' + x0 + "," + Ye(0) + "V" + (y0 + r) + "Q" + x0 + "," + y0 + " " + (x0 + r) + "," + y0 + "H" + (x0 + w - r) + "Q" + (x0 + w) + "," + y0 + " " + (x0 + w) + "," + (y0 + r) + "V" + Ye(0) + 'Z" fill="' + col + '"/>'; };
  hp.forEach((v, k) => {
    const x0 = x(k * 4) + 1;
    if (he) { const w = (bw - 4) / 2; o.push(bar(x0, w, v, "var(--plan)") + bar(x0 + w + 2, w, he[k], "var(--edit)")); }
    else o.push(bar(x0, bw - 2, v, "var(--plan)"));
  });

  // ---- battery lane
  const BL = lanes[3];
  const cap = S.cfg.battery.capacity_kwh;
  const Yb = gridY(BL, 0, cap, cap / 2, (v) => v);
  const socs = [p.soc0_kwh].concat(rows.map((r) => r.soc));
  o.push('<path d="' + path(socs, Xr, Yb) + "L" + x(n) + "," + Yb(0) + "L" + x(0) + "," + Yb(0) + 'Z" fill="var(--plan)" opacity=".14"/>');
  o.push('<path d="' + path(socs, Xr, Yb) + '" fill="none" stroke="var(--plan)" stroke-width="2"/>');
  if (ed) o.push('<path d="' + path([p.soc0_kwh].concat(ed.map((r) => r.soc)), Xr, Yb) + '" fill="none" stroke="var(--edit)" stroke-width="2"/>');
  o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Yb(0) + '" y2="' + Yb(0) + '"/>');

  // ---- x axis
  const every = narrow ? 4 : 2;
  rows.forEach((r, i) => {
    const t = parseT(r.t);
    if (t.getMinutes() !== 0 || t.getHours() % every !== 0) return;
    const mid = t.getHours() === 0;
    o.push('<line x1="' + x(i) + '" x2="' + x(i) + '" y1="' + (H - XA) + '" y2="' + (H - XA + 4) + '" stroke="var(--axis)"/>');
    o.push('<text class="ax" x="' + x(i) + '" y="' + (H - 6) + '" text-anchor="middle"' + (mid ? ' style="fill:var(--ink);font-weight:600"' : "") + ">" + (mid ? WD[wdIdx(t)] : String(t.getHours()).padStart(2, "0")) + "</text>");
    if (mid) o.push('<line x1="' + x(i) + '" x2="' + x(i) + '" y1="' + RIB + '" y2="' + (H - XA) + '" stroke="var(--axis)" stroke-width="1"/>');
  });
  o.push('<line id="tl-cross" x1="0" x2="0" y1="' + RIB + '" y2="' + (H - XA) + '" stroke="var(--ink)" stroke-width="1" visibility="hidden"/>');
  o.push('<rect id="tl-hit" x="' + L + '" y="0" width="' + (W - L - R) + '" height="' + H + '" fill="transparent"/>');
  o.push("</svg>");
  box.innerHTML = o.join("");

  // legend
  const lg = ['<span><i class="sw" style="border-color:var(--plan)"></i>' + esc(chosenLabel()) + " plan</span>"];
  if (rout && p.chosen !== "Current routine") lg.push('<span><i class="sw dot" style="border-color:var(--muted)"></i>Your routine</span>');
  if (edt) lg.push('<span><i class="sw" style="border-color:var(--edit)"></i>Your edit</span>');
  lg.push('<span><i class="sw dash" style="border-color:var(--ink-2)"></i>Setpoint</span>', '<span><i class="sw box" style="background:var(--below)"></i>Below comfort floor</span>', '<span><i class="sw box" style="background:var(--cosy)"></i>Cosy</span>', '<span><i class="sw box" style="background:var(--peak)"></i>Peak</span>');
  $("#tl-legend").innerHTML = lg.join("");
  $("#tl-foot").textContent = "From " + fmtDate(t0) + " " + hh(t0.getHours() + t0.getMinutes() / 60) + " to " + fmtDate(tN) + " " + hh(tN.getHours()) + ". Starting room " + deg(p.t_room0) + ", battery " + p.soc0_kwh + " kWh. The battery charges in Cosy windows and covers the house and pump at other times.";

  // hover
  const hit = $("#tl-hit"), cross = $("#tl-cross"), tip = $("#tip");
  const show = (ev) => {
    const rect = box.querySelector("svg").getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const i = Math.max(0, Math.min(n - 1, Math.floor(((px - L) / (W - L - R)) * n)));
    const r = rows[i], t = parseT(r.t), tf = tariff(t.getHours());
    cross.setAttribute("x1", x(i + 1)); cross.setAttribute("x2", x(i + 1)); cross.setAttribute("visibility", "visible");
    const cls = { home: "Home, awake", sleep: "Asleep", away: "Out" }[r.cls] || r.cls;
    const row = (col, k, v) => '<div class="tr"><span>' + (col ? '<i style="background:' + col + '"></i>' : "") + k + "</span><b>" + v + "</b></div>";
    let h = '<div class="th"><span>' + WD[wdIdx(t)] + " " + hh(t.getHours() + t.getMinutes() / 60) + '</span><span class="tag ' + tf.slot + '">' + tf.label + " " + pence(tf.rate) + "</span></div>";
    h += row("", "Occupancy", cls) + row("", "Setpoint", deg(r.target));
    h += row("var(--plan)", "Room (plan)", deg(r.room, 2));
    if (cur[i] && p.chosen !== "Current routine") h += row("var(--muted)", "Room (routine)", deg(cur[i].room, 2));
    if (ed) h += row("var(--edit)", "Room (edit)", deg(ed[i].room, 2) + (ed[i].target !== r.target ? " @" + ed[i].target + "°" : ""));
    h += row("", "Outside", deg(r.outdoor)) + row("", "Pump", r.el_kw.toFixed(2) + " kW") + row("", "Battery", r.soc.toFixed(1) + " kWh");
    tip.innerHTML = h; tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let lx = ev.clientX + 14; if (lx + tw > innerWidth - 8) lx = ev.clientX - tw - 14;
    tip.style.left = Math.max(8, lx) + "px"; tip.style.top = Math.max(8, Math.min(innerHeight - th - 8, ev.clientY - th / 2)) + "px";
  };
  const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  hit.addEventListener("pointermove", show); hit.addEventListener("pointerdown", show); hit.addEventListener("pointerleave", hide);
}
function chosenLabel() { return NAMES[S.plan.chosen] || S.plan.chosen; }

/* ---------------------------------------------------------------- editor */
let simTimer = null, simSeq = 0;
function renderEditor() {
  const p = S.plan, base = planValues(), vals = S.edit || base, c = S.cfg.comfort;
  const d = dateOf(p.for_date);
  $("#edit-sub").innerHTML = "Changes are simulated instantly against tonight's forecast. Nothing is sent to Home Assistant until you press Push. Editing <b>" + fmtDate(d) + "</b>.";
  const ed = $("#editor");
  ed.style.gridTemplateColumns = S.cfg.blocks.map(([a, b]) => Math.max(2.2, b - a) + "fr").join(" ");
  const lo = c.min_setpoint_c, hi = c.max_setpoint_c;
  ed.innerHTML = S.cfg.blocks.map(([a, b], i) => {
    const v = vals[i], t = tariff(a), ch = Math.abs(v - base[i]) > 1e-9;
    return '<div class="blk' + (ch ? " changed" : "") + '"><div class="time">' + rng(a, b) + '</div><span class="tag ' + t.slot + '">' + t.label + "</span>" +
      '<div class="bar-wrap" aria-hidden="true"><div class="bar" style="height:' + (8 + ((v - lo) / (hi - lo)) * 92) + '%"></div></div>' +
      '<div class="val">' + v.toFixed(1) + "<small>°C</small></div>" +
      '<div class="steps"><button type="button" class="step" data-i="' + i + '" data-d="-0.5" aria-label="Lower ' + rng(a, b) + '"' + (v <= lo ? " disabled" : "") + '>−</button><button type="button" class="step" data-i="' + i + '" data-d="0.5" aria-label="Raise ' + rng(a, b) + '"' + (v >= hi ? " disabled" : "") + ">+</button></div>" +
      '<div class="was">' + (ch ? "plan " + base[i].toFixed(1) + "°" : "") + "</div></div>";
  }).join("");
  ed.querySelectorAll(".step").forEach((btn) => btn.addEventListener("click", () => {
    const i = +btn.dataset.i, nv = (S.edit || planValues()).slice();
    nv[i] = Math.round((nv[i] + +btn.dataset.d) * 2) / 2;
    setEdit(nv);
  }));
  renderEdSummary();
}
function setEdit(values) {
  S.edit = values;
  renderEditor();
  clearTimeout(simTimer);
  if (sameVals(values, planValues())) { S.editRes = null; renderTimeline(); renderEdSummary(); return; }
  $("#ed-msg").className = "msg"; $("#ed-msg").textContent = "Simulating…";
  simTimer = setTimeout(async () => {
    const seq = ++simSeq;
    try {
      const r = await api("/api/simulate", { values });
      if (seq !== simSeq) return;
      S.editRes = r; $("#ed-msg").textContent = r.exact ? "" : "Approximate: this plan was made before what-if support, so solar gain is left out.";
    } catch (e) { $("#ed-msg").className = "msg err"; $("#ed-msg").textContent = e.message; }
    renderTimeline(); renderEdSummary();
  }, 220);
}
function renderEdSummary() {
  const p = S.plan, ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const r = editing() ? S.editRes : ch;
  const dv = (a, b, money = true) => { const d = a - b; if (Math.abs(d) < 0.005) return '<span class="d">same</span>'; const good = d < 0; return '<span class="d ' + (good ? "up-good" : "up-bad") + '">' + (good ? "▼ " : "▲ ") + (money ? gbp(Math.abs(d)) : Math.abs(d).toFixed(1)) + (good ? " less" : " more") + "</span>"; };
  const tiles = [
    ["Model score", gbp(r.total), editing() ? dv(r.total, ch.total) + '<span class="d"> vs plan</span>' : '<span class="d">grid + comfort + end state</span>'],
    ["vs your routine", gbp(Math.abs(cur.total - r.total)), '<span class="d">' + (cur.total - r.total >= 0 ? "better overall" : "worse overall") + "</span>"],
    ["Electricity", gbp(r.grid_cost), editing() ? dv(r.grid_cost, ch.grid_cost) : '<span class="d">grid import</span>'],
    ["Pump", r.kwh_hp.toFixed(1) + " kWh", editing() ? dv(r.kwh_hp, ch.kwh_hp, false).replace("less", "kWh less").replace("more", "kWh more") : '<span class="d">heating only</span>'],
    ["Coldest awake", deg(r.t_min_awake), '<span class="d">' + (r.penalty > 0.05 ? "shortfall " + gbp(r.penalty) : "floors held") + "</span>"],
  ];
  $("#ed-summary").innerHTML = tiles.map(([k, v, d]) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + "</div>" + d + "</div>").join("");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const past = dateOf(p.for_date) < today;
  $("#ed-push").disabled = !editing() || past;
  $("#ed-push").title = past ? "This plan's day has passed. Re-run the optimiser first." : editing() ? "" : "Change a setpoint first";
}
function wireEditor() {
  $("#ed-reset").onclick = () => setEdit(planValues());
  $("#ed-routine").onclick = () => setEdit(routineValues().slice());
  $("#ed-rerun").onclick = () => rerun("/api/rerun", {}, $("#ed-msg"));
  $("#ed-push").onclick = async () => {
    const vals = S.edit, p = S.plan;
    const blocks = tidy(vals);
    const body = "<p>This writes <b>" + WDL[wdIdx(dateOf(p.for_date))] + "</b> in <code>" + esc(S.cfg.schedule_entity) + "</code>. Your live schedule is not touched.</p>" +
      strip(blocks) + '<p style="font-variant-numeric:tabular-nums">' + blocks.map((b) => rng(b.from, b.to) + " " + b.temperature.toFixed(1) + "°").join(" · ") + "</p>";
    if (!(await confirmDlg("Push your edited plan?", body, "Push"))) return;
    const m = $("#ed-msg"); m.className = "msg"; m.textContent = "Pushing…";
    try {
      const r = await api("/api/push", { values: vals });
      m.className = "msg " + (r.pushed.verified ? "ok" : "err");
      m.textContent = r.pushed.verified ? "Pushed and verified." : "Pushed, but the read-back did not match.";
      await loadAll();
    } catch (e) { m.className = "msg err"; m.textContent = e.message; }
  };
}

/* ---------------------------------------------------------------- strategies */
function sparkline(vals, col, lo, hi, W = 240, H = 44) {
  const X = (i) => 2 + ((W - 4) * i) / (vals.length - 1), Y = (v) => 4 + (H - 8) * (1 - (v - lo) / (hi - lo));
  return '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" aria-hidden="true"><line class="gl" x1="0" x2="' + W + '" y1="' + Y(S.cfg.comfort.soft_floor_c) + '" y2="' + Y(S.cfg.comfort.soft_floor_c) + '" stroke-dasharray="3 3"/><path d="' + vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1)).join("") + '" fill="none" stroke="' + col + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
}
function renderStrategies() {
  const p = S.plan, cur = p.strategies["Current routine"], cmp = p.compare || {};
  const t0 = parseT(p.start || p.trace_best[0].t);
  $("#st-window").textContent = fmtDate(t0) + " " + hh(t0.getHours()) + " to 04:00 two days on (about " + Math.round((p.trace_best || []).length / 4) + " h)";
  const names = ORDER.filter((n) => p.strategies[n]);
  const all = names.flatMap((n) => (cmp[n] ? cmp[n].room : []));
  const lo = Math.min(...all, S.cfg.comfort.soft_floor_c) - 0.2, hi = Math.max(...all) + 0.2;
  $("#st-cards").innerHTML = names.map((n) => {
    const r = p.strategies[n], d = cur.total - r.total, chosen = n === p.chosen;
    const ex = n === "Current routine" ? ["the setpoints from your live schedule"] : explain(r.values, cur.values);
    const delta = n === "Current routine" ? '<span class="delta" style="color:var(--muted)">Baseline</span>' : '<span class="delta ' + (d >= 0.005 ? "up-good" : d <= -0.005 ? "up-bad" : "") + '">' + (Math.abs(d) < 0.005 ? "Same as routine" : (d > 0 ? "▼ " + gbp(d) + " better overall" : "▲ " + gbp(-d) + " worse overall")) + "</span>";
    return '<article class="scard' + (chosen ? " is-chosen" : "") + '"><div class="nm"><i class="sw' + (n === "Current routine" ? " dot" : "") + '" style="border-color:' + COLOR[n] + '"></i>' + esc(NAMES[n] || n) + (chosen ? ' <span class="tag chosen">Chosen</span>' : "") + "</div>" +
      '<div><div class="k-small">Model score</div><div class="big">' + gbp(r.total) + "</div>" + delta + "</div>" +
      (cmp[n] ? sparkline(cmp[n].room, COLOR[n], lo, hi) : "") +
      strip(tidy(r.values), false) +
      '<div class="kv"><span>Electricity</span><b>' + gbp(r.grid_cost) + (n === "Current routine" ? "" : ' <span class="' + (r.grid_cost <= cur.grid_cost ? "up-good" : "up-bad") + '">(' + (r.grid_cost <= cur.grid_cost ? "−" : "+") + "£" + Math.abs(r.grid_cost - cur.grid_cost).toFixed(2) + ")</span>") + "</b><span>Comfort shortfall</span><b>" + gbp(r.penalty) + "</b><span>Pump</span><b>" + r.kwh_hp.toFixed(1) + " kWh</b><span>Coldest awake</span><b>" + deg(r.t_min_awake) + "</b></div>" +
      '<p class="why">' + (ex.length ? esc(ex.slice(0, 3).join("; ")) : "Same setpoints as your routine.") + "</p>" +
      '<button type="button" class="btn ghost" data-try="' + esc(n) + '">Open in editor</button></article>';
  }).join("");
  document.querySelectorAll("[data-try]").forEach((b) => b.addEventListener("click", () => { setEdit(p.strategies[b.dataset.try].values.slice()); $("#edit").scrollIntoView(); }));
  const nb = p.strategies["Optimised (no battery, for reference)"];
  renderBreakdown(nb);
}
function renderBreakdown(nb) {
  const p = S.plan, cmp = p.compare;
  const box = $("#breakdown");
  const chName = cmp && cmp[p.chosen] ? p.chosen : "Optimised";
  if (!cmp || !cmp["Current routine"] || chName === "Current routine") { box.innerHTML = '<p class="foot">The routine was kept, so there is no saving to break down.</p>'; $("#br-sub").textContent = ""; return; }
  const comps = [["cosy", "Cosy-rate electricity"], ["standard", "Standard-rate electricity"], ["peak", "Peak-rate electricity"], ["penalty", "Comfort shortfall"], ["terminal", "Heat left in the house"], ["battery", "Battery left over"]];
  const a = cmp["Current routine"].breakdown, b = cmp[chName].breakdown;
  const rows = comps.map(([k, l]) => ({ l, v: a[k] - b[k] }));
  const net = rows.reduce((s, r) => s + r.v, 0);
  $("#br-sub").textContent = NAMES[chName] + " vs your routine";
  const W = Math.max(300, box.clientWidth || 600), narrow = W < 560;
  const LW = narrow ? 150 : 200, VW = 64, rowH = 30;
  const m = Math.max(0.05, ...rows.map((r) => Math.abs(r.v)));
  const cx = LW + (W - LW - VW) / 2, sc = (W - LW - VW) / 2 / m;
  const H = rows.length * rowH + 30;
  const o = ['<svg width="' + W + '" height="' + H + '" role="img" aria-label="Saving by component">'];
  o.push('<text class="ax" x="' + (cx - 6) + '" y="12" text-anchor="end">◀ costs more</text><text class="ax" x="' + (cx + 6) + '" y="12">saves ▶</text>');
  rows.forEach((r, i) => {
    const y = 22 + i * rowH, w = Math.abs(r.v) * sc;
    o.push('<text x="0" y="' + (y + 15) + '" style="fill:var(--ink-2);font-size:13px">' + r.l + "</text>");
    if (w > 0.5) o.push('<rect x="' + (r.v >= 0 ? cx : cx - w) + '" y="' + (y + 4) + '" width="' + w + '" height="16" rx="3" fill="' + (r.v >= 0 ? "var(--plan)" : "var(--crit)") + '"><title>' + r.l + ": " + (r.v >= 0 ? "saves " : "costs ") + gbp(Math.abs(r.v)) + "</title></rect>");
    o.push('<text x="' + W + '" y="' + (y + 16) + '" text-anchor="end" style="fill:var(--ink);font-size:13px;font-variant-numeric:tabular-nums">' + (r.v >= 0 ? "+" : "−") + "£" + Math.abs(r.v).toFixed(2) + "</text>");
  });
  o.push('<line class="base" x1="' + cx + '" x2="' + cx + '" y1="18" y2="' + (H - 6) + '"/></svg>');
  box.innerHTML = o.join("") + '<p class="foot">Net model saving <b style="color:var(--ink)">' + gbp(net) + "</b>." + (nb ? " Without the battery the optimised plan would cost " + gbp(nb.total) + " in model terms." : "") + " Comfort shortfall and end-state heat are priced so that cold rooms and a cold house at the end of the window count against a plan.</p>";
}

/* ---------------------------------------------------------------- week */
function renderWeek() {
  const p = S.plan, occ = S.cfg.occupancy;
  const stLabel = (s) => ({ home: "Home all day", away_day: "Out " + rng(occ.away_from, occ.away_to), away_all: "Away all day" }[s] || s);
  const ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const meanOf = p.forecast_tomorrow.mean;
  const cards = [{ date: p.for_date, status: p.occupancy?.status_tomorrow, mean: meanOf, total: ch.total, cur: cur.total, blocks: p.blocks, kwh: ch.kwh_hp, first: true }]
    .concat((p.outlook || []).filter((o) => !o.error).map((o) => ({ date: o.date, status: o.status, mean: o.forecast_mean, total: o.total, cur: o.current_total, blocks: o.blocks, kwh: o.kwh_hp })));
  $("#week-cards").innerHTML = cards.map((c) => {
    const d = dateOf(c.date), sv = c.cur - c.total;
    return '<div class="day' + (c.first ? " first" : "") + '"><div class="dn">' + WD[wdIdx(d)] + " " + d.getDate() + "<span>" + (c.first ? "pushed" : deg(c.mean, 0)) + "</span></div>" +
      '<div class="meta">' + esc(stLabel(c.status)) + (c.first ? " · " + deg(c.mean, 0) : "") + "</div>" +
      '<div class="cost">' + gbp(c.total) + "</div>" +
      '<div class="meta ' + (sv > 0.005 ? "up-good" : "") + '">' + (sv > 0.005 ? "▼ " + gbp(sv) + " vs routine" : "Same as routine") + "</div>" +
      strip(c.blocks, false) + '<div class="meta">' + (c.kwh != null ? c.kwh.toFixed(0) + " kWh pump" : "") + "</div></div>";
  }).join("");
  const a = seqColor(16).bg, b = seqColor(22).bg;
  $("#sp-scale").innerHTML = "<span>Setpoint</span><span>16°</span><span class='ramp' style='background:linear-gradient(90deg," + a + "," + b + ")'></span><span>22°</span><span>· costs are model totals for each 30 h window</span>";
}

/* ---------------------------------------------------------------- occupancy */
function renderOcc() {
  const occ = S.cfg.occupancy;
  $("#occ-days").innerHTML = WD.map((n, i) => '<button type="button" class="tg" data-wd="' + i + '" aria-pressed="' + occ.away_weekdays.includes(i) + '">' + n + "</button>").join("");
  $("#occ-days").querySelectorAll(".tg").forEach((b) => b.addEventListener("click", () => { b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") !== "true"); refreshUsual(); }));
  $("#occ-from").value = occ.away_from; $("#occ-to").value = occ.away_to;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const opts = [["home", "Home"], ["away_day", "Out daytime"], ["away_all", "Away"]];
  let h = "";
  for (let i = 0; i < 8; i++) {
    const d = new Date(today.getTime() + i * 864e5), iso = isoDate(d);
    const usual = occ.away_weekdays.includes(wdIdx(d)) ? "away_day" : "home";
    const eff = (occ.overrides || {})[iso] || usual;
    h += '<div class="drow"><div class="dl2">' + fmtDate(d) + "<small>" + (i === 0 ? "Today" : i === 1 ? "Tomorrow" : "") + '</small></div><div class="seg" role="radiogroup" aria-label="' + fmtDate(d) + '">' +
      opts.map(([k, l]) => '<label><input type="radio" name="d_' + iso + '" value="' + k + '"' + (eff === k ? " checked" : "") + "><span>" + l + "</span></label>").join("") +
      '</div><div class="usual" data-usual="' + iso + '"></div></div>';
  }
  $("#occ-dates").innerHTML = h;
  refreshUsual();
  const hint = S.params?.info?.presence_hint || {};
  const hs = Object.entries(hint).map(([k, v]) => WD[+k] + " " + Math.round(v.home_share * 100) + "%");
  $("#occ-hint").textContent = hs.length ? "From your phone's location, share of " + rng(occ.away_from, occ.away_to) + " spent at home recently: " + hs.join(" · ") + "." : "";
}
function awayDays() { return [...document.querySelectorAll("#occ-days .tg")].filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => +b.dataset.wd); }
function refreshUsual() {
  const aw = awayDays();
  document.querySelectorAll("[data-usual]").forEach((el) => {
    const d = dateOf(el.dataset.usual), usual = aw.includes(wdIdx(d)) ? "away_day" : "home";
    const sel = document.querySelector('input[name="d_' + el.dataset.usual + '"]:checked')?.value;
    el.textContent = sel && sel !== usual ? "Usually " + (usual === "home" ? "home" : "out") : "";
  });
}
function wireOcc() {
  $("#occ-dates").addEventListener("change", refreshUsual);
  $("#occ-save").onclick = () => {
    const ov = {};
    document.querySelectorAll("#occ-dates input:checked").forEach((x) => (ov[x.name.slice(2)] = x.value));
    rerun("/api/occupancy", { away_weekdays: awayDays(), away_from: +$("#occ-from").value, away_to: +$("#occ-to").value, overrides: ov }, $("#occ-msg"));
  };
}
async function rerun(path, body, m) {
  m.className = "msg"; m.textContent = path === "/api/occupancy" ? "Saving…" : "Starting…";
  try { await api(path, body); } catch (e) { m.className = "msg err"; m.textContent = e.message; return; }
  m.textContent = "Re-running the model (about a minute)…";
  for (let i = 0; i < 160; i++) {
    await new Promise((z) => setTimeout(z, 1500));
    const s = await api("/api/rerun-status").catch(() => ({}));
    if (s.state === "done") { m.className = "msg ok"; m.textContent = "Done. New plan pushed."; await loadAll(); return; }
    if (s.state === "error") { m.className = "msg err"; m.textContent = "Failed: " + (s.message || "").slice(-160); return; }
  }
  m.textContent = "Still running. Refresh in a minute.";
}

/* ---------------------------------------------------------------- model / trust */
function renderModel() {
  const pj = S.params || {}, pr = pj.params || S.plan.params || {}, info = pj.info || {}, bt = pj.backtest || [];
  const days = info.days_used ?? null;
  const mae = bt.length ? bt.reduce((s, b) => s + Math.abs(b.pred_kwh - b.actual_kwh), 0) / bt.length : null;
  const act = bt.length ? bt.reduce((s, b) => s + b.actual_kwh, 0) / bt.length : null;
  const ico = (c) => '<svg class="ico" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="var(--' + c + ')"/><path d="' + (c === "good" ? "M6 10.5l2.5 2.5L14 7.5" : "M10 5.5v5.5M10 14v.5") + '" stroke="#0b0b0b" stroke-width="2" fill="none" stroke-linecap="round"/></svg>';
  const mature = days != null && days >= 28 && mae != null && act && mae / act < 0.15;
  $("#maturity").className = "banner " + (mature ? "good" : "warn");
  $("#maturity").innerHTML = ico(mature ? "good" : "warn") + "<div>" + (mature ? "<b>Settled.</b> " + days + " days of data and daily error within 15%. The savings figures are reasonable estimates." :
    "<b>Early days.</b> " + (days != null ? days + " days of data" : "Not much data yet") + (mae != null ? ", daily error about " + mae.toFixed(1) + " kWh on roughly " + act.toFixed(0) + " kWh" : "") + ". Treat savings as indicative until about 4 weeks of data are in.") + "</div>";
  const tiles = [
    ["Heat loss", (pr.ua_kw_per_k ?? 0).toFixed(3) + " kW/K", "≈ " + ((pr.ua_kw_per_k || 0) * 20).toFixed(1) + " kW at 0° out, 20° in"],
    ["Free gains", (pr.gains_kw ?? 0).toFixed(2) + " kW", "people, appliances, base solar"],
    ["Thermal mass", (pr.c_kwh_per_k ?? 0).toFixed(1) + " kWh/K", "seen by the thermostat room"],
    ["Backtest error", mae != null ? mae.toFixed(1) + " kWh/day" : "–", info.heat_loss_r2 != null ? "heat-loss fit R² " + info.heat_loss_r2 : bt.length + " days tested"],
  ];
  $("#model-tiles").innerHTML = tiles.map(([k, v, d]) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + d + "</div></div>").join("");
  $("#model-notes").innerHTML = (pj.notes || []).map((n) => "<li>" + esc(n) + "</li>").join("") + "<li>Last fit " + esc((pj.fitted_at || "never").replace("T", " ")) + "</li>";
  renderBacktest(bt);
  const hist = (pj.history || []).map((h) => h.params.ua_kw_per_k);
  $("#ua-hist").innerHTML = hist.length > 1 ? sparkline(hist, "var(--plan)", Math.min(...hist) - 0.005, Math.max(...hist) + 0.005, 300, 60) + '<div class="foot">' + hist[0].toFixed(3) + " → " + hist[hist.length - 1].toFixed(3) + " over " + hist.length + " fits</div>" : '<p class="foot">Needs a few daily fits.</p>';
}
function renderBacktest(bt) {
  const box = $("#backtest");
  if (!bt.length) { box.innerHTML = '<p class="foot">No complete days to test yet.</p>'; return; }
  $("#bt-legend").innerHTML = '<span><i class="sw box" style="background:var(--plan)"></i>Model</span><span><i class="sw box" style="background:var(--measured)"></i>Measured</span>';
  const W = Math.max(300, box.clientWidth || 500), H = 190, L = 34, R = 6, T = 10, B = 34;
  const hi = Math.ceil(Math.max(...bt.flatMap((b) => [b.pred_kwh, b.actual_kwh])) / 5) * 5;
  const Y = (v) => T + (H - T - B) * (1 - v / hi), gw = (W - L - R) / bt.length, bw = Math.min(16, (gw - 6) / 2);
  const o = ['<svg width="' + W + '" height="' + H + '" role="img" aria-label="Predicted and measured pump kWh per day">'];
  for (let v = 0; v <= hi; v += hi > 20 ? 10 : 5) o.push('<line class="gl" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + v + "</text>");
  bt.forEach((b, i) => {
    const cx = L + gw * i + gw / 2, d = dateOf(b.date);
    const rect = (x0, v, col, lab) => '<rect x="' + x0 + '" y="' + Y(v) + '" width="' + bw + '" height="' + (Y(0) - Y(v)) + '" rx="3" fill="' + col + '"><title>' + fmtDate(d) + " · " + lab + " " + v + " kWh · outside " + b.mean_outdoor + "°</title></rect>";
    o.push(rect(cx - bw - 1, b.pred_kwh, "var(--plan)", "model") + rect(cx + 1, b.actual_kwh, "var(--measured)", "measured"));
    o.push('<text class="ax" x="' + cx + '" y="' + (H - B + 14) + '" text-anchor="middle">' + WD[wdIdx(d)].slice(0, 2) + "</text>" + '<text class="ax" x="' + cx + '" y="' + (H - B + 27) + '" text-anchor="middle">' + Math.round(b.mean_outdoor) + "°</text>");
  });
  o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(0) + '" y2="' + Y(0) + '"/></svg>');
  box.innerHTML = o.join("");
}
function renderActuals() {
  const a = S.actuals, box = $("#actuals");
  if (!a || !box) return;
  const d = dateOf(a.date);
  $("#act-title").textContent = fmtDate(d) + ": room temperature";
  const act = a.actual.filter((r) => r.room != null), pred = (a.predicted || []).filter((r) => r.room != null);
  if (!act.length) { box.innerHTML = '<p class="foot">No measurements stored for ' + fmtDate(d) + " yet.</p>"; return; }
  $("#act-legend").innerHTML = (pred.length ? '<span><i class="sw" style="border-color:var(--plan)"></i>Predicted</span>' : "") + '<span><i class="sw" style="border-color:var(--measured)"></i>Measured</span>';
  const W = Math.max(300, box.clientWidth || 500), H = 190, L = 34, R = 8, T = 10, B = 24;
  const vals = act.map((r) => r.room).concat(pred.map((r) => r.room));
  const lo = Math.floor(Math.min(...vals) - 0.3), hi = Math.ceil(Math.max(...vals) + 0.3);
  const X = (h) => L + ((W - L - R) * h) / 23, Y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const o = ['<svg width="' + W + '" height="' + H + '" role="img" aria-label="Predicted and measured room temperature">'];
  for (let v = lo; v <= hi; v += hi - lo > 4 ? 2 : 1) o.push('<line class="gl" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + v + "°</text>");
  for (let h = 0; h < 24; h += 3) o.push('<text class="ax" x="' + X(h) + '" y="' + (H - 6) + '" text-anchor="middle">' + String(h).padStart(2, "0") + "</text>");
  const line = (rs, col) => '<path d="' + rs.map((r, i) => (i ? "L" : "M") + X(r.h) + "," + Y(r.room)).join("") + '" fill="none" stroke="' + col + '" stroke-width="2" stroke-linejoin="round"/>';
  if (pred.length) o.push(line(pred, "var(--plan)"));
  o.push(line(act, "var(--measured)"));
  act.forEach((r) => o.push('<circle cx="' + X(r.h) + '" cy="' + Y(r.room) + '" r="4" fill="var(--measured)" stroke="var(--surface)" stroke-width="2"><title>' + hh(r.h) + " measured " + r.room + "°" + "</title></circle>"));
  o.push("</svg>");
  box.innerHTML = o.join("");
  const ka = a.actual.reduce((s, r) => s + (r.kwh || 0), 0), kp = (a.predicted || []).reduce((s, r) => s + (r.kwh || 0), 0);
  let err = "";
  if (pred.length) { const m = {}; pred.forEach((r) => (m[r.h] = r.room)); const e = act.filter((r) => m[r.h] != null).map((r) => r.room - m[r.h]); if (e.length) err = " Measured ran " + Math.abs(e.reduce((s, v) => s + v, 0) / e.length).toFixed(1) + "° " + (e.reduce((s, v) => s + v, 0) >= 0 ? "warmer" : "cooler") + " than predicted on average."; }
  $("#act-foot").textContent = "Pump electricity: measured " + ka.toFixed(1) + " kWh" + (pred.length ? ", predicted " + kp.toFixed(1) + " kWh (" + (a.strategy ? NAMES[a.strategy] || a.strategy : "") + ")." : ". No stored plan for this day.") + err;
}

/* ---------------------------------------------------------------- history */
function renderHistory() {
  const idx = S.idx.slice().reverse();
  const tot = idx.reduce((s, i) => s + (i.saving_gbp || 0), 0);
  const kept = idx.filter((i) => i.chosen === "Current routine").length;
  const first = idx.length ? dateOf(idx[idx.length - 1].for_date) : null;
  $("#hist-tiles").innerHTML = [
    ["Model-estimated saving", gbp(tot), idx.length + " plans since " + (first ? fmtDate(first) : "–")],
    ["Average per day", idx.length ? gbp(tot / idx.length) : "–", "Not metered: an estimate against your routine"],
  ].map(([k, v, d]) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + d + "</div></div>").join("");
  $("#hist-table").innerHTML = "<thead><tr><th>Day</th><th>Plan</th><th>Setpoints</th><th class='r'>Saving</th><th>Pushed</th></tr></thead><tbody>" +
    idx.slice(0, 14).map((i) => "<tr><td>" + fmtDate(dateOf(i.for_date)) + "</td><td>" + esc(NAMES[i.chosen] || i.chosen) + "</td><td>" + strip(i.blocks || [], false) + "</td><td class='r'>" + gbp(i.saving_gbp || 0) + "</td><td>" + (i.pushed ? (i.pushed.verified ? "✓ verified" : "unverified") : "preview") + "</td></tr>").join("") + "</tbody>";
  $("#log").innerHTML = S.log.slice().reverse().map((l) => {
    const rest = Object.entries(l).filter(([k]) => k !== "at" && k !== "event").map(([k, v]) => k + "=" + (typeof v === "object" ? JSON.stringify(v) : v)).join(" ");
    return '<li class="' + (l.event === "error" ? "e-error" : "") + '">' + esc((l.at || "").replace("T", " ")) + " <b>" + esc(l.event) + "</b> " + esc(rest.slice(0, 200)) + "</li>";
  }).join("");
  void kept;
}

/* ---------------------------------------------------------------- dialog, theme, boot */
function confirmDlg(title, html, ok) {
  const d = $("#dlg");
  $("#dlg-title").textContent = title; $("#dlg-body").innerHTML = html; $("#dlg-ok").textContent = ok || "Confirm";
  return new Promise((res) => { d.onclose = () => res(d.returnValue === "ok"); d.returnValue = ""; d.showModal(); });
}
function wireTheme() {
  $("#theme").onclick = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("hp-theme", root.dataset.theme); } catch (e) { /* ignore */ }
    if (S.plan) render();
  };
}
let rz = null;
addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (S.plan) { renderTimeline(); renderBreakdown(S.plan.strategies["Optimised (no battery, for reference)"]); renderBacktest((S.params || {}).backtest || []); renderActuals(); } }, 150); });
wireTheme(); wireEditor(); wireOcc();
loadAll().catch((e) => { $("#loading").textContent = "Could not load the plan: " + e.message; });
setInterval(loadStatus, 60000);
setInterval(() => { if (!S.edit && document.visibilityState === "visible") loadAll(); }, 300000);
