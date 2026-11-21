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
| Status chips | Plan pushed and verified, whether the thermostat is following the plan, Daikin API calls left, room, outside, battery and current tariff (live from HA, cached 60 s) |
| Plan | Headline with real electricity cost kept separate from comfort, plus a shared-time chart: room temperature against setpoint and comfort floors, outside temperature, pump kWh per hour, battery, with Cosy and Peak shading and a hover readout |
| Edit | Change any block by 0.5 °C and see cost and comfort recalculated straight away (`/api/simulate`, no HA calls). Push your version to the helper with a confirmation step |
| Compare | Strategy cards (model score, electricity against routine, comfort, kWh, room-temperature sparkline, a plain reason for each), and where the saving comes from |
| Week | Six-day preview with setpoint strips |
| Who's home | Weekly pattern plus per-day overrides; saving re-plans and re-pushes |
| Model | Data-maturity banner, fitted parameters, backtest bars (model against measured), yesterday's predicted and measured room temperature |
| History | Recent plans, model-estimated savings, activity log |

Light and dark themes; laid out for phones first.

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
| POST | `/api/occupancy`, `/api/rerun` | as before |

Optional: set `HEATPLANNER_WRITE_KEY=...` in `/etc/heatplanner.env` to require a key for the POST
actions that change things (`push`, `occupancy`, `rerun`). The browser asks for it once and remembers it.
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
