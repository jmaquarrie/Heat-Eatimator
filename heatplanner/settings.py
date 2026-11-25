"""Settings page: every adjustable factor of the model, with validation, pinned model parameters, a change
log, and a "preview impact" that re-scores the current plan under proposed settings before saving."""
import copy
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import data, model

LOCAL = ZoneInfo("Europe/London")
LOG = "settings_log.json"
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# kind: num | int | bool | text | hours (list of [from,to] ranges) | days (weekday list) | bounds (block edges)
#       | times (HH:MM list) | bins (outdoor-temperature -> COP map) | entity
# path "params.<key>" = model parameter (fitted daily; setting it pins it)
SECTIONS = [
    {"id": "comfort", "title": "Comfort", "intro": "What counts as warm enough, and how hard the planner works to stay there.", "fields": [
        ("comfort.target_c", "num", "Comfortable temperature", "°C", 17, 23, 0.5, "Used by the Steady strategy and as your everyday target."),
        ("comfort.soft_floor_c", "num", "Comfort floor", "°C", 15, 22, 0.1, "Below this while you're home and awake counts as \"below comfort\"."),
        ("comfort.hard_floor_c", "num", "Hard floor", "°C", 14, 22, 0.1, "Below this is penalised much more heavily."),
        ("comfort.sleep_floor_c", "num", "Overnight floor", "°C", 12, 21, 0.1, "Minimum while asleep."),
        ("comfort.away_floor_c", "num", "Away floor", "°C", 8, 20, 0.5, "Minimum while nobody is home."),
        ("comfort.soft_penalty_gbp_per_kh", "num", "Cost of being a bit cold", "£ per °C·h", 0, 2, 0.01, "How much 1 °C below the comfort floor for an hour is worth avoiding. Raise it to favour comfort over cost."),
        ("comfort.hard_penalty_gbp_per_kh", "num", "Cost of being properly cold", "£ per °C·h", 0, 5, 0.05, "The same below the hard, overnight and away floors."),
        ("comfort.end_of_day_c", "num", "End-of-day reference", "°C", 15, 22, 0.5, "Heat below this left at 04:00 the day after is charged for, so plans don't run the house down at the end."),
        ("comfort.min_setpoint_c", "num", "Lowest setpoint allowed", "°C", 10, 20, 0.5, "The planner and editor never go below this."),
        ("comfort.max_setpoint_c", "num", "Highest setpoint allowed", "°C", 18, 26, 0.5, "The planner and editor never go above this."),
    ]},
    {"id": "routine", "title": "Your day", "intro": "When you sleep and when you're usually out. Specific days are set in Who's home.", "fields": [
        ("occupancy.sleep_from", "num", "Bedtime", "h", 18, 24, 0.5, "Hour (0–24) the overnight floor starts."),
        ("occupancy.sleep_to", "num", "Get up", "h", 3, 11, 0.5, "Hour the overnight floor ends."),
        ("occupancy.away_weekdays", "days", "Usually out in the daytime on", "", None, None, None, "Weekdays you're normally out."),
        ("occupancy.away_from", "num", "Out from", "h", 0, 24, 0.5, ""),
        ("occupancy.away_to", "num", "Back at", "h", 0, 24, 0.5, ""),
        ("fire.weekdays", "days", "Log fire on", "", None, None, None, "Days the log fire is usually lit."),
        ("fire.from", "num", "Fire lit from", "h", 0, 24, 0.5, ""),
        ("fire.to", "num", "Fire out by", "h", 0, 24, 0.5, ""),
        ("fire.extra_gain_kw", "num", "Heat from the fire", "kW", 0, 6, 0.1, "Extra heat the fire adds to the thermostat room."),
        ("house_load_kw", "num", "House electricity (excl. heat pump)", "kW", 0, 3, 0.05, "Average background load the battery also covers."),
    ]},
    {"id": "tariff", "title": "Tariff", "intro": "Octopus Cosy prices and windows.", "fields": [
        ("tariff.auto_detect", "bool", "Detect prices automatically", "", None, None, None, "Re-read the three prices from your Octopus rate history after each data run. Turn off to use the prices below."),
        ("tariff.rate_cosy", "num", "Cosy price", "£/kWh", 0, 1, 0.0001, ""),
        ("tariff.rate_standard", "num", "Standard price", "£/kWh", 0, 1, 0.0001, ""),
        ("tariff.rate_peak", "num", "Peak price", "£/kWh", 0, 2, 0.0001, ""),
        ("tariff.cosy", "hours", "Cosy windows", "", None, None, None, "e.g. 4-7, 13-16, 22-24"),
        ("tariff.peak", "hours", "Peak windows", "", None, None, None, "e.g. 16-19"),
    ]},
    {"id": "battery", "title": "Battery", "intro": "The Anker Solix as the model sees it (it's managed in the Anker app).", "fields": [
        ("battery.enabled", "bool", "Include the battery", "", None, None, None, "Turn off to plan as if there were no battery."),
        ("battery.capacity_kwh", "num", "Capacity", "kWh", 0, 50, 0.1, ""),
        ("battery.reserve_kwh", "num", "Reserve kept back", "kWh", 0, 20, 0.1, ""),
        ("battery.max_charge_kw", "num", "Max charge rate", "kW", 0, 10, 0.1, ""),
        ("battery.max_discharge_kw", "num", "Max discharge rate", "kW", 0, 10, 0.1, ""),
        ("battery.rte", "num", "Round-trip efficiency", "0–1", 0.5, 1, 0.01, "0.90 = 90% of the energy put in comes back out."),
    ]},
    {"id": "water", "title": "Water temperature (leaving water offset)", "intro": "How far the planner may move the Daikin's weather curve.", "fields": [
        ("lwt_offset.enabled", "bool", "Plan the water offset", "", None, None, None, "Off = setpoints only, offset left as it is."),
        ("lwt_offset.min", "int", "Lowest offset", "°C", -10, 0, 1, "Cooler water = better efficiency but slower heating."),
        ("lwt_offset.max", "int", "Highest offset", "°C", 0, 10, 1, ""),
        ("lwt_offset.write_cost_gbp", "num", "Cost of each offset change", "£", 0, 0.5, 0.005, "Discourages changing the offset for tiny gains (each change uses Daikin API calls)."),
        ("params.lwc_a", "num", "Weather curve: water at 0 °C outside", "°C", 20, 55, 0.1, "Fitted from your water temperature history."),
        ("params.lwc_b", "num", "Weather curve: slope", "°C per °C", -2, 0, 0.01, "How much hotter the water runs per degree colder outside (negative)."),
        ("params.k_emit_kw_per_k", "num", "Radiator output", "kW per °C", 0.1, 4, 0.01, "Heat the radiators give per degree of water above room temperature. Lower makes cool water slower to heat the house."),
    ]},
    {"id": "house", "title": "House and heat pump", "intro": "The physics the planner simulates. These are fitted from your data every night; setting one pins it.", "fields": [
        ("params.ua_kw_per_k", "num", "Heat loss", "kW per °C", 0.05, 1.5, 0.001, "Heat lost per degree between inside and outside."),
        ("params.c_kwh_per_k", "num", "Thermal mass", "kWh per °C", 1, 60, 0.1, "How much heat it takes to warm the thermostat room by 1 °C. Higher = slower to warm and cool."),
        ("params.gains_kw", "num", "Free heat", "kW", 0, 5, 0.01, "People, appliances and base sunshine."),
        ("params.gains_solar", "num", "Sunshine gain", "kW per kW/m²", 0, 20, 0.1, "Extra heat from sun through the windows."),
        ("params.outdoor_bias_c", "num", "Outdoor sensor correction", "°C", -5, 5, 0.01, "Added to the forecast to match the heat pump's own outdoor sensor."),
        ("params.qmax_a7_kw", "num", "Heat pump capacity at 7 °C", "kW", 2, 20, 0.1, ""),
        ("params.qmax_slope_kw_per_k", "num", "Capacity lost per °C colder", "kW", 0, 1, 0.01, ""),
        ("params.cop_blend_published", "num", "COP: weight on Daikin's figures", "0–1", 0, 1, 0.05, "0 = only your measured COP, 1 = only Daikin's published curve."),
        ("params.cop_measured", "bins", "Measured COP by outdoor temperature", "", None, None, None, "Outdoor °C : COP, e.g. 0:4.36, 2:4.6"),
        ("params.terminal_cop", "num", "Efficiency for valuing end-of-day heat", "COP", 1.5, 7, 0.1, ""),
    ]},
    {"id": "planner", "title": "Planner", "intro": "How the optimiser searches and when it changes your routine.", "fields": [
        ("optimiser.min_gain_points", "num", "Only change my routine for at least", "points", 0, 20, 0.1, "Keep your routine unless the optimised plan scores this much better (routine = 100)."),
        ("optimiser.setpoint_step", "num", "Setpoint step", "°C", 0.5, 1, 0.5, "0.5 = try every half degree."),
        ("optimiser.passes", "int", "Search effort", "passes", 1, 8, 1, "More passes = slower but more thorough."),
        ("blocks", "bounds", "Time blocks", "", None, None, None, "Hours where the day is split, e.g. 0,4,7,13,16,19,22,24. Changing this needs a re-plan."),
        ("outlook.start_room_c", "num", "Week ahead: room at 22:00 the night before", "°C", 14, 23, 0.1, ""),
        ("outlook.start_battery_kwh", "num", "Week ahead: battery at 22:00", "kWh", 0, 50, 0.1, ""),
        ("publish_sensors", "bool", "Publish sensor.heatplanner_* to Home Assistant", "", None, None, None, ""),
    ]},
    {"id": "hermes", "title": "Hermes", "intro": "Limits on what Hermes may change on its own.", "fields": [
        ("agent.max_adjustments_per_day", "int", "Most adjustments a day", "", 0, 30, 1, "0 stops Hermes adjusting."),
        ("agent.min_minutes_between", "int", "Minimum gap between adjustments", "min", 0, 240, 5, ""),
        ("agent.max_setpoint_change_c", "num", "Largest change per block", "°C", 0, 5, 0.5, ""),
        ("agent.checkin_times", "times", "Wake Hermes at", "", None, None, None, "e.g. 07:00, 12:30, 16:30 (needs the webhook below)."),
        ("agent.webhook_url", "text", "Hermes webhook URL", "", None, None, None, "Optional. The planner POSTs chat messages and check-ins here."),
    ]},
    {"id": "entities", "title": "Home Assistant entities", "intro": "Which sensors the planner reads. The schedules it writes to can't be changed here.", "fields": [
        (f"entities.{k}", "entity", label, "", None, None, None, "")
        for k, label in [("outdoor", "Outdoor temperature"), ("room", "Room temperature"), ("heat_kwh", "Heating kWh today"),
                         ("cop", "Calculated COP"), ("lwt", "Leaving water temperature"), ("rate", "Octopus current rate"),
                         ("battery_soc", "Battery charge (%)"), ("heating", "Heating running"), ("person", "Presence"),
                         ("climate", "Thermostat (climate)"), ("lwt_offset", "Leaving water offset (climate)"),
                         ("api_budget", "Daikin API calls left"), ("grid_import_kwh", "Grid import (kWh counter)"),
                         ("grid_power", "Grid power (W)"), ("battery_charge_w", "Battery charging power"),
                         ("battery_discharge_w", "Battery discharging power")]
    ]},
]
FIELDS = {f[0]: f for sec in SECTIONS for f in sec["fields"]}


# ------------------------------------------------------------------ reading
def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set(d, path, value):
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = value


def _del(d, path):
    ks = path.split(".")
    for k in ks[:-1]:
        if k not in d:
            return
        d = d[k]
    d.pop(ks[-1], None)


def current(path, cfg=None, over=None, fitted=None):
    """(value in force, default, source) where source is default | custom | fitted | pinned."""
    cfg = cfg or data.load_config()
    over = over if over is not None else data.load_json("config.json", {})
    if path.startswith("params."):
        k = path[7:]
        fitted = fitted if fitted is not None else model.deep_merge(model.DEFAULT_PARAMS, data.load_json("params.json", {}).get("params", {}))
        pin = (over.get("param_overrides") or {}).get(k)
        dflt = model.DEFAULT_PARAMS.get(k)
        if pin is not None:
            return pin, fitted.get(k), "pinned"
        return fitted.get(k), dflt, "fitted" if fitted.get(k) != dflt else "default"
    dflt = _get(model.DEFAULT_CONFIG, path)
    val = _get(cfg, path)
    return val, dflt, "custom" if _get(over, path) is not None and val != dflt else "default"


def get_settings():
    cfg = data.load_config()
    over = data.load_json("config.json", {})
    fitted = model.deep_merge(model.DEFAULT_PARAMS, data.load_json("params.json", {}).get("params", {}))
    out = []
    for sec in SECTIONS:
        fs = []
        for path, kind, label, unit, lo, hi, step, help_ in sec["fields"]:
            val, dflt, src = current(path, cfg, over, fitted)
            fs.append({"path": path, "kind": kind, "label": label, "unit": unit, "min": lo, "max": hi, "step": step,
                       "help": help_, "value": val, "default": dflt, "source": src})
        out.append({"id": sec["id"], "title": sec["title"], "intro": sec["intro"], "fields": fs})
    return {"sections": out, "log": data.load_json(LOG, [])[-30:]}


# ------------------------------------------------------------------ validation
def parse(path, raw):
    """Turn a value from the page into the stored form, or raise ValueError with a plain message."""
    f = FIELDS.get(path)
    if not f:
        raise ValueError("unknown setting %s" % path)
    _, kind, label, unit, lo, hi, step, _ = f
    if kind in ("num", "int"):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError("%s: enter a number" % label)
        if kind == "int":
            v = int(round(v))
        if lo is not None and v < lo or hi is not None and v > hi:
            raise ValueError("%s: must be between %g and %g %s" % (label, lo, hi, unit))
        return v
    if kind == "bool":
        return bool(raw) if not isinstance(raw, str) else raw.lower() in ("1", "true", "yes", "on")
    if kind == "days":
        days = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",") if x.strip()]
        out = sorted({(WD.index(d[:3].title()) if isinstance(d, str) and not d.isdigit() else int(d)) for d in days})
        if any(not 0 <= d <= 6 for d in out):
            raise ValueError("%s: weekdays only" % label)
        return out
    if kind == "hours":
        rng = raw if isinstance(raw, list) else [[float(a) for a in part.split("-")] for part in str(raw).replace(" ", "").split(",") if part]
        out = []
        for r in rng:
            if len(r) != 2 or not (0 <= r[0] < r[1] <= 24):
                raise ValueError("%s: use ranges like 4-7, 13-16 within 0-24" % label)
            out.append([r[0], r[1]])
        return sorted(out)
    if kind == "bounds":
        pts = raw if isinstance(raw, list) and raw and not isinstance(raw[0], list) else None
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            pts = [raw[0][0]] + [b for _, b in raw]
        if pts is None:
            pts = [float(x) for x in str(raw).replace(" ", "").split(",") if x]
        pts = [float(x) for x in pts]
        if len(pts) < 3 or pts[0] != 0 or pts[-1] != 24 or any(b <= a for a, b in zip(pts, pts[1:])):
            raise ValueError("%s: hours from 0 to 24 in increasing order, e.g. 0,4,7,13,16,19,22,24" % label)
        return [[a, b] for a, b in zip(pts, pts[1:])]
    if kind == "times":
        ts = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",") if x.strip()]
        for t in ts:
            try:
                dt.datetime.strptime(t, "%H:%M")
            except ValueError:
                raise ValueError("%s: use HH:MM, e.g. 07:00" % label)
        return ts
    if kind == "bins":
        if isinstance(raw, dict):
            pairs = raw.items()
        else:
            pairs = [p.split(":") for p in str(raw).replace(" ", "").split(",") if p]
        out = {}
        for k, v in pairs:
            v = float(v)
            if not 1 < v < 9:
                raise ValueError("%s: COP values look wrong (%g)" % (label, v))
            out[str(int(float(k)))] = v
        if len(out) < 2:
            raise ValueError("%s: give at least two temperature:COP pairs" % label)
        return out
    if kind == "entity":
        v = str(raw).strip()
        if v and "." not in v:
            raise ValueError("%s: an entity id looks like sensor.something" % label)
        return v
    v = str(raw).strip()
    if path == "agent.webhook_url" and v and not v.startswith(("http://", "https://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    return v


def _check_consistency(cfg):
    c = cfg["comfort"]
    if c["min_setpoint_c"] >= c["max_setpoint_c"]:
        raise ValueError("Lowest setpoint must be below the highest setpoint")
    if c["hard_floor_c"] > c["soft_floor_c"]:
        raise ValueError("Hard floor can't be above the comfort floor")
    if cfg["battery"]["reserve_kwh"] >= cfg["battery"]["capacity_kwh"] and cfg["battery"].get("enabled", True):
        raise ValueError("Battery reserve must be less than its capacity")
    o = cfg["lwt_offset"]
    if o["min"] > o["max"]:
        raise ValueError("Lowest water offset must not be above the highest")


def build(changes, resets=()):
    """Return (new config.json overrides, effective cfg, effective params) without saving."""
    over = copy.deepcopy(data.load_json("config.json", {}))
    for path in resets:
        if path.startswith("params."):
            (over.get("param_overrides") or {}).pop(path[7:], None)
        else:
            _del(over, path)
    for path, raw in (changes or {}).items():
        v = parse(path, raw)
        if path.startswith("params."):
            over.setdefault("param_overrides", {})[path[7:]] = v
        else:
            _set(over, path, v)
    cfg = model.deep_merge(model.DEFAULT_CONFIG, over)
    _check_consistency(cfg)
    params = model.deep_merge(model.deep_merge(model.DEFAULT_PARAMS, data.load_json("params.json", {}).get("params", {})),
                              over.get("param_overrides") or {})
    return over, cfg, params


def save(changes, resets=(), who="you"):
    before = data.load_json("config.json", {})
    over, cfg, _ = build(changes, resets)
    data.save_json("config.json", over)
    entry = {"at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "by": who,
             "changes": {k: v for k, v in (changes or {}).items()}, "reset": list(resets)}
    log = data.load_json(LOG, [])
    log.append(entry)
    data.save_json(LOG, log[-200:])
    blocks_changed = before.get("blocks") != over.get("blocks")
    return {"saved": True, "needs_replan": blocks_changed, "entry": entry}


# ------------------------------------------------------------------ preview
def preview(changes, resets=()):
    """Score the latest plan (and your routine) under the proposed settings, and re-optimise the plan day
    quickly, without saving anything. Uses the forecast stored with the plan."""
    from . import planner
    plan = data.load_json("plan_latest.json", None)
    if not plan or not plan.get("sim"):
        raise ValueError("needs a plan made by the current version (after the next 22:00 run)")
    _, cfg_new, params_new = build(changes, resets)
    cfg_old = data.load_config()
    if cfg_new["blocks"] != cfg_old["blocks"]:
        raise ValueError("the time blocks changed, so the current plan can't be re-scored; save and re-plan instead")

    def run(cfg, params):
        p2 = copy.deepcopy(plan)
        p2["sim"]["params"] = params
        p2["sim"]["terminal_ref_c"] = cfg["comfort"].get("end_of_day_c", 19.5)
        ctx, fixed, _, _, fixed_off = planner.sim_context(cfg, p2)
        offs_on = fixed_off is not None and cfg["lwt_offset"].get("enabled", True)
        fo = fixed_off if offs_on else None

        def ev(v, o):
            return model.evaluate(cfg, params, ctx, fixed, v, offsets=o if offs_on else None, fixed_off=fo)
        st = plan["strategies"]
        cur_v, cur_o = [b["temperature"] for b in plan["blocks_raw"]], planner.plan_offsets(plan)
        rt = st["Current routine"]
        r_plan, r_rout = ev(cur_v, cur_o), ev(rt["values"], rt.get("offsets"))
        lo, hi = cfg["lwt_offset"]["min"], cfg["lwt_offset"]["max"]
        best_v, best_o, _ = model.optimise(cfg, params, ctx, fixed, [cur_v], passes=2, offsets=cur_o if offs_on else None,
                                           fixed_off=fo, tune_offsets=offs_on, offset_cands=model.offset_candidates(cfg, lo, hi),
                                           offset_starts=2)
        r_best = ev(best_v, best_o)
        summ = lambda r: {"electricity": round(r["grid_cost"], 2), "hours_below": round(r["hours_below"], 2),
                          "kwh": round(r["kwh_hp"], 1), "coldest": round(r["t_min_awake"], 1),
                          "score": planner.score_points(r["total"], r_rout["total"])}
        return {"plan": summ(r_plan), "routine": summ(r_rout),
                "replanned": dict(summ(r_best), values=best_v, offsets=best_o,
                                  blocks=planner.plan_blocks(cfg, best_v, best_o if offs_on else None))}
    return {"for_date": plan["for_date"], "before": run(cfg_old, data.load_params()), "after": run(cfg_new, params_new)}
