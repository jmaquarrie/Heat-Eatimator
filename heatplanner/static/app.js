/* Heat Planner dashboard. Vanilla JS, no dependencies; charts are hand-built SVG. */
"use strict";

const $ = (s) => document.querySelector(s);
const S = { plan: null, params: null, idx: [], cfg: null, status: null, log: [], edit: null, editOff: null, editRes: null, actuals: null, occ: null, sel: null, view: null, revs: [], measured: null, chat: [], chatSeen: 0, agent: {} };
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
const TABS = ["now", "plan", "week", "home", "more"];
const MOBILE = () => matchMedia("(max-width: 759px)").matches;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gbp = (v) => (v < 0 ? "−£" : "£") + Math.abs(v).toFixed(2);
const deg = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d).replace(/^-(0(\.0+)?)$/, "$1") + "°");
const hh = (h) => { const m = Math.round(h * 60); return String(Math.floor(m / 60) % 25).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
const rng = (a, b) => hh(a) + "–" + hh(b);
const parseT = (s) => { const [d, t] = s.split("T"); const [Y, M, D] = d.split("-").map(Number); const [h, m] = t.split(":").map(Number); return new Date(Y, M - 1, D, h, m); };
const dateOf = (iso) => { const [Y, M, D] = iso.split("-").map(Number); return new Date(Y, M - 1, D); };
const wdIdx = (d) => (d.getDay() + 6) % 7;
const fmtDate = (d) => WD[wdIdx(d)] + " " + d.getDate() + " " + MON[d.getMonth()];
const isoDate = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const nowH = () => { const n = new Date(); return n.getHours() + n.getMinutes() / 60; };
const tile = (k, v, d) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + (d || "") + "</div></div>";

function tariff(h) {
  const t = S.cfg.tariff, x = ((h % 24) + 24) % 24;
  if (t.cosy.some(([a, b]) => a <= x && x < b)) return { slot: "cosy", rate: t.rate_cosy, label: "Cosy" };
  if (t.peak.some(([a, b]) => a <= x && x < b)) return { slot: "peak", rate: t.rate_peak, label: "Peak" };
  return { slot: "standard", rate: t.rate_standard, label: "Standard" };
}
/** Score in points: your routine = 100, each 1% cheaper overall = +1 (higher is better). */
function scoreOf(total, routineTotal) {
  const R = routineTotal != null ? routineTotal : (V().strategies["Current routine"] || {}).total;
  return R > 0.1 ? Math.round(100 + (100 * (R - total)) / R) : null;
}
const pts = (v) => (v == null ? "–" : String(v));
const pence = (r) => Math.round(r * 1000) / 10 + "p";

/** Plain-language comfort: hours below the soft floor while home and awake. */
function comfortText(r, short) {
  const f = deg(S.cfg.comfort.soft_floor_c);
  if (r.hours_below == null) return r.penalty < 0.05 ? "Warm enough" : "Some time below " + f;
  if (r.hours_below < 0.25) return short ? "Warm enough" : "Warm enough all day";
  const h = r.hours_below < 1 ? Math.round(r.hours_below * 60) + " min" : (Math.round(r.hours_below * 2) / 2) + " h";
  return h + " below " + f + (short ? "" : " while home");
}
function moneyDelta(a, b, unit) {
  const d = a - b;
  if (Math.abs(d) < 0.005) return '<span class="d">same as routine</span>';
  return '<span class="d ' + (d < 0 ? "up-good" : "up-bad") + '">' + (d < 0 ? "▼ " : "▲ ") + (unit ? Math.abs(d).toFixed(1) + " " + unit : gbp(Math.abs(d))) + (d < 0 ? " less" : " more") + " than routine</span>";
}

/* ---------------------------------------------------------------- API */
function key() { try { return localStorage.getItem("hp-key") || ""; } catch (e) { return ""; } }
let servedFromCache = null;
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
  if (path === "/api/plan" && r.headers.get("X-From-Cache")) servedFromCache = r.headers.get("X-From-Cache");
  const txt = await r.text();
  let j; try { j = JSON.parse(txt); } catch (e) { j = { error: txt }; }
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

async function loadAll() {
  servedFromCache = null;
  const [plan, params, idx, cfg, log] = await Promise.all(["/api/plan", "/api/params", "/api/plans", "/api/config", "/api/log"].map((p) => api(p).catch(() => null)));
  S.plan = plan && plan.for_date ? plan : null; S.params = params || {}; S.idx = idx || []; S.cfg = cfg; S.log = log || [];
  S.edit = null; S.editOff = null; S.editRes = null; S.occ = null;
  if (S.plan) await selectDay(dayOptions().includes(S.sel) ? S.sel : defaultDay(), false);
  const off = $("#offline");
  off.hidden = !servedFromCache;
  if (servedFromCache) off.textContent = "Offline: showing the plan saved on this device. Live readings and editing come back when the planner is reachable.";
  render();
  loadStatus();
  api("/api/actuals").then((a) => { S.actuals = a; renderActuals(); renderMeter(); }).catch(() => { renderMeter(); });
}
async function loadStatus() {
  try { S.status = await api("/api/status"); } catch (e) { S.status = { available: false, error: String(e.message) }; }
  renderChips(); renderNow();
}

/* ---------------------------------------------------------------- helpers */
/** The day shown in the Plan view and editor: the main plan, or a day fetched from /api/day. */
function V() { return S.view || S.plan; }
function planValues() { return V().blocks_raw.map((b) => b.temperature); }
function offOn() { const v = V(); return !!(v && v.lwt_offset && v.lwt_offset.enabled && v.blocks_raw[0] && "lwt_offset" in v.blocks_raw[0]); }
function planOffsets() { return offOn() ? V().blocks_raw.map((b) => b.lwt_offset) : null; }
/** Days you can pick without the "Edit today's plan" button: future days only. */
function todayAvailable() { const p = S.plan, t = isoDate(new Date()); return p.for_date === t || !!(p.sim && p.sim.start.slice(0, 10) === t && p.sim.fixed); }
function dayOptions() {
  const t = isoDate(new Date()), out = [];
  if (todayAvailable()) out.push(t);
  if (S.plan.for_date > t) out.push(S.plan.for_date);
  (S.plan.outlook || []).forEach((o) => { if (!o.error && o.date > t && !out.includes(o.date)) out.push(o.date); });
  return out;
}
function defaultDay() { const o = dayOptions(); return o.length ? o[0] : S.plan.for_date; }   // today when it can be shown
async function selectDay(iso, redraw = true) {
  S.sel = iso; S.edit = null; S.editOff = null; S.editRes = null;
  if (iso === S.plan.for_date) S.view = null;
  else {
    try { const v = await api("/api/day?date=" + iso); S.view = v.kind === "plan" ? null : v; }
    catch (e) { S.view = null; S.sel = S.plan.for_date; }
  }
  await loadDayExtras();
  if (redraw) { renderDayPick(); renderHero(); renderEditor(); renderTimeline(); renderChanges(); }
}
/** Revisions for the selected day, and (for today) what the room has actually done so far. */
async function loadDayExtras() {
  S.revs = await api("/api/revisions?date=" + S.sel).catch(() => []);
  S.measured = S.sel === isoDate(new Date()) ? await api("/api/actuals?date=" + S.sel).catch(() => null) : null;
}
function renderDayPick() {
  const t = isoDate(new Date()), opts = dayOptions().slice();
  const wk = {}; (S.plan.outlook || []).forEach((o) => (wk[o.date] = o));
  const html = opts.map((iso) => {
    const d = dateOf(iso), lab = iso === t ? "Today" : WD[wdIdx(d)] + " " + d.getDate();
    const tag = iso === t ? "now" : iso === S.plan.for_date ? "planned" : wk[iso] && wk[iso].edited ? "your edit" : "preview";
    return '<button type="button" class="dp' + (iso === S.sel ? " on" : "") + '" data-day="' + iso + '" aria-pressed="' + (iso === S.sel) + '"><b>' + lab + "</b>" + (tag ? "<small>" + tag + "</small>" : "") + "</button>";
  }).join("");
  document.querySelectorAll(".daypick").forEach((el) => {
    el.innerHTML = html;
    el.querySelectorAll("[data-day]").forEach((b) => b.addEventListener("click", () => selectDay(b.dataset.day).then(renderWeek)));
  });
}
const sgn = (v) => (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v);
function offSummary(offs) {
  if (!offs) return "–";
  const lo = Math.min(...offs), hi = Math.max(...offs);
  return lo === hi ? sgn(lo) + " all day" : sgn(lo) + " to " + sgn(hi);
}
function routineValues() { return S.plan.strategies["Current routine"].values; }
function sameVals(a, b) { return a && b && a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-9); }
/* ---- which day the editor is changing: the plan's day, or (after 22:00) the rest of today ---- */
function todayIso() { return isoDate(new Date()); }
function editDate() { return V().for_date; }
function baseVals() { return planValues(); }
function baseOffs() { return planOffsets(); }
/** Blocks that have already ended on the day being edited (locked). */
function lockedIdx() {
  const d = editDate(), t = todayIso();
  if (d > t) return new Set();
  if (d < t) return new Set(S.cfg.blocks.map((_, i) => i));
  const h = nowH();
  return new Set(S.cfg.blocks.map(([a, b], i) => (b <= h ? i : -1)).filter((i) => i >= 0));
}
function editing() { return !!(S.editRes && ((S.edit && !sameVals(S.edit, baseVals())) || (S.editOff && offOn() && !sameVals(S.editOff, baseOffs())))); }
function chosenLabel() { const v = V(); return v.chosen === "Your edit" && v.manual && v.manual.by === "hermes" ? "Hermes-adjusted" : NAMES[v.chosen] || v.chosen; }
function isDark() { const t = document.documentElement.dataset.theme; return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; }

function seqColor(v) {
  // sequential one-hue ramp for setpoints 16..22
  const lo = css("--seq-lo"), hi = css("--seq-hi");
  const t = Math.max(0, Math.min(1, (v - 16) / 6));
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const a = p(lo), b = p(hi);
  const c = a.map((x, i) => Math.round(x + (b[i] - x) * t));
  return { bg: "rgb(" + c.join(",") + ")", lite: isDark() ? t > 0.55 : t < 0.45 };
}
function strip(blocks, labels = true) {
  return '<div class="strip" role="img" aria-label="' + esc(blocks.map((b) => rng(b.from, b.to) + " " + b.temperature + "°").join(", ")) + '">' +
    blocks.map((b) => { const c = seqColor(b.temperature); return '<div class="' + (c.lite ? "lite" : "") + '" style="flex:' + (b.to - b.from) + ";background:" + c.bg + '" title="' + rng(b.from, b.to) + " " + b.temperature + '°">' + (labels && b.to - b.from >= 3 ? b.temperature : "") + "</div>"; }).join("") + "</div>";
}
function tidy(values, offs) {
  const out = [];
  S.cfg.blocks.forEach(([a, b], i) => {
    const v = values[i], o = offs ? offs[i] : undefined, last = out[out.length - 1];
    if (last && last.temperature === v && last.lwt_offset === o) last.to = b;
    else out.push(o === undefined ? { from: a, to: b, temperature: v } : { from: a, to: b, temperature: v, lwt_offset: o });
  });
  return out;
}
function mergeBlocks(bs) {
  const out = [];
  bs.forEach((b) => { if (out.length && out[out.length - 1].temperature === b.temperature) out[out.length - 1].to = b.to; else out.push({ ...b }); });
  return out;
}
/** Today's setpoint blocks: from the plan if it is for today, else from the stored "today" part of tonight's plan. */
function todayBlocks() {
  const p = S.plan, today = isoDate(new Date());
  if (p.for_date === today) return p.blocks;
  if (p.sim && p.sim.start.slice(0, 10) === today) return mergeBlocks(p.sim.fixed.map(([a, b, t]) => ({ from: a, to: b, temperature: t })));
  return null;
}

/** Plain-English reasons a strategy differs from the routine. */
function explain(vals, base, offs, baseOffs) {
  const parts = [], oparts = [];
  if (offs && baseOffs) {
    // group consecutive blocks with the same offset change
    let run = null;
    const flush = () => { if (run) { oparts.push("runs the water " + Math.abs(run.d) + "° " + (run.d < 0 ? "cooler" : "hotter") + " " + rng(run.a, run.b)); run = null; } };
    S.cfg.blocks.forEach(([a, b], i) => { const d = offs[i] - baseOffs[i]; if (!d) { flush(); return; } if (run && run.d === d && run.b === a) run.b = b; else { flush(); run = { a, b, d }; } });
    flush();
  }
  S.cfg.blocks.forEach(([a, b], i) => {
    const d = vals[i] - base[i];
    if (Math.abs(d) < 0.25) return;
    const t = tariff(a), r = rng(a, b), w = vals[i] + "° instead of " + base[i] + "° " + r;
    if (d > 0) parts.push(t.slot === "cosy" ? "pre-heats to " + w + " while power is cheap" : "runs warmer at " + w);
    else parts.push(t.slot === "peak" ? "eases off to " + w + " through the peak" : "runs cooler at " + w);
  });
  return parts.concat(oparts);
}
const joinAnd = (a) => (a.length > 1 ? a.slice(0, -1).join(", ") + " and " + a[a.length - 1] : a[0] || "");

/* ---------------------------------------------------------------- tabs (phones) */
function setTab(t, scroll = true) {
  if (!TABS.includes(t)) t = "now";
  document.body.dataset.tab = t;
  document.querySelectorAll(".sec").forEach((s) => s.classList.toggle("tab-off", s.dataset.tab !== t));
  document.querySelectorAll(".tabbar button").forEach((b) => b.setAttribute("aria-current", b.dataset.go === t ? "page" : "false"));
  if (scroll && MOBILE()) scrollTo(0, 0);
  if (S.plan) requestAnimationFrame(drawCharts);
}
function tabForHash(h) {
  const id = (h || "").replace("#", "");
  if (TABS.includes(id)) return id;
  const sec = id && document.getElementById(id);
  return sec && sec.dataset.tab ? sec.dataset.tab : null;
}

/* ---------------------------------------------------------------- render: all */
function render() {
  $("#loading").hidden = !!S.plan;
  if (!S.plan) { $("#loading").textContent = "No plan yet. The first one is made at 22:00, or run “heatplanner.cli plan”."; return; }
  document.querySelectorAll(".sec").forEach((s) => (s.hidden = false));
  setTab(document.body.dataset.tab, false);
  renderDayPick(); renderNow(); renderHero(); renderEditor(); renderChanges(); renderStrategies(); renderWeek(); renderOcc(); renderModel(); renderHistory(); renderChips();
  drawCharts();
  $("#foot-entity").textContent = S.cfg.schedule_entity;
}
function drawCharts() {
  if (!S.plan) return;
  renderTimeline(); renderBreakdown(); renderBacktest((S.params || {}).backtest || []); renderActuals();
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
    else if (st.following === false) out.push(chip("warn", "Pump at <b>" + deg(st.climate.setpoint) + "</b>, plan <b>" + deg(st.helper.temperature) + "</b>", "Expected until the HA automation linking the helper to the thermostat is in place"));
    else out.push(chip("", "Pump link: not set up yet"));
    if (offOn() && st.offset_following === false) out.push(chip("warn", "Offset at <b>" + sgn(st.offset.value) + "</b>, plan <b>" + sgn(st.helper.lwt_offset) + "</b>", "Expected until the offset automation is in place"));
    if (st.api_budget != null) out.push(chip(st.api_budget < 15 ? "warn" : "", "Daikin API left <b>" + Math.round(st.api_budget) + "</b>", "Daikin cloud calls left today (about 200 a day)"));
    if (st.grid_w != null) out.push(chip("", (st.grid_w >= 0 ? "Grid importing <b>" : "Grid exporting <b>") + (Math.abs(st.grid_w) / 1000).toFixed(2) + " kW</b>"));
  }
  $("#chips").innerHTML = out.join("");
}

/* ---------------------------------------------------------------- now card */
function renderNow() {
  if (!S.plan || !S.cfg) return;
  const st = S.status || {}, h = nowH(), p = S.plan;
  const tb = todayBlocks();
  let curB = null, nextB = null, nextWhen = "";
  if (tb) {
    const i = tb.findIndex((b) => b.from <= h && h < b.to);
    curB = tb[i] || null;
    nextB = i >= 0 ? tb[i + 1] || null : null;
    if (nextB) nextWhen = hh(nextB.from);
    else if (p.for_date !== isoDate(new Date()) && p.blocks.length) { nextB = p.blocks[0]; nextWhen = "00:00"; }
  }
  const target = st.helper && st.helper.temperature != null ? st.helper.temperature : curB ? curB.temperature : null;
  let pred = null;
  const now = new Date();
  for (const r of p.trace_best || []) { if (parseT(r.t) <= now) pred = r.room; else break; }
  const tf = tariff(h);
  let nh = Math.floor(h * 2) / 2 + 0.5, guard = 0;
  while (tariff(nh).slot === tf.slot && guard++ < 48) nh += 0.5;
  const nt = tariff(nh);
  const room = st.room;
  const diff = room != null && pred != null ? room - pred : null;
  const facts = [];
  facts.push('<div class="fact"><span>Tariff now</span><b><span class="tag ' + tf.slot + '">' + tf.label + " " + pence(tf.rate) + "</span></b><small>" + nt.label + " from " + hh(nh % 24) + "</small></div>");
  const orr = Object.entries(st.other_rooms || {});
  if (orr.length) facts.push('<div class="fact"><span>Rest of house</span><b>' + orr.map(([, v]) => deg(v)).join(" / ") + "</b><small>" + orr.map(([k]) => k).join(" / ") + "</small></div>");
  if (st.battery_pct != null) facts.push('<div class="fact"><span>Battery</span><b>' + Math.round(st.battery_pct) + "%</b><small>" + (S.cfg.battery.capacity_kwh * st.battery_pct / 100).toFixed(1) + " kWh</small></div>");
  if (st.outdoor != null) facts.push('<div class="fact"><span>Outside</span><b>' + deg(st.outdoor) + "</b><small>" + deg(p.forecast_tomorrow.min, 0) + " to " + deg(p.forecast_tomorrow.max, 0) + " on plan day</small></div>");
  if (offOn() || (st.offset && st.offset.value != null)) {
    const pv = st.helper && st.helper.lwt_offset != null ? st.helper.lwt_offset : curB && curB.lwt_offset != null ? curB.lwt_offset : null;
    facts.push('<div class="fact"><span>Water offset</span><b>' + (st.offset && st.offset.value != null ? sgn(st.offset.value) + "°" : "–") + "</b><small>" + (pv != null ? "plan " + sgn(pv) + "°" : "") + (st.offset && st.offset.lwt != null ? " · water " + deg(st.offset.lwt) : "") + "</small></div>");
  }
  if (st.heating) facts.push('<div class="fact"><span>Heating</span><b>' + (st.heating === "on" ? "Running" : "Idle") + "</b><small>" + (st.following === true ? "following plan" : st.following === false ? "not following plan" : "pump link not set") + "</small></div>");
  let strip24 = "";
  if (tb) strip24 = '<div class="today"><div class="mini-title">Today\'s setpoints</div><div class="today-strip">' + strip(tb) + '<i class="now-mark" style="left:' + (h / 24 * 100).toFixed(2) + '%"></i></div><div class="today-ax"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div></div>';
  $("#now-card").innerHTML =
    '<div class="now-main"><div class="now-room"><span class="k">Room now</span><span class="big">' + (room != null ? deg(room) : "–") + "</span>" +
    '<span class="d">' + (target != null ? "target " + deg(target) : "") + (diff != null ? ' · <span class="' + (Math.abs(diff) > 1 ? "up-bad" : "") + '">' + (Math.abs(diff) < 0.15 ? "as predicted" : Math.abs(diff).toFixed(1) + "° " + (diff > 0 ? "warmer" : "cooler") + " than predicted") + "</span>" : "") + "</span></div>" +
    '<div class="now-next"><span class="k">Next change</span>' + (nextB ? '<span class="nx"><b>' + nextWhen + "</b> → <b>" + deg(nextB.temperature) + '</b></span><span class="tag ' + tariff(nextB.from).slot + '">' + tariff(nextB.from).label + "</span>" : '<span class="nx">None today</span>') + "</div></div>" +
    strip24 + '<div class="facts">' + facts.join("") + "</div>" +
    (st.available === false ? '<p class="foot">Live readings unavailable: ' + esc(st.error || "") + "</p>" : "") +
    latestUpdateHtml();
}

function latestUpdateHtml() {
  const u = (S.chat || []).filter((m) => m.from === "hermes" && m.kind === "update").slice(-1)[0];
  if (!u) return "";
  return '<button type="button" class="hermes-latest" onclick="openChat()"><span class="orb-mini" aria-hidden="true"></span><span><b>Hermes, ' + esc(u.at.slice(11, 16)) + ":</b> " + esc(u.text.split("\n")[0].slice(0, 160)) + "</span></button>";
}

/* ---------------------------------------------------------------- hero */
function renderHero() {
  const p = V(), d = dateOf(p.for_date);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const rel = Math.round((d - today) / 864e5);
  const when = rel === 0 ? "Today" : rel === 1 ? "Tomorrow" : rel === -1 ? "Yesterday" : WDL[wdIdx(d)];
  const ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const fc = p.forecast_tomorrow;
  $("#hero-eyebrow").innerHTML = "<span>" + (p.kind === "outlook" ? "Preview · " + fmtDate(d) : when + "'s plan · " + fmtDate(d)) + "</span>" + '<span class="tag">' + deg(fc.min, 0) + " to " + deg(fc.max, 0) + " outside</span>" +
    (p.strategies["Current routine"] && p.strategies[p.chosen] ? '<span class="tag score">Score ' + pts(scoreOf(p.strategies[p.chosen].total)) + " · routine 100</span>" : "") +
    '<span class="tag">' + ({ home: "Home all day", away_day: "Out " + rng(S.cfg.occupancy.away_from, S.cfg.occupancy.away_to), away_all: "Away all day" }[p.occupancy?.status_tomorrow] || "") + "</span>";
  let title, why = "";
  const money = cur.grid_cost - ch.grid_cost, comfort = cur.penalty - ch.penalty;
  if (p.kind === "today_tail") { title = "The rest of today"; why = "Tonight's remaining setpoints. Tomorrow's plan is shown on its own day."; }
  else if (p.chosen === "Current routine") { title = "Your routine is already the best plan"; why = "Nothing beat it by more than 2p, so the planner kept your usual setpoints."; }
  else if (p.chosen === "Your edit" && p.manual && p.manual.by === "hermes") { title = "Hermes adjusted today's plan"; why = esc(p.manual.reason || "") + " "; }
  else if (p.chosen === "Your edit") { title = "Running your edited plan"; why = "Pushed by hand at " + esc((p.manual?.at || "").slice(11, 16)) + ". " + (money >= 0.01 ? "About " + gbp(money) + " less electricity than your routine. " : money <= -0.01 ? "About " + gbp(-money) + " more electricity than your routine. " : "") + "Tonight's 22:00 run plans the next day as normal."; }
  else {
    if (money >= 0.01) title = "Saves about " + gbp(money) + " on electricity" + (comfort > 0.05 ? " and keeps you warmer" : "");
    else if (comfort > 0.05) title = "Keeps you warmer than your routine for " + gbp(-money) + " more";
    else title = "A slightly better day than your routine";
    const ex = explain(ch.values, cur.values, ch.offsets, cur.offsets);
    why = ex.length ? "It " + joinAnd(ex.slice(0, 4)) + "." : "";
    const sp = p.strategies["Setpoints only (offset unchanged, for reference)"];
    if (p.note && p.kind === "outlook") why += " " + p.note;
    if (sp && sp.grid_cost - ch.grid_cost >= 0.01) why += " Planning the water temperature as well cuts electricity by " + gbp(sp.grid_cost - ch.grid_cost) + " compared with changing setpoints alone.";
    if (comfort > 0.05 && cur.hours_below != null) why += " Your routine would leave the room " + comfortText(cur) + ".";
    else if (comfort > 0.05) why += " Your routine would let the room fall below the comfort floor while you're home.";
  }
  $("#hero-title").textContent = title;
  $("#hero-why").innerHTML = why;
  $("#hero-tiles").innerHTML = [
    tile("Electricity", gbp(ch.grid_cost), "Routine " + gbp(cur.grid_cost) + " · incl. house"),
    tile("Pump electricity", ch.kwh_hp.toFixed(1) + " kWh", "Routine " + cur.kwh_hp.toFixed(1) + " kWh"),
    tile("Coldest while awake", deg(ch.t_min_awake), "Comfort floor " + deg(S.cfg.comfort.soft_floor_c)),
    tile("Comfort", comfortText(ch, true), "Routine: " + comfortText(cur, true).toLowerCase()),
  ].join("");
}

/* ---------------------------------------------------------------- timeline (small multiples, shared x) */
function renderTimeline() {
  const box = $("#timeline");
  const p = V(), rows = p.trace_best || [];
  if (!box.offsetParent) return; // hidden tab: drawn when shown
  if (!rows.length) { box.innerHTML = '<p class="foot">No trace stored with this plan.</p>'; return; }
  const cur = p.trace_current || [];
  const ed = editing() ? S.editRes.trace : null;
  const n = rows.length;
  const W = Math.max(300, box.clientWidth || 900);
  const narrow = W < 600;
  const L = narrow ? 30 : 42, R = narrow ? 44 : 64;
  const lanes = [
    { id: "room", title: "Room °C", h: narrow ? 210 : 260 },
    ...(rows[0].lwt != null ? [{ id: "lwt", title: "Water temperature °C (target while running)", h: 62 }] : []),
    { id: "out", title: "Outside °C", h: 54 },
    { id: "el", title: "Pump kWh per hour", h: 66 },
    { id: "soc", title: "Battery kWh", h: 52 },
  ];
  const RIB = 20, GAP = 22, XA = 22;
  let y = RIB + 6;
  lanes.forEach((ln) => { ln.y0 = y + 14; y = ln.y0 + ln.h + GAP; });
  const H = y - GAP + XA;
  const x = (i) => L + ((W - L - R) * i) / n;
  const o = [];
  o.push('<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Predicted room temperature, outside temperature, pump electricity and battery through the plan window">');

  let segStart = 0;
  for (let i = 1; i <= n; i++) {
    const prev = tariff(parseT(rows[i - 1].t).getHours());
    const nxt = i < n ? tariff(parseT(rows[i].t).getHours()) : null;
    if (!nxt || nxt.slot !== prev.slot) {
      const x0 = x(segStart), x1 = x(i);
      if (prev.slot !== "standard") o.push('<rect x="' + x0 + '" y="0" width="' + (x1 - x0) + '" height="' + (H - XA) + '" fill="var(--' + prev.slot + ')"/>');
      if (x1 - x0 > 30 && !(narrow && prev.slot === "standard")) o.push('<text x="' + (x0 + 5) + '" y="14" class="ax" style="fill:' + (prev.slot === "standard" ? "var(--muted)" : "var(--" + prev.slot + "-ink)") + ';font-weight:600">' + (x1 - x0 > 52 ? prev.label : prev.label.slice(0, 3)) + (x1 - x0 > 76 ? " " + pence(prev.rate) : "") + "</text>");
      segStart = i;
    }
  }
  const t0 = parseT(rows[0].t), tN = new Date(t0.getTime() + n * 9e5), now = new Date();
  if (now > t0 && now < tN) {
    const xn = x((now - t0) / 9e5);
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

  // ---- room lane (domain always covers the setpoint range so handles can reach it)
  const c = S.cfg.comfort;
  const floorOf = (cls) => (cls === "sleep" ? c.sleep_floor_c : cls === "away" ? c.away_floor_c : c.soft_floor_c);
  const roomPts = (tr) => [p.t_room0].concat(tr.map((r) => r.room));
  const plan = roomPts(rows), rout = cur.length ? roomPts(cur) : null, edt = ed ? roomPts(ed) : null;
  const allR = plan.concat(rout || [], edt || [], [c.min_setpoint_c, c.max_setpoint_c]);
  const rlo = Math.floor(Math.min(...allR) - 0.3), rhi = Math.ceil(Math.max(...allR) + 0.3);
  const lane = (id) => lanes.find((l) => l.id === id);
  const RL = lane("room");
  const Yr = gridY(RL, rlo, rhi, rhi - rlo > 6 ? 2 : 1, (v) => v + "°");
  const floorPath = rows.map((r, i) => (i ? "L" : "M") + x(i) + "," + Yr(floorOf(r.cls)) + "L" + x(i + 1) + "," + Yr(floorOf(r.cls))).join("");
  o.push('<path d="' + floorPath + "L" + x(n) + "," + (RL.y0 + RL.h) + "L" + x(0) + "," + (RL.y0 + RL.h) + 'Z" fill="var(--below)"/>');
  o.push('<path d="' + floorPath + '" fill="none" stroke="var(--muted)" stroke-width="1"/>');
  o.push('<path d="' + stepPath(rows.map((r) => r.target), Yr) + '" fill="none" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"/>');
  if (ed) o.push('<path d="' + stepPath(ed.map((r) => r.target), Yr) + '" fill="none" stroke="var(--edit)" stroke-width="1.5" stroke-dasharray="5 4" opacity=".8"/>');
  if (rout) o.push('<path d="' + path(rout, x, Yr) + '" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="2 3"/>');
  // the setpoints as originally planned, when the day has been changed since
  const revs = (S.revs || []).filter((r) => r.kind !== "undo" || true);
  let orig = null;
  if (revs.length) {
    orig = revs[0].before.map((b) => b.temperature);
    const segs = [];
    rows.forEach((r, j) => {
      if (r.t.slice(0, 10) !== p.for_date) return;
      const tt = parseT(r.t), hr = tt.getHours() + tt.getMinutes() / 60, bi = S.cfg.blocks.findIndex(([a, b]) => hr >= a && hr < b);
      if (bi >= 0) segs.push("M" + x(j).toFixed(1) + "," + Yr(orig[bi]).toFixed(1) + "H" + x(j + 1).toFixed(1));
    });
    if (segs.length) o.push('<path d="' + segs.join("") + '" fill="none" stroke="var(--s4)" stroke-width="2" stroke-dasharray="1 3" stroke-linecap="round"/>');
  }
  o.push('<path d="' + path(plan, x, Yr) + '" fill="none" stroke="var(--plan)" stroke-width="2.25" stroke-linejoin="round"/>');
  if (edt) o.push('<path d="' + path(edt, x, Yr) + '" fill="none" stroke="var(--edit)" stroke-width="2.25" stroke-linejoin="round"/>');
  // what the room actually did today (hourly means from HA history)
  const meas = S.measured && S.measured.actual ? S.measured.actual.filter((r) => r.room != null) : [];
  const day0 = dateOf(p.for_date);
  const xAt = (dtm) => x((dtm - t0) / 9e5);
  if (meas.length && S.sel === isoDate(new Date())) {
    const pts = meas.map((r) => [xAt(new Date(day0.getFullYear(), day0.getMonth(), day0.getDate(), r.h, 30)), Yr(r.room), r]).filter(([xx]) => xx >= L && xx <= W - R);
    if (pts.length) {
      o.push('<path d="' + pts.map(([xx, yy], i) => (i ? "L" : "M") + xx.toFixed(1) + "," + yy.toFixed(1)).join("") + '" fill="none" stroke="var(--measured)" stroke-width="2"/>');
      pts.forEach(([xx, yy, r]) => o.push('<circle cx="' + xx.toFixed(1) + '" cy="' + yy.toFixed(1) + '" r="3.5" fill="var(--measured)" stroke="var(--surface)" stroke-width="1.5"><title>' + hh(r.h) + "–" + hh(r.h + 1) + " measured " + r.room + "°</title></circle>"));
    }
  }
  // a marker at each change made to this day
  revs.forEach((r) => {
    const at = new Date(r.at.slice(0, 19));
    const xx = xAt(at);
    if (xx < L || xx > W - R) return;
    const col = r.by === "hermes" ? "var(--s3)" : r.by === "you" ? "var(--edit)" : "var(--ink-2)";
    o.push('<g class="rev-mark"><line x1="' + xx + '" x2="' + xx + '" y1="' + RL.y0 + '" y2="' + (RL.y0 + RL.h) + '" stroke="' + col + '" stroke-width="1.5" stroke-dasharray="3 2"/>' +
      '<circle cx="' + xx + '" cy="' + (RL.y0 + 8) + '" r="8" fill="' + col + '"/><text x="' + xx + '" y="' + (RL.y0 + 12) + '" text-anchor="middle" style="fill:#fff;font-size:10px;font-weight:700">' + (r.by === "hermes" ? "H" : r.by === "you" ? "Y" : "P") + "</text>" +
      "<title>" + esc((r.by === "hermes" ? "Hermes" : r.by === "you" ? "You" : "Planner") + " at " + r.at.slice(11, 16) + ": " + (r.reason || "edit") + " — " + describeRev(r).join("; ")) + "</title></g>");
  });
  const dls = [{ t: "Plan", v: plan[n], col: "var(--plan)" }];
  if (rout && p.chosen !== "Current routine") dls.push({ t: "Routine", v: rout[n], col: "var(--muted)" });
  if (edt) dls.push({ t: "Edit", v: edt[n], col: "var(--edit)" });
  dls.forEach((d) => (d.y = Yr(d.v)));
  dls.sort((a, b) => a.y - b.y);
  for (let i = 1; i < dls.length; i++) if (dls[i].y - dls[i - 1].y < 13) dls[i].y = dls[i - 1].y + 13;
  dls.forEach((d) => o.push('<circle cx="' + x(n) + '" cy="' + Yr(d.v) + '" r="3" fill="' + d.col + '" stroke="var(--surface)" stroke-width="2"/><text class="dl" x="' + (x(n) + 6) + '" y="' + (d.y + 4) + '">' + d.t + "</text>"));

  // ---- outside lane
  // ---- leaving-water lane: planned target (curve + offset) against the normal curve
  const WL = lane("lwt");
  if (WL) {
    const lw = rows.map((r) => r.lwt), base0 = rows.map((r) => r.lwt - (r.off || 0)), lwe = ed ? ed.map((r) => r.lwt) : [];
    const all = lw.concat(base0, lwe).filter((v) => v != null);
    const wlo = Math.floor(Math.min(...all) - 1), whi = Math.ceil(Math.max(...all) + 1);
    const Yw = gridY(WL, wlo, whi, Math.max(2, Math.ceil((whi - wlo) / 2)), (v) => v + "°");
    o.push('<path d="' + path(base0, (i) => x(i + 0.5), Yw) + '" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="4 3"/>');
    o.push('<path d="' + path(lw, (i) => x(i + 0.5), Yw) + '" fill="none" stroke="var(--plan)" stroke-width="2"/>');
    if (ed && lwe[0] != null) o.push('<path d="' + path(lwe, (i) => x(i + 0.5), Yw) + '" fill="none" stroke="var(--edit)" stroke-width="2"/>');
    o.push('<text class="dl" x="' + (x(n) + 6) + '" y="' + (Yw(base0[n - 1]) + 4) + '" style="fill:var(--muted)">offset 0</text>');
  }
  const OL = lane("out");
  const outs = rows.map((r) => r.outdoor);
  const olo = Math.floor(Math.min(...outs) - 0.5), ohi = Math.ceil(Math.max(...outs) + 0.5);
  const Yo = gridY(OL, olo, ohi, Math.max(1, Math.ceil((ohi - olo) / 2)), (v) => v + "°");
  if (olo < 0 && ohi > 0) o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Yo(0) + '" y2="' + Yo(0) + '"/>');
  o.push('<path d="' + path(outs, (i) => x(i + 0.5), Yo) + '" fill="none" stroke="var(--ink-2)" stroke-width="2" stroke-linecap="round"/>');

  // ---- electricity lane: hourly bars
  const EL = lane("el");
  const hourly = (tr) => { const a = []; for (let i = 0; i < tr.length; i += 4) a.push(tr.slice(i, i + 4).reduce((s, r) => s + r.el_kw * 0.25, 0)); return a; };
  const hp = hourly(rows), he = ed ? hourly(ed) : null;
  const ehi = Math.max(0.5, Math.ceil(Math.max(...hp, ...(he || [0])) * 2) / 2);
  const Ye = gridY(EL, 0, ehi, ehi > 2 ? 1 : 0.5, (v) => v);
  o.push('<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Ye(0) + '" y2="' + Ye(0) + '"/>');
  const bw = x(4) - x(0);
  const bar = (x0, w, v, col) => { if (v <= 0.005 || w <= 0) return ""; const y0 = Ye(v), h = Ye(0) - y0, r = Math.min(3, w / 2, h); return '<path d="M' + x0 + "," + Ye(0) + "V" + (y0 + r) + "Q" + x0 + "," + y0 + " " + (x0 + r) + "," + y0 + "H" + (x0 + w - r) + "Q" + (x0 + w) + "," + y0 + " " + (x0 + w) + "," + (y0 + r) + "V" + Ye(0) + 'Z" fill="' + col + '"/>'; };
  hp.forEach((v, k) => {
    const x0 = x(k * 4) + 1;
    if (he) { const w = (bw - 4) / 2; o.push(bar(x0, w, v, "var(--plan)") + bar(x0 + w + 2, w, he[k], "var(--edit)")); }
    else o.push(bar(x0, bw - 2, v, "var(--plan)"));
  });

  // ---- battery lane
  const BL = lane("soc");
  const cap = S.cfg.battery.capacity_kwh;
  const Yb = gridY(BL, 0, cap, cap / 2, (v) => v);
  const socs = [p.soc0_kwh].concat(rows.map((r) => r.soc));
  o.push('<path d="' + path(socs, x, Yb) + "L" + x(n) + "," + Yb(0) + "L" + x(0) + "," + Yb(0) + 'Z" fill="var(--plan)" opacity=".14"/>');
  o.push('<path d="' + path(socs, x, Yb) + '" fill="none" stroke="var(--plan)" stroke-width="2"/>');
  if (ed) o.push('<path d="' + path([p.soc0_kwh].concat(ed.map((r) => r.soc)), x, Yb) + '" fill="none" stroke="var(--edit)" stroke-width="2"/>');
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

  // ---- drag handles: one per block of the planned day
  const vals = S.edit || planValues(), base = planValues();
  const lockedH = lockedIdx();
  const blockSpan = [];
  S.cfg.blocks.forEach(([a, b], bi) => {
    let i0 = -1, i1 = -1;
    rows.forEach((r, j) => { if (r.t.slice(0, 10) !== p.for_date) return; const tt = parseT(r.t), hr = tt.getHours() + tt.getMinutes() / 60; if (hr >= a && hr < b) { if (i0 < 0) i0 = j; i1 = j; } });
    if (i0 >= 0 && !lockedH.has(bi)) blockSpan[bi] = [x(i0), x(i1 + 1)];
  });
  blockSpan.forEach((sp, bi) => {
    if (!sp) return;
    const cx = (sp[0] + sp[1]) / 2, cy = Yr(vals[bi]), changed = Math.abs(vals[bi] - base[bi]) > 1e-9, col = changed ? "var(--edit)" : "var(--ink-2)";
    o.push('<g class="hdl" data-i="' + bi + '" transform="translate(' + cx + "," + cy + ')" tabindex="0" role="slider" aria-label="Setpoint ' + rng(...S.cfg.blocks[bi]) + '" aria-valuemin="' + c.min_setpoint_c + '" aria-valuemax="' + c.max_setpoint_c + '" aria-valuenow="' + vals[bi] + '">' +
      '<circle r="18" fill="transparent"/><circle class="knob" r="7.5" fill="var(--surface)" stroke="' + col + '" stroke-width="2"/><path d="M-2.5,-1.6h5M-2.5,1.6h5" stroke="' + col + '" stroke-width="1.3"/>' +
      '<text class="hdl-lab" y="-13" text-anchor="middle"></text></g>');
  });
  o.push("</svg>");
  box.innerHTML = o.join("");

  const lg = ['<span><i class="sw" style="border-color:var(--plan)"></i>' + esc(chosenLabel()) + " plan</span>"];
  if (rout && p.chosen !== "Current routine") lg.push('<span><i class="sw dot" style="border-color:var(--muted)"></i>Your routine</span>');
  if (edt) lg.push('<span><i class="sw" style="border-color:var(--edit)"></i>Your edit</span>');
  if (rows[0].lwt != null) lg.push('<span><i class="sw dash" style="border-color:var(--muted)"></i>Water at offset 0</span>');
  if (orig) lg.push('<span><i class="sw dot" style="border-color:var(--s4)"></i>Setpoint as first planned</span>');
  if (meas.length && S.sel === isoDate(new Date())) lg.push('<span><i class="sw" style="border-color:var(--measured)"></i>Measured today</span>');
  if (revs.length) lg.push('<span><i class="sw box" style="background:var(--s3)"></i>H = Hermes, Y = you</span>');
  lg.push('<span><i class="sw dash" style="border-color:var(--ink-2)"></i>Setpoint</span>', '<span><i class="sw box" style="background:var(--below)"></i>Below comfort floor</span>', '<span><i class="sw box" style="background:var(--cosy)"></i>Cosy</span>', '<span><i class="sw box" style="background:var(--peak)"></i>Peak</span>');
  $("#tl-legend").innerHTML = lg.join("");
  $("#tl-foot").innerHTML = "<b>Drag a handle</b> up or down to try a different setpoint for that block. It's simulated straight away; nothing is pushed until you choose to. Window " + fmtDate(t0) + " " + hh(t0.getHours() + t0.getMinutes() / 60) + " to " + fmtDate(tN) + " " + hh(tN.getHours()) + "; starting room " + deg(p.t_room0) + ", battery " + p.soc0_kwh + " kWh" +
    (p.battery && p.battery.max_discharge_kw != null ? ", battery output capped at " + p.battery.max_discharge_kw + " kW" + (p.battery.discharge_limit_source ? " (read from " + esc(p.battery.discharge_limit_source) + ")" : "") : "") + ".";

  // hover readout
  const svg = box.querySelector("svg"), hit = $("#tl-hit"), cross = $("#tl-cross"), tip = $("#tip");
  const show = (ev) => {
    const rect = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, Math.floor(((ev.clientX - rect.left - L) / (W - L - R)) * n)));
    const r = rows[i], t = parseT(r.t), tf = tariff(t.getHours());
    cross.setAttribute("x1", x(i + 1)); cross.setAttribute("x2", x(i + 1)); cross.setAttribute("visibility", "visible");
    const cls = { home: "Home, awake", sleep: "Asleep", away: "Out" }[r.cls] || r.cls;
    const row = (col, k, v) => '<div class="tr"><span>' + (col ? '<i style="background:' + col + '"></i>' : "") + k + "</span><b>" + v + "</b></div>";
    let h = '<div class="th"><span>' + WD[wdIdx(t)] + " " + hh(t.getHours() + t.getMinutes() / 60) + '</span><span class="tag ' + tf.slot + '">' + tf.label + " " + pence(tf.rate) + "</span></div>";
    h += row("", "Occupancy", cls) + row("", "Setpoint", deg(r.target));
    h += row("var(--plan)", "Room (plan)", deg(r.room, 2));
    if (cur[i] && p.chosen !== "Current routine") h += row("var(--muted)", "Room (routine)", deg(cur[i].room, 2));
    if (ed) h += row("var(--edit)", "Room (edit)", deg(ed[i].room, 2) + (ed[i].target !== r.target ? " @" + ed[i].target + "°" : ""));
    if (r.lwt != null) h += row("", "Water target", deg(r.lwt) + " (" + sgn(r.off) + ")" + (ed && ed[i].off !== r.off ? " · edit " + sgn(ed[i].off) : ""));
    h += row("", "Outside", deg(r.outdoor)) + row("", "Pump", r.el_kw.toFixed(2) + " kW") + row("", "Battery", r.soc.toFixed(1) + " kWh");
    tip.innerHTML = h; tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let lx = ev.clientX + 14; if (lx + tw > innerWidth - 8) lx = ev.clientX - tw - 14;
    tip.style.left = Math.max(8, lx) + "px"; tip.style.top = Math.max(8, Math.min(innerHeight - th - 8, ev.clientY - th / 2)) + "px";
  };
  const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  hit.addEventListener("pointermove", show); hit.addEventListener("pointerdown", show); hit.addEventListener("pointerleave", hide);

  // drag handles
  const toVal = (clientY) => {
    const py = clientY - svg.getBoundingClientRect().top;
    const v = rlo + (1 - (py - RL.y0) / RL.h) * (rhi - rlo);
    return Math.max(c.min_setpoint_c, Math.min(c.max_setpoint_c, Math.round(v * 2) / 2));
  };
  box.querySelectorAll(".hdl").forEach((g) => {
    const bi = +g.dataset.i, sp = blockSpan[bi], cx = (sp[0] + sp[1]) / 2, lab = g.querySelector(".hdl-lab");
    let v = vals[bi], dragging = false;
    const place = (nv) => { v = nv; g.setAttribute("transform", "translate(" + cx + "," + Yr(v) + ")"); lab.textContent = v.toFixed(1) + "°"; g.setAttribute("aria-valuenow", v); };
    const commit = () => { if (Math.abs(v - vals[bi]) > 1e-9) { const nv = (S.edit || planValues()).slice(); nv[bi] = v; setEdit(nv, S.editOff); } else lab.textContent = ""; };
    g.addEventListener("pointerdown", (e) => { e.preventDefault(); e.stopPropagation(); dragging = true; g.setPointerCapture(e.pointerId); g.classList.add("dragging"); hide(); place(toVal(e.clientY)); });
    g.addEventListener("pointermove", (e) => { if (dragging) place(toVal(e.clientY)); });
    const end = () => { if (!dragging) return; dragging = false; g.classList.remove("dragging"); commit(); };
    g.addEventListener("pointerup", end); g.addEventListener("pointercancel", end);
    g.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowUp" || e.key === "ArrowRight" ? 0.5 : e.key === "ArrowDown" || e.key === "ArrowLeft" ? -0.5 : 0;
      if (!d) return; e.preventDefault();
      const nv = (S.edit || planValues()).slice(); nv[bi] = Math.max(c.min_setpoint_c, Math.min(c.max_setpoint_c, nv[bi] + d)); setEdit(nv, S.editOff);
      setTimeout(() => box.querySelector('.hdl[data-i="' + bi + '"]')?.focus(), 400);
    });
  });
}

/** e.g. ["16:00–19:00 20.0° → 20.5°, water −2 → 0"] */
function describeRev(r) {
  return (r.changed || []).map((i) => {
    const b = r.before[i], a = r.after[i], parts = [];
    if (Math.abs(b.temperature - a.temperature) > 1e-9) parts.push(b.temperature.toFixed(1) + "° → " + a.temperature.toFixed(1) + "°");
    if (b.lwt_offset !== undefined && b.lwt_offset !== a.lwt_offset) parts.push("water " + sgn(b.lwt_offset) + " → " + sgn(a.lwt_offset));
    return rng(b.from, b.to) + " " + parts.join(", ");
  });
}
function renderChanges() {
  const box = $("#changes");
  if (!box) return;
  const revs = S.revs || [];
  box.hidden = !revs.length;
  if (!revs.length) return;
  box.innerHTML = '<div class="mini-title">Changes to this day</div><ol class="changes">' + revs.slice().reverse().map((r) =>
    '<li class="by-' + esc(r.by) + '"><span class="who">' + (r.by === "hermes" ? "Hermes" : r.by === "you" ? "You" : "Planner") + "</span>" +
    '<span class="when">' + esc(r.at.slice(11, 16)) + '</span><div class="what"><b>' + esc(describeRev(r).join("; ") || r.kind) + "</b>" +
    (r.reason ? '<span class="why">' + esc(r.reason) + "</span>" : "") + "</div></li>").join("") + "</ol>";
}

/* ---------------------------------------------------------------- editor */
let simTimer = null, simSeq = 0;
function renderEditor() {
  const p = V(), base = baseVals(), vals = S.edit || base, c = S.cfg.comfort;
  const oOn = offOn(), baseO = baseOffs(), offs = S.editOff || baseO, oc = p.lwt_offset || {};
  const d = dateOf(editDate()), locked = lockedIdx(), isToday = editDate() === todayIso();
  $("#ed-reset").textContent = p.kind === "today_tail" ? "Reset" : "Reset to plan";
  $("#edit-sub").innerHTML = "Nothing goes to Home Assistant until you press Push. Editing <b>" + (isToday ? "today, " : "") + fmtDate(d) + "</b>" +
    (locked.size && locked.size < S.cfg.blocks.length ? "; blocks that have already ended are locked." : ".") +
    (p.kind === "today_tail" ? " Tomorrow's plan is left as it is." : p.kind === "outlook" ? " This is a week-ahead preview; pushing pins your edit so the 22:00 run the night before keeps it." : "") +
    (oOn ? " <b>Water</b> is the leaving-water offset: lower means cooler radiators and a better COP, but slower heating." : "");
  const un = $("#undo");
  const prev = p.manual && p.manual.previous;
  un.hidden = !(p.chosen === "Your edit" && prev);
  if (!un.hidden) {
    un.innerHTML = "<span>" + (p.manual.by === "hermes" ? "Hermes adjusted this day" : p.kind === "outlook" ? "Your edit is pinned to this day" : "You're running your own edit") + ", pushed <b>" + esc((p.manual.at || "").replace("T", " ").slice(5, 16)) + "</b>.</span>" + '<button type="button" class="btn ghost" id="undo-btn">Undo: back to ' + esc(NAMES[prev] || prev) + "</button>";
    $("#undo-btn").onclick = undoEdit;
  }
  const ed = $("#editor");
  ed.style.gridTemplateColumns = "repeat(" + S.cfg.blocks.length + ", minmax(0, 1fr))";
  const lo = c.min_setpoint_c, hi = c.max_setpoint_c;
  ed.innerHTML = S.cfg.blocks.map(([a, b], i) => {
    const v = vals[i], t = tariff(a), ch = Math.abs(v - base[i]) > 1e-9 || (oOn && offs[i] !== baseO[i]), lk = locked.has(i);
    const now = isToday && a <= nowH() && nowH() < b;
    const dis = (cond) => (lk || cond ? " disabled" : "");
    const orow = oOn ? '<div class="orow"><span class="olab">Water</span><button type="button" class="step sm" data-o="' + i + '" data-d="-1" aria-label="Cooler water ' + rng(a, b) + '"' + dis(offs[i] <= oc.min) + '>−</button><b class="oval">' + sgn(offs[i]) + '</b><button type="button" class="step sm" data-o="' + i + '" data-d="1" aria-label="Hotter water ' + rng(a, b) + '"' + dis(offs[i] >= oc.max) + ">+</button></div>" : "";
    return '<div class="blk' + (ch ? " changed" : "") + (lk ? " locked" : "") + (now ? " current" : "") + '"' + (lk ? ' title="This block has ended"' : "") + '><div class="time">' + rng(a, b) + '</div><span class="tag ' + t.slot + '">' + t.label + "</span>" +
      '<div class="bar-wrap" aria-hidden="true"><div class="bar" style="height:' + (8 + ((v - lo) / (hi - lo)) * 92) + '%"></div></div>' +
      '<div class="val">' + v.toFixed(1) + "<small>°C</small></div>" +
      '<div class="steps"><button type="button" class="step" data-i="' + i + '" data-d="-0.5" aria-label="Lower ' + rng(a, b) + '"' + dis(v <= lo) + '>−</button><button type="button" class="step" data-i="' + i + '" data-d="0.5" aria-label="Raise ' + rng(a, b) + '"' + dis(v >= hi) + ">+</button></div>" +
      orow + '<div class="was">' + (lk ? "Ended" : ch ? "was " + base[i].toFixed(1) + "°" + (oOn ? " " + sgn(baseO[i]) : "") : now ? "Now" : "") + "</div></div>";
  }).join("");
  ed.querySelectorAll(".step[data-i]").forEach((btn) => btn.addEventListener("click", () => {
    const i = +btn.dataset.i, nv = (S.edit || baseVals()).slice();
    nv[i] = Math.round((nv[i] + +btn.dataset.d) * 2) / 2;
    setEdit(nv, S.editOff);
  }));
  ed.querySelectorAll(".step[data-o]").forEach((btn) => btn.addEventListener("click", () => {
    const i = +btn.dataset.o, no = (S.editOff || baseOffs()).slice();
    no[i] = Math.max(oc.min, Math.min(oc.max, no[i] + +btn.dataset.d));
    setEdit(S.edit || baseVals().slice(), no);
  }));
  renderEdSummary();
}
function setEdit(values, offsets) {
  S.edit = values;
  if (offsets !== undefined) S.editOff = offOn() ? offsets : null;
  renderEditor();
  clearTimeout(simTimer);
  if (sameVals(values, baseVals()) && (!offOn() || !S.editOff || sameVals(S.editOff, baseOffs()))) { S.editRes = null; renderTimeline(); renderEdSummary(); return; }
  $("#ed-msg").className = "msg"; $("#ed-msg").textContent = "Simulating…";
  simTimer = setTimeout(async () => {
    const seq = ++simSeq;
    try {
      const r = await api("/api/simulate", simBody(values, S.editOff || baseOffs()));
      if (seq !== simSeq) return;
      S.editRes = r; $("#ed-msg").textContent = r.exact ? "" : "Approximate: this plan was made before what-if support, so solar gain is left out.";
    } catch (e) { $("#ed-msg").className = "msg err"; $("#ed-msg").textContent = e.message; }
    renderTimeline(); renderEdSummary();
  }, 200);
}
/** Request body for simulate/push: the server works out which kind of day the date is. */
function simBody(values, offs) {
  const b = { date: editDate(), values };
  if (offOn()) b.offsets = offs;
  return b;
}
function renderEdSummary() {
  const p = V(), ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const e = editing(), r = e ? S.editRes : ch;
  const vsPlan = (a, b, unit) => { if (!e) return ""; const d = a - b; if (Math.abs(d) < 0.005) return '<span class="d">same as plan</span>'; return '<span class="d ' + (d < 0 ? "up-good" : "up-bad") + '">' + (d < 0 ? "▼ " : "▲ ") + (unit ? Math.abs(d).toFixed(1) + " " + unit : gbp(Math.abs(d))) + (d < 0 ? " less" : " more") + " than plan</span>"; };
  const dg = r.grid_cost - cur.grid_cost;
  $("#ed-summary").innerHTML = [
    tile("Electricity", gbp(r.grid_cost), vsPlan(r.grid_cost, ch.grid_cost) || moneyDelta(r.grid_cost, cur.grid_cost)),
    tile("vs your routine", (dg > 0.005 ? "+" : "") + gbp(dg), dg <= 0.005 ? "electricity saved" : "extra electricity"),
    tile("Comfort", comfortText(r, true), e ? "plan: " + comfortText(ch, true).toLowerCase() : "routine: " + comfortText(cur, true).toLowerCase()),
    tile("Pump", r.kwh_hp.toFixed(1) + " kWh", vsPlan(r.kwh_hp, ch.kwh_hp, "kWh") || "heating only"),
    tile("Coldest awake", deg(r.t_min_awake), "floor " + deg(S.cfg.comfort.soft_floor_c)),
  ].join("");
  $("#ed-score").innerHTML = "Score <b>" + pts(scoreOf(r.total, cur.total)) + "</b>" + (e ? " (plan " + pts(scoreOf(ch.total, cur.total)) + ", your routine 100)" : " (your routine = 100)") + ". Higher is better; it counts electricity and time below your comfort floors.";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const past = dateOf(editDate()) < today;
  $("#ed-push").disabled = !e || past;
  $("#ed-push").title = past ? "This plan's day has passed. Re-run the optimiser first." : e ? "" : "Change a setpoint first";
}
async function undoEdit() {
  const p = V(), prev = p.manual.previous, vals = p.strategies[prev].values, offs = p.strategies[prev].offsets || null;
  const body = "<p>This puts the planner's <b>" + esc(NAMES[prev] || prev) + "</b> plan back into <code>" + esc(S.cfg.schedule_entity) + "</code> for " + WDL[wdIdx(dateOf(p.for_date))] + ".</p>" + strip(tidy(vals, offs));
  if (!(await confirmDlg("Undo your edit?", body, "Undo"))) return;
  const m = $("#ed-msg"); m.className = "msg"; m.textContent = "Restoring…";
  try { const r = await api("/api/restore", { date: editDate() }); await loadAll(); $("#ed-msg").className = "msg " + (r.pushed.verified ? "ok" : "err"); $("#ed-msg").textContent = r.pushed.verified ? "Restored and verified." : "Restored, but the read-back did not match."; }
  catch (e) { m.className = "msg err"; m.textContent = e.message; }
}
function wireEditor() {
  $("#ed-reset").onclick = () => setEdit(baseVals(), baseOffs());
  $("#ed-routine").onclick = () => {
    // your routine, but blocks that have already ended keep their current values
    const r = S.plan.strategies["Current routine"], lk = lockedIdx(), bv = baseVals(), bo = baseOffs();
    setEdit(r.values.map((v, i) => (lk.has(i) ? bv[i] : v)), r.offsets ? r.offsets.map((o, i) => (lk.has(i) ? bo[i] : o)) : bo);
  };
  $("#ed-rerun").onclick = () => rerun("/api/rerun", {}, $("#ed-msg"));
  $("#ed-push").onclick = async () => {
    const vals = S.edit || baseVals(), offs = offOn() ? S.editOff || baseOffs() : null, p = V();
    const blocks = tidy(vals, offs).filter((b) => b.to > (editDate() === todayIso() ? nowH() : 0));
    const body = "<p>This writes " + (editDate() === todayIso() ? "the rest of <b>today</b>" : "<b>" + WDL[wdIdx(dateOf(editDate()))] + "</b>") + " in <code>" + esc(S.cfg.schedule_entity) + "</code>. Your live schedule is not touched" + (p.kind === "today_tail" ? ", and tomorrow's plan stays as it is." : p.kind === "outlook" ? ". The edit is pinned, so the 22:00 run the night before keeps it; you can undo it." : ", and you can undo it afterwards.") + "</p>" +
      strip(blocks) + '<p style="font-variant-numeric:tabular-nums">' + blocks.map((b) => rng(b.from, b.to) + " " + b.temperature.toFixed(1) + "°" + (b.lwt_offset !== undefined ? " (water " + sgn(b.lwt_offset) + ")" : "")).join(" · ") + "</p>";
    if (!(await confirmDlg("Push your edited plan?", body, "Push"))) return;
    const m = $("#ed-msg"); m.className = "msg"; m.textContent = "Pushing…";
    try {
      const r = await api("/api/push", simBody(vals, offs));
      await loadAll();
      $("#ed-msg").className = "msg " + (r.pushed.verified ? "ok" : "err");
      $("#ed-msg").textContent = r.pushed.verified ? "Pushed and verified." : "Pushed, but the read-back did not match.";
    } catch (e) { m.className = "msg err"; m.textContent = e.message; }
  };
}

/* ---------------------------------------------------------------- strategies */
function sparkline(vals, col, lo, hi, W = 240, H = 44, floor = true) {
  const X = (i) => 2 + ((W - 4) * i) / (vals.length - 1), Y = (v) => 4 + (H - 8) * (1 - (v - lo) / (hi - lo));
  return '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" aria-hidden="true">' + (floor ? '<line class="gl" x1="0" x2="' + W + '" y1="' + Y(S.cfg.comfort.soft_floor_c) + '" y2="' + Y(S.cfg.comfort.soft_floor_c) + '" stroke-dasharray="3 3"/>' : "") + '<path d="' + vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1)).join("") + '" fill="none" stroke="' + col + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
}
function renderStrategies() {
  const p = S.plan, cur = p.strategies["Current routine"], cmp = p.compare || {};
  const t0 = parseT(p.start || p.trace_best[0].t);
  $("#st-window").textContent = fmtDate(t0) + " " + hh(t0.getHours()) + " to 04:00 two days on (about " + Math.round((p.trace_best || []).length / 4) + " h)";
  const names = ORDER.filter((n) => p.strategies[n]);
  const all = names.flatMap((n) => (cmp[n] ? cmp[n].room : []));
  const lo = Math.min(...all, S.cfg.comfort.soft_floor_c) - 0.2, hi = Math.max(...all) + 0.2;
  $("#st-cards").innerHTML = names.map((n) => {
    const r = p.strategies[n], chosen = n === p.chosen, base = n === "Current routine";
    const ex = base ? ["the setpoints from your live schedule"] : explain(r.values, cur.values, r.offsets, cur.offsets);
    const ok = r.hours_below == null ? r.penalty < 0.05 : r.hours_below < 0.25;
    return '<article class="scard' + (chosen ? " is-chosen" : "") + '"><div class="nm"><i class="sw' + (base ? " dot" : "") + '" style="border-color:' + COLOR[n] + '"></i>' + esc(NAMES[n] || n) + (chosen ? ' <span class="tag chosen">Chosen</span>' : "") + "</div>" +
      '<div><div class="k-small">Electricity</div><div class="big">' + gbp(r.grid_cost) + "</div>" + (base ? '<span class="d" style="color:var(--muted)">Baseline</span>' : moneyDelta(r.grid_cost, cur.grid_cost)) + "</div>" +
      '<div class="comfort ' + (ok ? "ok" : "low") + '"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' + (ok ? '<path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>' : '<path d="M8 3v6M8 12v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>') + "</svg>" + comfortText(r) + "</div>" +
      (cmp[n] ? sparkline(cmp[n].room, COLOR[n], lo, hi) : "") +
      strip(tidy(r.values), false) +
      '<div class="kv"><span>Pump</span><b>' + r.kwh_hp.toFixed(1) + " kWh</b>" + (offOn() && r.offsets ? "<span>Water offset</span><b>" + offSummary(r.offsets) + "</b>" : "") + "<span>Coldest awake</span><b>" + deg(r.t_min_awake) + "</b><span>Score</span><b>" + pts(scoreOf(r.total, cur.total)) + " pts</b></div>" +
      '<p class="why">' + (ex.length ? esc(ex.slice(0, 4).join("; ")) : "Same setpoints as your routine.") + "</p>" +
      '<button type="button" class="btn ghost" data-try="' + esc(n) + '">Open in editor</button></article>';
  }).join("");
  document.querySelectorAll("[data-try]").forEach((b) => b.addEventListener("click", () => {
    const st = p.strategies[b.dataset.try];
    const go = () => setEdit(st.values.slice(), st.offsets ? st.offsets.slice() : null);
    if (S.view) selectDay(S.plan.for_date).then(go); else go();
    if (MOBILE()) setTab("plan"); else $("#edit").scrollIntoView();
  }));
}
function renderBreakdown() {
  const p = S.plan, cmp = p.compare, box = $("#breakdown");
  if (!box.offsetParent) return;
  const chName = cmp && cmp[p.chosen] ? p.chosen : "Optimised";
  if (!cmp || !cmp["Current routine"] || chName === "Current routine") { box.innerHTML = '<p class="foot">The routine was kept, so there is nothing to break down.</p>'; $("#br-sub").textContent = ""; return; }
  const comps = [["cosy", "Cosy-rate electricity"], ["standard", "Standard-rate electricity"], ["peak", "Peak-rate electricity"], ["penalty", "Comfort shortfall"], ["terminal", "Heat left in the house"], ["battery", "Battery left over"]];
  const a = cmp["Current routine"].breakdown, b = cmp[chName].breakdown;
  const rows = comps.map(([k, l]) => ({ l, v: a[k] - b[k] }));
  const R = p.strategies["Current routine"].total;
  rows.forEach((r) => (r.v = R > 0.1 ? (100 * r.v) / R : 0));      // £ → points (routine = 100)
  const net = rows.reduce((s, r) => s + r.v, 0);
  $("#br-sub").textContent = "Points by part: " + NAMES[chName] + " (" + pts(scoreOf(p.strategies[chName].total)) + ") vs your routine (100)";
  const W = Math.max(280, box.clientWidth || 600), narrow = W < 560;
  const LW = narrow ? 140 : 200, VW = 60, rowH = 30;
  const m = Math.max(0.05, ...rows.map((r) => Math.abs(r.v)));
  const cx = LW + (W - LW - VW) / 2, sc = (W - LW - VW) / 2 / m;
  const H = rows.length * rowH + 30;
  const o = ['<svg width="' + W + '" height="' + H + '" role="img" aria-label="Score difference by component">'];
  o.push('<text class="ax" x="' + (cx - 6) + '" y="12" text-anchor="end">◀ worse</text><text class="ax" x="' + (cx + 6) + '" y="12">better ▶</text>');
  rows.forEach((r, i) => {
    const y = 22 + i * rowH, w = Math.abs(r.v) * sc;
    o.push('<text x="0" y="' + (y + 15) + '" style="fill:var(--ink-2);font-size:' + (narrow ? 12 : 13) + 'px">' + r.l + "</text>");
    if (w > 0.5) o.push('<rect x="' + (r.v >= 0 ? cx : cx - w) + '" y="' + (y + 4) + '" width="' + w + '" height="16" rx="3" fill="' + (r.v >= 0 ? "var(--plan)" : "var(--crit)") + '"><title>' + r.l + ": " + Math.abs(r.v).toFixed(1) + " points " + (r.v >= 0 ? "better" : "worse") + "</title></rect>");
    o.push('<text x="' + W + '" y="' + (y + 16) + '" text-anchor="end" style="fill:var(--ink);font-size:13px;font-variant-numeric:tabular-nums">' + (r.v >= 0 ? "+" : "−") + Math.abs(r.v).toFixed(1) + " pts</text>");
  });
  o.push('<line class="base" x1="' + cx + '" x2="' + cx + '" y1="18" y2="' + (H - 6) + '"/></svg>');
  const nb = p.strategies["Optimised (no battery, for reference)"];
  box.innerHTML = o.join("") + '<p class="foot">Net ' + (net >= 0 ? "+" : "−") + Math.abs(net).toFixed(0) + " points." + (nb ? " Without the battery the optimised plan would need " + gbp(nb.grid_cost) + " of electricity." : "") + "</p>";
}

/* ---------------------------------------------------------------- week */
function renderWeek() {
  const p = S.plan, occ = S.cfg.occupancy;
  const stLabel = (s) => ({ home: "Home all day", away_day: "Out " + rng(occ.away_from, occ.away_to), away_all: "Away all day" }[s] || s);
  const ch = p.strategies[p.chosen], cur = p.strategies["Current routine"];
  const cards = [{ date: p.for_date, status: p.occupancy?.status_tomorrow, mean: p.forecast_tomorrow.mean, grid: ch.grid_cost, curGrid: cur.grid_cost, hb: ch.hours_below, pen: ch.penalty, blocks: p.blocks, kwh: ch.kwh_hp, first: true }]
    .concat((p.outlook || []).filter((o) => !o.error).map((o) => ({ date: o.date, status: o.status, mean: o.forecast_mean, grid: o.grid_cost, curGrid: o.current_grid_cost, hb: o.hours_below, pen: o.penalty, blocks: o.blocks, kwh: o.kwh_hp, edited: o.edited })));
  $("#week-cards").innerHTML = cards.map((c) => {
    const d = dateOf(c.date);
    const delta = c.curGrid != null ? (Math.abs(c.curGrid - c.grid) < 0.005 ? '<div class="meta">Same as routine</div>' : '<div class="meta ' + (c.grid < c.curGrid ? "up-good" : "up-bad") + '">' + (c.grid < c.curGrid ? "▼ " + gbp(c.curGrid - c.grid) + " less" : "▲ " + gbp(c.grid - c.curGrid) + " more") + " than routine</div>") : "";
    return '<button type="button" class="day' + (c.date === S.sel ? " first" : "") + '" data-day="' + c.date + '" aria-label="Open ' + fmtDate(d) + '"><div class="dn">' + WD[wdIdx(d)] + " " + d.getDate() + "<span>" + deg(c.mean, 0) + "</span></div>" +
      '<div class="meta">' + esc(stLabel(c.status)) + (c.first ? ' · <b class="pushed">pushed</b>' : "") + "</div>" +
      '<div class="cost">' + gbp(c.grid) + "<small> electricity</small></div>" + delta +
      strip(c.blocks, false) + '<div class="meta">' + (c.kwh != null ? Math.round(c.kwh) + " kWh pump · " : "") + comfortText({ hours_below: c.hb, penalty: c.pen }, true).toLowerCase() + (c.edited ? ' · <b class="pushed">your edit</b>' : "") + "</div></button>";
  }).join("");
  document.querySelectorAll("#week-cards [data-day]").forEach((b) => b.addEventListener("click", async () => {
    await selectDay(b.dataset.day); renderWeek();
    if (MOBILE()) { history.replaceState(null, "", "#plan"); setTab("plan"); } else $("#plan").scrollIntoView();
  }));
  $("#sp-scale").innerHTML = "<span>Setpoint</span><span>16°</span><span class='ramp' style='background:linear-gradient(90deg," + seqColor(16).bg + "," + seqColor(22).bg + ")'></span><span>22°</span><span>· electricity for each 30 h window incl. house load</span>";
}

/* ---------------------------------------------------------------- occupancy (two-week calendar) */
const OCC_NEXT = { home: "away_day", away_day: "away_all", away_all: "home" };
const OCC_LABEL = { home: "Home", away_day: "Out", away_all: "Away" };
function renderOcc() {
  const occ = S.cfg.occupancy;
  if (!S.occ) { S.occ = { aw: occ.away_weekdays.slice(), ov: { ...(occ.overrides || {}) } }; $("#occ-from").value = occ.away_from; $("#occ-to").value = occ.away_to; }
  $("#occ-days").innerHTML = WD.map((n, i) => '<button type="button" class="tg" data-wd="' + i + '" aria-pressed="' + S.occ.aw.includes(i) + '">' + n + "</button>").join("");
  $("#occ-days").querySelectorAll(".tg").forEach((b) => b.addEventListener("click", () => {
    const i = +b.dataset.wd; S.occ.aw = S.occ.aw.includes(i) ? S.occ.aw.filter((x) => x !== i) : S.occ.aw.concat(i); renderOcc();
  }));
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - wdIdx(today));
  const end = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 13);
  let h = WD.map((n) => '<div class="cal-h">' + n.slice(0, 2) + "</div>").join("");
  for (let d = new Date(start); d <= end || wdIdx(d) !== 0; d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)) {
    const iso = isoDate(d);
    if (d < today || d > end) { h += '<div class="cal-d off"><span class="dd">' + d.getDate() + "</span></div>"; continue; }
    const usual = S.occ.aw.includes(wdIdx(d)) ? "away_day" : "home";
    const eff = S.occ.ov[iso] || usual;
    const changed = eff !== usual;
    h += '<button type="button" class="cal-d ' + eff + (changed ? " ov" : "") + (iso === isoDate(today) ? " today" : "") + '" data-iso="' + iso + '" aria-label="' + fmtDate(d) + ": " + OCC_LABEL[eff] + (changed ? " (changed from usual)" : "") + '">' +
      '<span class="dd">' + d.getDate() + (d.getDate() === 1 || iso === isoDate(today) ? " " + MON[d.getMonth()] : "") + '</span><span class="st">' + OCC_LABEL[eff] + "</span></button>";
  }
  $("#occ-cal").innerHTML = h;
  $("#occ-cal").querySelectorAll("button.cal-d").forEach((b) => b.addEventListener("click", () => {
    const iso = b.dataset.iso, usual = S.occ.aw.includes(wdIdx(dateOf(iso))) ? "away_day" : "home";
    const nxt = OCC_NEXT[S.occ.ov[iso] || usual];
    if (nxt === usual) delete S.occ.ov[iso]; else S.occ.ov[iso] = nxt;
    renderOcc();
    $('#occ-cal [data-iso="' + iso + '"]')?.focus();
  }));
  const hint = S.params?.info?.presence_hint || {};
  const hs = Object.entries(hint).map(([k, v]) => WD[+k] + " " + Math.round(v.home_share * 100) + "%");
  $("#occ-hint").textContent = hs.length ? "From your phone's location, share of " + rng(occ.away_from, occ.away_to) + " spent at home recently: " + hs.join(" · ") + "." : "";
}
function wireOcc() {
  $("#occ-save").onclick = () => {
    const ov = {};
    $("#occ-cal").querySelectorAll("button.cal-d").forEach((b) => { const iso = b.dataset.iso, usual = S.occ.aw.includes(wdIdx(dateOf(iso))) ? "away_day" : "home"; ov[iso] = S.occ.ov[iso] || usual; });
    Object.entries(S.occ.ov).forEach(([k, v]) => { if (!(k in ov)) ov[k] = v; });
    rerun("/api/occupancy", { away_weekdays: S.occ.aw.slice().sort(), away_from: +$("#occ-from").value, away_to: +$("#occ-to").value, overrides: ov }, $("#occ-msg"));
  };
}
async function rerun(path, body, m) {
  m.className = "msg"; m.textContent = path === "/api/occupancy" ? "Saving…" : "Starting…";
  try { await api(path, body); } catch (e) { m.className = "msg err"; m.textContent = e.message; return; }
  m.textContent = "Re-running the model (about a minute)…";
  for (let i = 0; i < 160; i++) {
    await new Promise((z) => setTimeout(z, 1500));
    const s = await api("/api/rerun-status").catch(() => ({}));
    if (s.state === "done") { await loadAll(); m.className = "msg ok"; m.textContent = "Done. New plan pushed."; return; }
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
  $("#maturity").innerHTML = ico(mature ? "good" : "warn") + "<div>" + (mature ? "<b>Settled.</b> " + days + " days of data and daily error within 15%. The figures are reasonable estimates." :
    "<b>Early days.</b> " + (days != null ? days + " days of data" : "Not much data yet") + (mae != null ? ", daily error about " + mae.toFixed(1) + " kWh on roughly " + act.toFixed(0) + " kWh" : "") + ". Treat the savings as rough until about 4 weeks of data are in.") + "</div>";
  $("#model-tiles").innerHTML = [
    tile("Heat loss", (pr.ua_kw_per_k ?? 0).toFixed(3) + " kW/K", "≈ " + ((pr.ua_kw_per_k || 0) * 20).toFixed(1) + " kW at 0° out, 20° in"),
    tile("Free gains", (pr.gains_kw ?? 0).toFixed(2) + " kW", "people, appliances, base solar"),
    tile("Thermal mass", (pr.c_kwh_per_k ?? 0).toFixed(1) + " kWh/K", "seen by the thermostat room"),
    tile("Backtest error", mae != null ? mae.toFixed(1) + " kWh/day" : "–", info.heat_loss_r2 != null ? "heat-loss fit R² " + info.heat_loss_r2 : bt.length + " days tested"),
  ].join("");
  $("#model-notes").innerHTML = (pj.notes || []).map((n) => "<li>" + esc(n) + "</li>").join("") + "<li>Last fit " + esc((pj.fitted_at || "never").replace("T", " ")) + "</li>";
  const hist = (pj.history || []).map((h) => h.params.ua_kw_per_k);
  $("#ua-hist").innerHTML = hist.length > 1 ? sparkline(hist, "var(--plan)", Math.min(...hist) - 0.005, Math.max(...hist) + 0.005, 300, 60, false) + '<div class="foot">' + hist[0].toFixed(3) + " → " + hist[hist.length - 1].toFixed(3) + " over " + hist.length + " fits</div>" : '<p class="foot">Needs a few daily fits.</p>';
}
function renderBacktest(bt) {
  const box = $("#backtest");
  if (!box.offsetParent) return;
  if (!bt.length) { box.innerHTML = '<p class="foot">No complete days to test yet.</p>'; return; }
  $("#bt-legend").innerHTML = '<span><i class="sw box" style="background:var(--plan)"></i>Model</span><span><i class="sw box" style="background:var(--measured)"></i>Measured</span>';
  const W = Math.max(280, box.clientWidth || 500), H = 190, L = 34, R = 6, T = 10, B = 34;
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
  if (!a || !box || !box.offsetParent) return;
  const d = dateOf(a.date);
  $("#act-title").textContent = fmtDate(d) + ": room temperature";
  const act = a.actual.filter((r) => r.room != null), pred = (a.predicted || []).filter((r) => r.room != null);
  if (!act.length) { box.innerHTML = '<p class="foot">No measurements stored for ' + fmtDate(d) + " yet.</p>"; return; }
  $("#act-legend").innerHTML = (pred.length ? '<span><i class="sw" style="border-color:var(--plan)"></i>Predicted</span>' : "") + '<span><i class="sw" style="border-color:var(--measured)"></i>Measured</span>';
  const W = Math.max(280, box.clientWidth || 500), H = 190, L = 34, R = 8, T = 10, B = 24;
  const vals = act.map((r) => r.room).concat(pred.map((r) => r.room));
  const lo = Math.floor(Math.min(...vals) - 0.3), hi = Math.ceil(Math.max(...vals) + 0.3);
  const X = (h) => L + ((W - L - R) * h) / 23, Y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const o = ['<svg width="' + W + '" height="' + H + '" role="img" aria-label="Predicted and measured room temperature">'];
  for (let v = lo; v <= hi; v += hi - lo > 4 ? 2 : 1) o.push('<line class="gl" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + v + "°</text>");
  for (let h = 0; h < 24; h += 3) o.push('<text class="ax" x="' + X(h) + '" y="' + (H - 6) + '" text-anchor="middle">' + String(h).padStart(2, "0") + "</text>");
  const line = (rs, col) => '<path d="' + rs.map((r, i) => (i ? "L" : "M") + X(r.h) + "," + Y(r.room)).join("") + '" fill="none" stroke="' + col + '" stroke-width="2" stroke-linejoin="round"/>';
  if (pred.length) o.push(line(pred, "var(--plan)"));
  o.push(line(act, "var(--measured)"));
  act.forEach((r) => o.push('<circle cx="' + X(r.h) + '" cy="' + Y(r.room) + '" r="4" fill="var(--measured)" stroke="var(--surface)" stroke-width="2"><title>' + hh(r.h) + " measured " + r.room + "°</title></circle>"));
  o.push("</svg>");
  box.innerHTML = o.join("");
  const ka = a.actual.reduce((s, r) => s + (r.kwh || 0), 0), kp = (a.predicted || []).reduce((s, r) => s + (r.kwh || 0), 0);
  let err = "";
  if (pred.length) { const m = {}; pred.forEach((r) => (m[r.h] = r.room)); const e = act.filter((r) => m[r.h] != null).map((r) => r.room - m[r.h]); if (e.length) { const av = e.reduce((s, v) => s + v, 0) / e.length; err = " Measured ran " + Math.abs(av).toFixed(1) + "° " + (av >= 0 ? "warmer" : "cooler") + " than predicted on average."; } }
  $("#act-foot").textContent = "Pump electricity: measured " + ka.toFixed(1) + " kWh" + (pred.length ? ", predicted " + kp.toFixed(1) + " kWh (" + (a.strategy ? NAMES[a.strategy] || a.strategy : "") + ")." : ". No stored plan for this day.") + err;
}
function renderMeter() {
  const a = S.actuals, box = $("#meter");
  if (!box || !S.cfg) return;
  const g = a && a.grid;
  if (!g) {
    $("#meter-sub").textContent = "not set up";
    box.innerHTML = '<p class="sub">Add your Anker smart meter\'s grid-import energy sensor (kWh) to <code>config.json</code> as <code>"entities": {"grid_import_kwh": "sensor.…"}</code>, and optionally live grid power as <code>"grid_power"</code>. After the next 03:30 data run this panel shows what the house actually drew from the grid each day and what that cost at your Cosy rates.</p>';
    return;
  }
  const d = dateOf(a.date);
  $("#meter-sub").textContent = fmtDate(d) + (g.hours < 24 ? " · " + g.hours + " of 24 hours recorded" : "");
  const s = g.kwh_by_slot, tot = g.kwh || 1;
  const seg = (k, lab) => '<div class="mseg ' + k + '" style="flex:' + Math.max(0.0001, s[k]) + '" title="' + lab + " " + s[k].toFixed(1) + ' kWh"></div>';
  box.innerHTML = '<div class="tiles three">' + tile("Grid import", g.kwh.toFixed(1) + " kWh", "whole house, from the meter") + tile("Cost at your rates", gbp(g.cost), "standing charge not included") +
    tile("Bought at Cosy rate", Math.round((s.cosy / tot) * 100) + "%", "the higher the better") + "</div>" +
    '<div class="mbar">' + seg("cosy", "Cosy") + seg("standard", "Standard") + seg("peak", "Peak") + "</div>" +
    '<div class="legend" style="margin-top:6px"><span><i class="sw box" style="background:var(--cosy-solid)"></i>Cosy ' + s.cosy.toFixed(1) + ' kWh</span><span><i class="sw box" style="background:var(--muted)"></i>Standard ' + s.standard.toFixed(1) + ' kWh</span><span><i class="sw box" style="background:var(--crit)"></i>Peak ' + s.peak.toFixed(1) + " kWh</span></div>";
}

/* ---------------------------------------------------------------- history */
function renderHistory() {
  const idx = S.idx.slice().reverse();
  const scored = idx.filter((i) => i.score != null);
  const avg = scored.length ? Math.round(scored.reduce((s, i) => s + i.score, 0) / scored.length) : null;
  const withE = idx.filter((i) => i.electricity_vs_routine_gbp != null);
  const elec = withE.reduce((s, i) => s + i.electricity_vs_routine_gbp, 0);
  const first = idx.length ? dateOf(idx[idx.length - 1].for_date) : null;
  $("#hist-tiles").innerHTML = [
    tile("Average score", avg != null ? avg + " pts" : "–", scored.length ? "over " + scored.length + " plans · your routine = 100" : "scores appear from the next plan"),
    tile("Electricity vs your routine", withE.length ? (Math.abs(elec) < 0.005 ? "same" : gbp(Math.abs(elec)) + (elec > 0 ? " less" : " more")) : "–",
         withE.length ? withE.length + " plans since " + (first ? fmtDate(first) : "–") + " · model estimate" : "appears from the next plan"),
  ].join("");
  $("#hist-table").innerHTML = "<thead><tr><th>Day</th><th>Plan</th><th>Setpoints</th><th class='r'>Score</th><th>Pushed</th></tr></thead><tbody>" +
    idx.slice(0, 14).map((i) => "<tr><td>" + fmtDate(dateOf(i.for_date)) + "</td><td>" + esc(NAMES[i.chosen] || i.chosen) + "</td><td>" + strip(i.blocks || [], false) + "</td><td class='r'>" + (i.score != null ? i.score + " pts" : "–") + "</td><td>" + (i.pushed ? (i.pushed.verified ? "✓ verified" : "unverified") : "preview") + "</td></tr>").join("") + "</tbody>";
  $("#log").innerHTML = S.log.slice().reverse().map((l) => {
    const rest = Object.entries(l).filter(([k]) => k !== "at" && k !== "event").map(([k, v]) => k + "=" + (typeof v === "object" ? JSON.stringify(v) : v)).join(" ");
    return '<li class="' + (l.event === "error" ? "e-error" : "") + '">' + esc((l.at || "").replace("T", " ")) + " <b>" + esc(l.event) + "</b> " + esc(rest.slice(0, 200)) + "</li>";
  }).join("");
}

/* ---------------------------------------------------------------- Hermes chat */
let chatTimer = null;
function chatOpen() { return !$("#chat").hidden; }
function openChat() {
  $("#chat").hidden = false; $("#orb").setAttribute("aria-expanded", "true");
  try { localStorage.setItem("hp-chat-seen", String(S.chatSeen = lastId())); } catch (e) { S.chatSeen = lastId(); }
  renderChat(); $("#orb-badge").hidden = true;
  setTimeout(() => $("#chat-text").focus(), 50);
  pollChat(); restartChatTimer();
}
function closeChat() { $("#chat").hidden = true; $("#orb").setAttribute("aria-expanded", "false"); restartChatTimer(); }
function lastId() { return S.chat.length ? S.chat[S.chat.length - 1].id : 0; }
function agoText(iso) {
  if (!iso) return "not connected yet";
  const m = Math.round((Date.now() - new Date(iso.slice(0, 19)).getTime()) / 60000);
  return m < 2 ? "online" : m < 90 ? "last checked in " + m + " min ago" : "last checked in " + iso.slice(11, 16) + (iso.slice(0, 10) === isoDate(new Date()) ? "" : " on " + iso.slice(5, 10));
}
async function pollChat() {
  try {
    const r = await api("/api/chat?since=" + lastId());
    S.agent = r.agent || {};
    if (r.messages.length) { S.chat = S.chat.concat(r.messages); renderChat(); if (S.plan) renderNow(); }
  } catch (e) { /* offline */ }
  $("#chat-status").textContent = agoText(S.agent.last_seen);
  const unread = S.chat.some((m) => m.id > S.chatSeen && m.from !== "you");
  if (chatOpen()) { S.chatSeen = lastId(); try { localStorage.setItem("hp-chat-seen", String(S.chatSeen)); } catch (e) { /* ignore */ } }
  $("#orb-badge").hidden = chatOpen() || !unread;
  $("#orb").classList.toggle("busy", S.chat.length > 0 && S.chat[S.chat.length - 1].from === "you");
}
function restartChatTimer() { clearInterval(chatTimer); chatTimer = setInterval(pollChat, chatOpen() ? 4000 : 30000); }
function renderChat() {
  const log = $("#chat-log");
  if (!S.chat.length) { log.innerHTML = '<li class="empty">Ask Hermes anything about today\'s heating. Hermes also posts here whenever it changes the plan, and says why.</li>'; return; }
  log.innerHTML = S.chat.slice(-80).map((m) => '<li class="from-' + esc(m.from) + " kind-" + esc(m.kind) + '">' + esc(m.text) + "<time>" + esc(m.at.slice(11, 16)) + (m.at.slice(0, 10) !== isoDate(new Date()) ? " · " + esc(m.at.slice(5, 10)) : "") + "</time></li>").join("");
  const pend = S.chat[S.chat.length - 1];
  if (pend.from === "you") log.insertAdjacentHTML("beforeend", '<li class="empty">' + (S.agent.last_seen && (Date.now() - new Date(S.agent.last_seen.slice(0, 19))) < 3 * 60000 ? "Hermes is thinking…" : "Sent. Hermes will reply when it next checks in.") + "</li>");
  log.scrollTop = log.scrollHeight;
}
function wireChat() {
  try { S.chatSeen = +localStorage.getItem("hp-chat-seen") || 0; } catch (e) { S.chatSeen = 0; }
  $("#orb").onclick = () => (chatOpen() ? closeChat() : openChat());
  $("#chat-close").onclick = closeChat;
  addEventListener("keydown", (e) => { if (e.key === "Escape" && chatOpen()) closeChat(); });
  const ta = $("#chat-text");
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(120, ta.scrollHeight) + "px"; });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); } });
  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    ta.value = ""; ta.style.height = "auto";
    try { const m = await api("/api/chat", { text }); S.chat.push(m); S.chatSeen = m.id; renderChat(); $("#orb").classList.add("busy"); }
    catch (err) { ta.value = text; $("#chat-status").textContent = "Couldn't send: " + err.message; }
  });
  pollChat(); restartChatTimer();
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
    root.dataset.theme = isDark() ? "light" : "dark";
    try { localStorage.setItem("hp-theme", root.dataset.theme); } catch (e) { /* ignore */ }
    if (S.plan) render();
  };
}
function wireTabs() {
  document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => { history.replaceState(null, "", "#" + b.dataset.go); setTab(b.dataset.go); }));
  document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", () => setTab(tabForHash(a.getAttribute("href")) || "now", false)));
  addEventListener("hashchange", () => { const t = tabForHash(location.hash); if (t) setTab(t, false); });
  $("#score-fold").addEventListener("toggle", () => renderBreakdown());
  setTab(tabForHash(location.hash) || "now", false);
}
let rz = null, wasMobile = MOBILE();
addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (MOBILE() !== wasMobile) { wasMobile = MOBILE(); setTab(document.body.dataset.tab, false); } else drawCharts(); }, 150); });
wireTheme(); wireEditor(); wireOcc(); wireTabs(); wireChat();
loadAll().catch((e) => { $("#loading").textContent = "Could not load the plan: " + e.message; });
setInterval(loadStatus, 60000);
setInterval(() => { if (!S.edit && document.visibilityState === "visible" && !$("#dlg").open) loadAll(); }, 300000);
if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
