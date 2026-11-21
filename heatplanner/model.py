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
        "climate": "climate.altherma_room_temperature",              # read-only: used to check the pump follows the plan
        "api_budget": "sensor.altherma_gateway_ratelimit_remaining_day",
    },
    "tariff": {
        "cosy": [[4, 7], [13, 16], [22, 24]],
        "peak": [[16, 19]],
        "rate_cosy": 0.140651, "rate_standard": 0.286808, "rate_peak": 0.430212,
    },
    "battery": {"capacity_kwh": 10.0, "reserve_kwh": 1.0, "max_kw": 2.5, "rte": 0.90},
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
    "fire": {"weekdays": [5, 6], "from": 18.0, "to": 23.0, "extra_gain_kw": 1.0},
    "optimiser": {"candidates": [x / 2 for x in range(32, 45)]},  # 16.0 .. 22.0 step 0.5
    "blocks": [[0, 4], [4, 7], [7, 13], [13, 16], [16, 19], [19, 22], [22, 24]],
    "push_hour": 22,
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
    rows = []
    cosy_rate = cfg["tariff"]["rate_cosy"]
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
        if t_room < tgt:
            need = (tgt - t_room) * c / DT + ua * (t_room - to) - gains
            q = min(max(need, 0.0), qmax(params, to))
        el = q / cop(params, to, q) if q > 0 else 0.0
        t_room += DT / c * (q + gains - ua * (t_room - to))
        # ---- electricity & battery
        rate, slot = rate_at(cfg, hour)
        load = house + el
        grid = load
        if use_battery:
            if slot == "cosy":
                room = (bat["capacity_kwh"] - soc)
                chg = min(bat["max_kw"] * DT, room / eta)
                soc += chg * eta
                grid = load + chg / DT
            else:
                avail = max(0.0, soc - bat["reserve_kwh"]) * eta
                d = min(load * DT, bat["max_kw"] * DT, avail)
                soc -= d / eta
                grid = (load * DT - d) / DT
        grid_cost += grid * DT * rate
        cost_slot[slot] += grid * DT * rate
        kwh_hp += el * DT
        kwh_by_slot[slot] += el * DT
        cls = comfort_class(cfg, now.date(), hour)
        pen += comfort_penalty(cfg, cls, t_room, DT)
        tmin = min(tmin, t_room) if cls != "sleep" else tmin
        if trace:
            rows.append({"t": now.strftime("%Y-%m-%dT%H:%M"), "room": round(t_room, 2), "target": tgt,
                         "outdoor": round(to, 1), "q_kw": round(q, 2), "el_kw": round(el, 2),
                         "rate": rate, "soc": round(soc, 2), "cls": cls, "cum": round(grid_cost + pen, 3)})
    # terminal value: leftover battery energy and thermal state, valued at the cosy price
    batt_value = (soc - soc_start) * cosy_rate / eta if use_battery else 0.0
    cop_ref = 4.0
    ref = ctx.get("terminal_ref_c", 19.5)
    terminal = (ref - t_room) * c / cop_ref * cosy_rate
    total = grid_cost - batt_value + terminal + pen
    out = {"grid_cost": grid_cost, "batt_value": batt_value, "terminal": terminal, "penalty": pen,
           "total": total, "kwh_hp": kwh_hp, "kwh_by_slot": kwh_by_slot, "t_end": t_room,
           "t_min_awake": tmin, "soc_end": soc, "cost_slot": cost_slot}
    if trace:
        out["trace"] = rows
    return out


# ------------------------------------------------------------------ optimiser
def blocks_from_values(cfg, values):
    return [(a, b, v) for (a, b), v in zip(cfg["blocks"], values)]


def evaluate(cfg, params, ctx, fixed, values, use_battery=True, trace=False):
    plan = {"fixed": fixed, "day": blocks_from_values(cfg, values)}
    return simulate(cfg, params, plan, ctx, use_battery, trace)


def optimise(cfg, params, ctx, fixed, starts, use_battery=True, passes=4):
    cands = cfg["optimiser"]["candidates"]
    best_all = None
    for start in starts:
        vals = list(start)
        best = evaluate(cfg, params, ctx, fixed, vals, use_battery)["total"]
        for _ in range(passes):
            improved = False
            for i in range(len(vals)):
                cur = vals[i]
                for cand in cands:
                    if cand == cur:
                        continue
                    trial = vals[:i] + [cand] + vals[i + 1:]
                    s = evaluate(cfg, params, ctx, fixed, trial, use_battery)["total"]
                    if s < best - 1e-6:
                        best, vals[i], improved = s, cand, True
            if not improved:
                break
        if best_all is None or best < best_all[0]:
            best_all = (best, list(vals))
    return best_all[1], best_all[0]


def tidy(values, cfg):
    """Merge neighbouring blocks with the same setpoint when presenting/pushing."""
    out = []
    for (a, b), v in zip(cfg["blocks"], values):
        if out and out[-1][2] == v:
            out[-1] = (out[-1][0], b, v)
        else:
            out.append((a, b, v))
    return out
