# Heat Planner

Nightly planner for a Daikin Altherma heat pump on Octopus Cosy with a home battery. Each night it
forecasts tomorrow, simulates candidate heating schedules, and writes the cheapest comfortable one to
the Home Assistant schedule helper `schedule.heat_pump_planner`. Background, hardware and the model
are in [HANDOVER.md](HANDOVER.md); changes since then and deployment steps are in [HANDOVER_BACK.md](HANDOVER_BACK.md).

Python stdlib only. Runs on LXC 122 as `heatplanner-web` (port 8110) plus two timers.

## Dashboard

`http://192.168.1.152:8110/`. The original page is still at `/classic`.

| Section | What it does |
|---|---|
| Status chips | Plan pushed and verified, whether the thermostat is following the plan, Daikin API calls left, live grid power (if a meter is configured) |
| Now | Room now against target and against what the model predicted, the next setpoint change, today's setpoints with a "now" marker, tariff now and next, battery, outside, heating state |
| Plan | Headline in real pounds with comfort kept separate, plus a shared-time chart: room against setpoint and comfort floors, outside temperature, pump kWh per hour, battery, Cosy and Peak shading, hover readout. **Drag the handles** on the setpoint line to try changes |
| Edit | ±0.5 °C and water offset per block with instant re-simulation (`/api/simulate`, no HA calls); push with confirmation; **undo** puts the optimiser's plan back. **Every view opens on today**; a day picker and the Week cards open any later day (pushing one pins the edit, which the 22:00 run the night before keeps). Blocks that have ended are locked |
| Compare | Strategy cards led by electricity in pounds and comfort as "time below 19.5°"; the model score and its breakdown are folded away under "How the planner scores a plan" |
| Week | Six-day preview: electricity per day against routine, comfort, setpoint strip |
| Who's home | Weekly pattern plus a two-week calendar; tap a day to cycle Home, Out and Away |
| Model | Data-maturity banner, fitted parameters, backtest bars, yesterday predicted against measured, and **measured grid import and cost** from your meter |
| History | Recent plans, activity log |

Changes made during the day (by you, by Hermes or by an undo) are drawn on the chart: the setpoint as
first planned (dotted), a marker at each change (**H** = Hermes, **Y** = you) with its reason, measured room
temperature so far today, and a "Changes to this day" list.

**Hermes chat:** the orb in the bottom-right corner opens a chat with Hermes. Hermes's change updates appear
there and on the Now card. See [HERMES_AGENT.md](HERMES_AGENT.md) for the agent API (state, projection from
now, suggest, guarded adjust, messages, webhook).

On phones the sections become five tabs (Now, Plan, Week, Home, More) with a bottom tab bar.
Light and dark themes.

### Install on your phone
The page has an app manifest and icons. On iPhone, Safari > Share > Add to Home Screen opens it
full-screen. Android's install prompt and offline viewing (showing the last plan when the LXC is
unreachable) need the page served over **HTTPS**, because browsers only run service workers in a
secure context. Putting the dashboard behind nginx proxy manager with a certificate (and auth)
turns both on. Nothing else needs changing.

### Grid meter (Anker Smart Meter Gen 2)
Add its entities to `/opt/heatplanner/data/config.json`:

```json
{"entities": {"grid_import_kwh": "sensor.<your meter's grid import energy>",
              "grid_power": "sensor.<your meter's grid power>"}}
```

`grid_import_kwh` can be a daily-resetting or a lifetime kWh counter. It is collected into the
30-minute store at each data run, and the Model tab then shows each day's measured import split
into Cosy, Standard and Peak, with its cost at your rates. `grid_power` (W, positive = importing)
appears as a live chip.

### "Model score" vs money
A strategy's total is grid cost plus priced comfort shortfall plus end-of-window heat and battery
value. A plan can therefore "win" while costing more in electricity, because it stops the room
dropping below the comfort floor. The dashboard shows both: the headline names real pounds, and
the cards show electricity against routine separately.

## Leaving water offset

With weather-dependent control the Daikin holds the water at `curve(outside) + offset`. The planner
now picks an offset per block as well as a setpoint:

* **Lower offset:** cooler radiators, better COP, but less heat output, so the room warms more slowly.
* **Higher offset:** faster heating, worse COP.
* The optimiser tunes setpoints first, then setpoints and offsets together. Each offset change is
  priced (`write_cost_gbp`, default 1p) so it doesn't flip for nothing.
* The range is `lwt_offset.min/max` in config (default −5 to +5), clipped to the entity's own limits.
* Offsets are written into the helper as block data (`{temperature, lwt_offset}`).
  `deploy/ha/planner_offset_to_daikin.yaml` applies them to `climate.altherma_leaving_water_offset`,
  with the same Daikin-budget guards as the setpoint automation.
* Set `"lwt_offset": {"enabled": false}` in `config.json` to plan setpoints only.

Model terms, all refitted daily from your LWT history once there's enough of it:
`lwc_a`/`lwc_b` (the weather curve, starting at LWT 36.4 − 0.55 × outside) and `k_emit_kw_per_k`
(radiator output per K of mean water over room). The default of 1.0 reproduces your observed
04:00–15:00 warm-up; 0.4 could not. HA-measured COP is scaled by Daikin's published LWT
sensitivity, so offsets move both halves of the COP blend.

Also changed: the end-of-window "heat left in the house" term now only charges for ending colder
than 19.5 °C. It gives no credit for ending warmer, which had made overheating at 22:00–24:00 look
profitable.

## Battery and other entities

* The battery is modelled at 2.5 kW charge and discharge (it is managed in the Anker app, not HA).
  Limits are separate settings (`max_charge_kw`, `max_discharge_kw`); `battery.discharge_limit_entity`
  can point at a live limit if that ever changes.
* Stored every data run for later learning: battery state of charge, charging and discharging power
  (`sensor.anker_solix_battery_charging_power` / `_discharging_power`).
* Grid meter defaults: `sensor.octopus_energy_electricity_…_current_total_consumption` (Home Mini,
  lifetime kWh) and `sensor.anker_solix_grid_import_power`.
* `other_rooms` (hallway and landing motion-sensor temperatures) show on the Now screen.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/plan`, `/api/params`, `/api/plans`, `/api/config`, `/api/log` | stored JSON |
| GET | `/api/status` | live HA readings (REST reads only; no Daikin cloud calls) |
| GET | `/api/actuals?date=YYYY-MM-DD` | measured hourly room / kWh beside that day's prediction (default yesterday) |
| POST | `/api/simulate` `{"date", "values":[7 setpoints], "offsets":[7]}` | what-if for any editable day, read-only |
| POST | `/api/push` `{"date", "values", "offsets"}` | writes that day's weekday in `schedule.heat_pump_planner` only (week-ahead days are also pinned in `data/day_overrides.json`) |
| GET/POST | `/api/chat` | dashboard chat with Hermes |
| GET | `/api/revisions?date=` | changes made to a day (who, when, why, before/after) |
| * | `/api/agent/*` | Hermes's API, keyed with `HEATPLANNER_AGENT_KEY`: see HERMES_AGENT.md |
| GET | `/api/day?date=` | plan-shaped view of any editable day (the plan day, the rest of today, a week-ahead day) |
| POST | `/api/restore` `{"date"}` | undo a hand edit: re-push the planner's choice (for a week-ahead day, drop the pin) |
| POST | `/api/occupancy`, `/api/rerun` | as before |

Optional: set `HEATPLANNER_WRITE_KEY=...` in `/etc/heatplanner.env` to require a key for the POST
actions that change things (`push`, `restore`, `occupancy`, `rerun`). The browser asks for it once and remembers it.
The simplest real protection is still putting the dashboard behind nginx proxy manager with auth.

## Home Assistant side

* `deploy/ha/planner_to_thermostat.yaml`: the automation linking the planner helper to
  `climate.altherma_room_temperature`. It's rate limited for the Daikin API budget. Paste it in
  yourself; the planner never installs it.
* `deploy/ha/dashboard_card.yaml`: an optional Lovelace card. After each run the planner sets
  `sensor.heatplanner_status|saving|cost|kwh|min_temp` (state `error` if a run fails, handy for an
  alert). Turn this off with `"publish_sensors": false` in `config.json`.

## Develop without HA

```
python3 dev/make_demo_data.py demo-data
HEATPLANNER_DATA=demo-data HEATPLANNER_PORT=8111 python3 -m heatplanner.server
python3 -m unittest discover -s tests
```

## Deploy to LXC 122

Same as before (see HANDOVER section 9): copy `heatplanner/` (now including `heatplanner/static/`)
to `/opt/heatplanner/`, `chown -R heatplanner:heatplanner /opt/heatplanner`, remove `__pycache__`,
then `sudo systemctl restart heatplanner-web`. What-if editing is exact for plans made by the new
code; older plans are simulated approximately (no solar term) until the next 22:00 run.
