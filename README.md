# Heat Planner

Nightly planner for a Daikin Altherma heat pump on Octopus Cosy with a home battery. Each night it
forecasts tomorrow, simulates candidate heating schedules, and writes the cheapest comfortable one to
the Home Assistant schedule helper `schedule.heat_pump_planner`. Background, hardware and the model
are in [HANDOVER.md](HANDOVER.md).

Python stdlib only. Runs on LXC 122 as `heatplanner-web` (port 8110) plus two timers.

## Dashboard

`http://192.168.1.152:8110/`. The original page is still at `/classic`.

| Section | What it does |
|---|---|
| Status chips | Plan pushed and verified, whether the thermostat is following the plan, Daikin API calls left, live grid power (if a meter is configured) |
| Now | Room now against target and against what the model predicted, the next setpoint change, today's setpoints with a "now" marker, tariff now and next, battery, outside, heating state |
| Plan | Headline in real pounds with comfort kept separate, plus a shared-time chart: room against setpoint and comfort floors, outside temperature, pump kWh per hour, battery, Cosy and Peak shading, hover readout. **Drag the handles** on the setpoint line to try changes |
| Edit | ±0.5 °C per block with instant re-simulation (`/api/simulate`, no HA calls); push with confirmation; **undo** puts the optimiser's plan back |
| Compare | Strategy cards led by electricity in pounds and comfort as "time below 19.5°"; the model score and its breakdown are folded away under "How the planner scores a plan" |
| Week | Six-day preview: electricity per day against routine, comfort, setpoint strip |
| Who's home | Weekly pattern plus a two-week calendar; tap a day to cycle Home, Out and Away |
| Model | Data-maturity banner, fitted parameters, backtest bars, yesterday predicted against measured, and **measured grid import and cost** from your meter |
| History | Recent plans, activity log |

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

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/plan`, `/api/params`, `/api/plans`, `/api/config`, `/api/log` | stored JSON |
| GET | `/api/status` | live HA readings (REST reads only; no Daikin cloud calls) |
| GET | `/api/actuals?date=YYYY-MM-DD` | measured hourly room / kWh beside that day's prediction (default yesterday) |
| POST | `/api/simulate` `{"values":[7 setpoints]}` | what-if, read-only |
| POST | `/api/push` `{"values":[...]}` | writes the plan's weekday in `schedule.heat_pump_planner` only |
| POST | `/api/restore` | undo a hand edit: re-push the strategy the planner had chosen |
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
