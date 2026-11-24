"""Thermal + heat pump + tariff + battery model, and the strategy optimiser.

A *strategy* is a list of (start_hour, end_hour, room_setpoint_C) blocks covering one
calendar day.  The heat pump is modelled as a room-thermostat: while the room is below
the block setpoint it heats (up to its capacity), otherwise it is off.
"""
import math

DT = 0.25  # simulation step (hours)

DEFAULT_PARAMS = {
    "ua_kw_per_k": 0.283,        # whole-house heat loss, kW per K (indoor - outdoor)
    "gains_kw": 1.36,            # average free gains (solar, people, appliances), kW
    "c_kwh_per_k": 9.8,          # effective thermal mass seen by the room sensor, kWh per K
    "cop_measured": {"-4": 3.5, "-2": 3.85, "0": 4.36, "2": 4.6, "4": 4.69, "6": 4.62, "8": 4.67, "10": 4.7, "12": 4.7},
    "cop_blend_published": 0.5,  # 0.5 = average of Daikin published model and HA-measured COP
    "lwt_base": 28.0,            # leaving water temp = base + slope * heat output (kW)
    "lwt_slope": 1.7,
    "gains_solar": 2.0,          # extra gains per kW/m2 of solar radiation above rad_ref (window area proxy)
    "rad_ref_kw_m2": 0.04,       # mean radiation already included in gains_kw
    "outdoor_bias_c": 0.0,       # added to forecast to match the heat pump outdoor sensor
    "qmax_a7_kw": 10.6,          # published capacity at A7/W35
    "qmax_slope_kw_per_k": 0.17, # capacity loss per K below 7 C (assumption)
    # Weather compensation (used when the leaving-water offset is planned). With weather-dependent
    # control the pump holds LWT = curve(outdoor) + offset while it runs.
    "lwc_a": 36.4,               # weather curve LWT at 0 C outdoor (assumption until fitted from LWT history)
    "lwc_b": -0.55,              # curve slope, K of LWT per K outdoor (assumption until fitted)
    "k_emit_kw_per_k": 1.0,      # radiator output per K of (mean water - room); 1.0 reproduces the observed 04:00-15:00 warm-up (refit daily)
}

DEFAULT_CONFIG = {
    "schedule_entity": "schedule.heat_pump_planner",
    "schedule_name": "Heat Pump Planner",
    "live_schedule_id": "heat_pump_schedule_2",   # read-only reference of the schedule you run today
    "entities": {
        "outdoor": "sensor.heat_pump_climate_control_outdoor_temperature",
        "room": "sensor.heat_pump_climate_control_room_temperature",
        "heat_kwh": "sensor.heat_pump_climate_control_heating_daily_electrical_consumption",
        "water_kwh": "sensor.heat_pump_water_tank_heating_daily_electrical_consumption",
        "cop": "sensor.daikin_calculated_cop",
        "lwt": "sensor.heat_pump_climate_control_leaving_water_temperature",
        "rate": "sensor.octopus_energy_electricity_20e5320146_1610023144959_current_rate",
        "target": "sensor.heat_pump_scheduled_target",
        "battery_soc": "sensor.marstek_m1_battery_state_of_charge",
        "heating": "binary_sensor.heat_pump_heating",
        "person": "person.arthurs_lane",
        # Optional grid meter (e.g. Anker Smart Meter Gen 2). Set in config.json -> entities. Leave "" if none.
        "grid_import_kwh": "sensor.octopus_energy_electricity_20e5320146_1610023144959_current_total_consumption",  # Home Mini, lifetime kWh
        "grid_power": "sensor.anker_solix_grid_import_power",   # live grid import in W, shown on the dashboard
        "battery_charge_w": "sensor.anker_solix_battery_charging_power",       # stored for learning real battery behaviour
        "battery_discharge_w": "sensor.anker_solix_battery_discharging_power",
        "climate": "climate.altherma_room_temperature",              # read-only: used to check the pump follows the plan
        "lwt_offset": "climate.altherma_leaving_water_offset",      # its target temperature is the offset (K)
        "api_budget": "sensor.altherma_gateway_ratelimit_remaining_day",
    },
    "tariff": {
        "cosy": [[4, 7], [13, 16], [22, 24]],
        "peak": [[16, 19]],
        "rate_cosy": 0.140651, "rate_standard": 0.286808, "rate_peak": 0.430212,
    },
    # Anker Solix, managed in the Anker app (not HA): 2.5 kW each way. discharge_limit_entity (W) can be set
    # to re-read a live limit at plan time; left empty because the HA battery helpers are not in use.
    "battery": {"capacity_kwh": 10.0, "reserve_kwh": 1.0, "max_kw": 2.5, "max_charge_kw": 2.5, "max_discharge_kw": 2.5,
                "rte": 0.90, "discharge_limit_entity": ""},
    "house_load_kw": 0.25,
    "comfort": {
        "target_c": 20.0,          # you said 20 feels comfortable
        "soft_floor_c": 19.5,      # soft penalty below this while occupied and awake
        "hard_floor_c": 19.0,      # heavy penalty below this while occupied and awake
        "sleep_floor_c": 17.5,
        "away_floor_c": 16.0,
        "soft_penalty_gbp_per_kh": 0.15,
        "hard_penalty_gbp_per_kh": 0.60,
        "max_setpoint_c": 22.0,
        "min_setpoint_c": 16.0,
    },
    "occupancy": {
        "away_weekdays": [0, 1],         # default weekly pattern: out in the daytime Mon, Tue (0 = Monday)
        "away_from": 8.0, "away_to": 16.0,
        "overrides": {},                 # {"YYYY-MM-DD": "home" | "away_day" | "away_all"} beats the weekly pattern
        "sleep_from": 23.0, "sleep_to": 6.5,
    },
    # extra room temperatures shown on the dashboard (the thermostat room runs much warmer than the rest)
    "other_rooms": {"Hallway": "sensor.hallway_motion_hallway_temperature", "Landing": "sensor.landing_motion_upstairs_temperature"},
    "fire": {"weekdays": [5, 6], "from": 18.0, "to": 23.0, "extra_gain_kw": 1.0},
    "optimiser": {"candidates": [x / 2 for x in range(32, 45)]},  # 16.0 .. 22.0 step 0.5
    "blocks": [[0, 4], [4, 7], [7, 13], [13, 16], [16, 19], [19, 22], [22, 24]],
    "push_hour": 22,
    # Plan the leaving-water offset alongside the room setpoint. The planner only writes it into the helper
    # (block data "lwt_offset"); an HA automation applies it. Range is also clipped to the entity's own min/max.
    "lwt_offset": {"enabled": True, "min": -5, "max": 5, "step": 1, "write_cost_gbp": 0.01},
    "publish_sensors": True,   # write sensor.heatplanner_* states into HA after each plan (read-only for HA, safe to disable)
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------------ heat pump
def qmax(params, to):
    return max(3.0, params["qmax_a7_kw"] - params["qmax_slope_kw_per_k"] * max(0.0, 7.0 - to))


def cop_published(to, lwt):
    base = 4.83 - 0.117 * (lwt - 35.0)             # Daikin: 4.83 @W35, 3.66 @W45 (A7)
    f = 1 - 0.027 * (7.0 - to) if to < 7 else min(1.1, 1 + 0.01 * (to - 7))
    return max(1.5, base * f)


def cop_measured(params, to):
    pts = sorted((float(k), v) for k, v in params["cop_measured"].items())
    # bins are the lower edge of 2 C bins -> centre is +1
    xs = [x + 1 for x, _ in pts]
    ys = [v for _, v in pts]
    if to <= xs[0]:
        return max(1.5, ys[0] - 0.12 * (xs[0] - to))
    if to >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if to <= xs[i]:
            w = (to - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + w * (ys[i] - ys[i - 1])
    return ys[-1]


def lwt_curve(params, to):
    return min(55.0, max(25.0, params["lwc_a"] + params["lwc_b"] * to))


def cop_lwt(params, to, lwt):
    """COP at a given leaving water temperature. The HA-measured COP (collected at the normal curve) is
    scaled by Daikin's published LWT sensitivity, so offsets move both halves of the blend."""
    pub = cop_published(to, lwt)
    meas = cop_measured(params, to) * pub / cop_published(to, lwt_curve(params, to))
    b = params["cop_blend_published"]
    return b * pub + (1 - b) * meas


def emitter_max(params, lwt, t_room):
    """Heat the radiators can pass at this LWT (mean water about 2.5 K below LWT)."""
    return max(0.0, params["k_emit_kw_per_k"] * (lwt - 2.5 - t_room))


def cop(params, to, q_kw):
    lwt = params["lwt_base"] + params["lwt_slope"] * q_kw
    b = params["cop_blend_published"]
    return b * cop_published(to, lwt) + (1 - b) * cop_measured(params, to)


# ------------------------------------------------------------------ tariff
def rate_at(cfg, h):
    t = cfg["tariff"]
    hh = h % 24
    for a, b in t["cosy"]:
        if a <= hh < b:
            return t["rate_cosy"], "cosy"
    for a, b in t["peak"]:
        if a <= hh < b:
            return t["rate_peak"], "peak"
    return t["rate_standard"], "standard"


# ------------------------------------------------------------------ comfort
def day_status(cfg, date):
    """'home' | 'away_day' | 'away_all' for a date (override beats weekly pattern)."""
    o = cfg["occupancy"]
    ov = o.get("overrides", {}).get(date.isoformat())
    if ov in ("home", "away_day", "away_all"):
        return ov
    return "away_day" if date.weekday() in o["away_weekdays"] else "home"


def comfort_class(cfg, date, hour):
    o = cfg["occupancy"]
    h = hour % 24
    st = day_status(cfg, date)
    if st == "away_all":
        return "away"
    if h >= o["sleep_from"] or h < o["sleep_to"]:
        return "sleep"
    if st == "away_day" and o["away_from"] <= h < o["away_to"]:
        return "away"
    return "home"


def comfort_penalty(cfg, cls, temp, dt):
    c = cfg["comfort"]
    if cls == "sleep":
        d = max(0.0, c["sleep_floor_c"] - temp)
        return c["hard_penalty_gbp_per_kh"] * d * dt
    if cls == "away":
        d = max(0.0, c["away_floor_c"] - temp)
        return c["hard_penalty_gbp_per_kh"] * d * dt
    soft = max(0.0, c["soft_floor_c"] - temp)
    hard = max(0.0, c["hard_floor_c"] - temp)
    return (c["soft_penalty_gbp_per_kh"] * soft + c["hard_penalty_gbp_per_kh"] * hard) * dt


# ------------------------------------------------------------------ simulation
def target_for(blocks, hour):
    h = hour % 24
    for a, b, t in blocks:
        if a <= h < b:
            return t
    return blocks[-1][2]


def simulate(cfg, params, plan, ctx, use_battery=True, trace=False):
    """Simulate from ctx['start_dt'] for ctx['hours'] hours.

    plan: dict {'fixed': [(a,b,T)] for the start day tail, 'day': [(a,b,T)] for next day}
          optional 'fixed_off' / 'day_off': [(a,b,offset)] -> weather-compensation mode with planned offsets
    ctx: start_dt (datetime, local, naive ok), hours, t_room0, soc0_kwh, outdoor(fn(dt)->C),
         tomorrow (date), terminal_ref_c
    Returns dict with cost, kwh, penalty, min temp, trace.
    """
    import datetime as dt_
    start = ctx["start_dt"]
    steps = int(round(ctx["hours"] / DT))
    t_room = ctx["t_room0"]
    bat = cfg["battery"]
    soc = ctx["soc0_kwh"] if use_battery else 0.0
    soc_start = soc
    eta = math.sqrt(bat["rte"])
    ua, c, g0 = params["ua_kw_per_k"], params["c_kwh_per_k"], params["gains_kw"]
    house = cfg["house_load_kw"]
    fire = cfg["fire"]
    grid_cost = 0.0
    kwh_hp = 0.0
    kwh_by_slot = {"cosy": 0.0, "standard": 0.0, "peak": 0.0}
    pen = 0.0
    cost_slot = {"cosy": 0.0, "standard": 0.0, "peak": 0.0}
    tmin = 99.0
    hours_below = 0.0   # time at home and awake with the room under the soft comfort floor (plain-language comfort)
    soft_floor = cfg["comfort"]["soft_floor_c"]
    rows = []
    cosy_rate = cfg["tariff"]["rate_cosy"]
    wc = plan.get("day_off") is not None
    for i in range(steps):
        now = start + dt_.timedelta(hours=i * DT)
        hour = now.hour + now.minute / 60.0
        # which schedule applies: the start day's fixed tail, then the planned day
        if now.date() == ctx["start_dt"].date():
            tgt = target_for(plan["fixed"], hour)
        elif now.date() == ctx["tomorrow"]:
            tgt = target_for(plan["day"], hour)
        else:  # day after tomorrow: assume tomorrow's late-night setpoint continues until the horizon end
            tgt = plan["day"][-1][2]
        to = ctx["outdoor"](now)
        gains = g0
        if "radiation" in ctx:
            gains = max(0.0, gains + params.get("gains_solar", 0.0) * (ctx["radiation"](now) - params.get("rad_ref_kw_m2", 0.04)))
        if now.weekday() in fire["weekdays"] and fire["from"] <= hour < fire["to"] and day_status(cfg, now.date()) != "away_all":
            gains += fire["extra_gain_kw"]
        q = 0.0
        off = lwt = None
        if wc:
            if now.date() == ctx["start_dt"].date():
                off = target_for(plan["fixed_off"], hour)
            elif now.date() == ctx["tomorrow"]:
                off = target_for(plan["day_off"], hour)
            else:
                off = plan["day_off"][-1][2]
            lwt = min(55.0, max(22.0, lwt_curve(params, to) + off))
        if t_room < tgt:
            need = (tgt - t_room) * c / DT + ua * (t_room - to) - gains
            q = min(max(need, 0.0), qmax(params, to))
            if wc:
                q = min(q, emitter_max(params, lwt, t_room))
        if q > 0:
            el = q / (cop_lwt(params, to, lwt) if wc else cop(params, to, q))
        else:
            el = 0.0
        t_room += DT / c * (q + gains - ua * (t_room - to))
        # ---- electricity & battery
        rate, slot = rate_at(cfg, hour)
        load = house + el
        grid = load
        if use_battery:
            if slot == "cosy":
                room = (bat["capacity_kwh"] - soc)
                chg = min(bat.get("max_charge_kw", bat["max_kw"]) * DT, room / eta)
                soc += chg * eta
                grid = load + chg / DT
            else:
                avail = max(0.0, soc - bat["reserve_kwh"]) * eta
                d = min(load * DT, bat.get("max_discharge_kw", bat["max_kw"]) * DT, avail)
                soc -= d / eta
                grid = (load * DT - d) / DT
        grid_cost += grid * DT * rate
        cost_slot[slot] += grid * DT * rate
        kwh_hp += el * DT
        kwh_by_slot[slot] += el * DT
        cls = comfort_class(cfg, now.date(), hour)
        pen += comfort_penalty(cfg, cls, t_room, DT)
        tmin = min(tmin, t_room) if cls != "sleep" else tmin
        if cls == "home" and t_room < soft_floor - 0.1:
            hours_below += DT
        if trace:
            rows.append({"t": now.strftime("%Y-%m-%dT%H:%M"), "room": round(t_room, 2), "target": tgt,
                         "outdoor": round(to, 1), "q_kw": round(q, 2), "el_kw": round(el, 2),
                         "rate": rate, "soc": round(soc, 2), "cls": cls, "cum": round(grid_cost + pen, 3),
                         "off": off, "lwt": None if lwt is None else round(lwt, 1)})
    # terminal value: leftover battery energy and thermal state, valued at the cosy price
    batt_value = (soc - soc_start) * cosy_rate / eta if use_battery else 0.0
    cop_ref = 4.0
    ref = ctx.get("terminal_ref_c", 19.5)
    # charge for ending the window colder than ref (that heat must be bought back), but give no credit for
    # ending warmer: a fixed-COP credit made "overheat at 22:00-24:00" look profitable once real COP beat it
    terminal = max(0.0, ref - t_room) * c / cop_ref * cosy_rate
    writes = 0.0
    if wc:   # each offset change costs Daikin API calls: price it so the optimiser doesn't flip for nothing
        offs = [plan["fixed_off"][-1][2]] + [b[2] for b in plan["day_off"]]
        writes = sum(1 for a, b in zip(offs, offs[1:]) if a != b) * cfg.get("lwt_offset", {}).get("write_cost_gbp", 0.01)
    total = grid_cost - batt_value + terminal + pen + writes
    out = {"writes": writes, "grid_cost": grid_cost, "batt_value": batt_value, "terminal": terminal, "penalty": pen,
           "total": total, "kwh_hp": kwh_hp, "kwh_by_slot": kwh_by_slot, "t_end": t_room,
           "t_min_awake": tmin, "hours_below": hours_below, "soc_end": soc, "cost_slot": cost_slot}
    if trace:
        out["trace"] = rows
    return out


# ------------------------------------------------------------------ optimiser
def blocks_from_values(cfg, values):
    return [(a, b, v) for (a, b), v in zip(cfg["blocks"], values)]


def evaluate(cfg, params, ctx, fixed, values, use_battery=True, trace=False, offsets=None, fixed_off=None):
    """values: one setpoint per cfg block. offsets (optional): one LWT offset per block; when given, the
    weather-compensation model is used, with fixed_off ([(a,b,off)]) for the start day's tail."""
    plan = {"fixed": fixed, "day": blocks_from_values(cfg, values)}
    if offsets is not None:
        plan["day_off"] = blocks_from_values(cfg, offsets)
        plan["fixed_off"] = fixed_off or [(0, 24, 0)]
    return simulate(cfg, params, plan, ctx, use_battery, trace)


def offset_candidates(cfg, lo=None, hi=None):
    o = cfg.get("lwt_offset", {})
    a, b, st = o.get("min", -5), o.get("max", 5), o.get("step", 1) or 1
    if lo is not None:
        a = max(a, lo)
    if hi is not None:
        b = min(b, hi)
    out, v = [], a
    while v <= b + 1e-9:
        out.append(round(v, 2))
        v += st
    return out


def optimise(cfg, params, ctx, fixed, starts, use_battery=True, passes=4, offsets=None, fixed_off=None, tune_offsets=False,
             offset_cands=None, offset_starts=4):
    """Coordinate descent. starts: list of setpoint lists. offsets: starting offsets (also used as-is when
    tune_offsets is False). Returns (setpoints, offsets_or_None, total).

    With tune_offsets the search does not just start from the offset currently set on the pump (which made it
    "stick" to the last value wherever the offset barely matters): it also starts from several uniform offsets,
    and besides single-block moves it tries moving a whole run of consecutive blocks at once, which costs one
    write instead of two and so isn't blocked by the write price."""
    cands = cfg["optimiser"]["candidates"]
    ocands = offset_cands if offset_cands is not None else offset_candidates(cfg)
    n = len(cfg["blocks"])
    ev = lambda v, o: evaluate(cfg, params, ctx, fixed, v, use_battery, offsets=o, fixed_off=fixed_off)["total"]
    pairs = [(list(st), list(offsets) if offsets is not None else None) for st in starts]
    if tune_offsets and offsets is not None and ocands:
        lo, hi = min(ocands), max(ocands)
        picks = [0, len(ocands) // 4, len(ocands) // 2, (3 * len(ocands)) // 4][:max(0, offset_starts)]
        uni = sorted({ocands[k] for k in picks})
        for st in starts[:1]:
            for u in uni:
                if [u] * n != list(offsets):
                    pairs.append((list(st), [u] * n))
    def descend(vals, offs, best, max_passes):
        for _ in range(max_passes):
            improved = False
            for i in range(n):
                for cand in cands:
                    if cand == vals[i]:
                        continue
                    trial = vals[:i] + [cand] + vals[i + 1:]
                    s_ = ev(trial, offs)
                    if s_ < best - 1e-6:
                        best, vals[i], improved = s_, cand, True
                if tune_offsets and offs is not None:
                    for cand in ocands:
                        if cand == offs[i]:
                            continue
                        trial = offs[:i] + [cand] + offs[i + 1:]
                        s_ = ev(vals, trial)
                        if s_ < best - 1e-6:
                            best, offs[i], improved = s_, cand, True
            if not improved:
                break
        return vals, offs, best

    def runs(vals, offs, best):
        # set blocks i..j to one offset in a single step (one write instead of two)
        improved = False
        for i in range(n):
            for j in range(i + 1, n):
                for cand in ocands:
                    if all(o == cand for o in offs[i:j + 1]):
                        continue
                    trial = offs[:i] + [cand] * (j - i + 1) + offs[j + 1:]
                    s_ = ev(vals, trial)
                    if s_ < best - 1e-6:
                        best, offs, improved = s_, trial, True
        return offs, best, improved

    best_all = None
    for vals, offs in pairs:
        vals, offs, b = descend(vals, offs, ev(vals, offs), passes)
        if tune_offsets and offs is not None:
            for _ in range(2):
                offs, b, improved = runs(vals, offs, b)
                if not improved:
                    break
                vals, offs, b = descend(vals, offs, b, passes)
        if best_all is None or b < best_all[2] - 1e-9:
            best_all = (list(vals), list(offs) if offs is not None else None, b)
    return best_all


def tidy(values, cfg, offsets=None):
    """Merge neighbouring blocks with the same setpoint (and offset) when presenting/pushing.
    Returns (a, b, T) tuples, or (a, b, T, offset) when offsets are given."""
    out = []
    for i, ((a, b), v) in enumerate(zip(cfg["blocks"], values)):
        o = offsets[i] if offsets is not None else None
        if out and out[-1][2] == v and (o is None or out[-1][3] == o):
            out[-1] = (out[-1][0], b) + tuple(out[-1][2:])
        else:
            out.append((a, b, v) if o is None else (a, b, v, o))
    return out
