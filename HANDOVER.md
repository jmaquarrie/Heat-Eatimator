# Heat pump planner: handover for Claude

Written 2026-11-21. Owner: "ShogunCynic", UK. Read this whole file before acting.

## 0. Ground rules from the owner
- **Never stop, restart or reconfigure existing services, containers or VMs without explicit permission.** Starting/restarting only the planner's own `heatplanner-*` units is fine.
- Never overwrite the live heating schedule `schedule.heat_pump_schedule_2`. The planner writes only to its own helper `schedule.heat_pump_planner`.
- Never put secrets (HA token, passwords) in chat, files you hand back, or wikis. The HA token lives only in `/etc/heatplanner.env` on the planner LXC.
- UK user: prefers local/area-based info. Be direct, show the sums, say plainly when something is an assumption.

## 1. What this is
A web tool plus nightly job that models the owner's house, Daikin heat pump, Octopus Cosy tariff and home battery, then:
1. every day at 22:00 forecasts tomorrow's weather, simulates candidate heating strategies and picks the cheapest that keeps the house comfortable,
2. pushes the chosen setpoints into the Home Assistant schedule helper `schedule.heat_pump_planner` for tomorrow's weekday (read back to verify),
3. re-fits the model from fresh data every day at 03:30,
4. shows everything on a dashboard, where the owner can also set who is home on which days and re-run the model.

The owner has not yet connected `schedule.heat_pump_planner` to the heat pump. They will do that themselves with an automation (see section 8).

## 2. Home Assistant (shared by every part of this system)
- Instance: VM 100 `homeassistant` on Proxmox node `maplebox`, **LAN IP 192.168.1.180 on port 80** (not 8123). Time zone Europe/London. Home location comes from `/api/config`.
- Auth: long-lived bearer token. Stored for the planner in `/etc/heatplanner.env` (`HASS_URL`, `HASS_TOKEN`) on LXC 122, root:heatplanner 0640. **It is deliberately not in this file.** Ask the owner to supply one if you work outside the LXC.
- Direct access from a laptop/browser to the LAN IP may fail from some networks. From the LXC it works.
- REST works (`/api/states`, `/api/history/period`, `/api/config`). Websocket works (`/api/websocket`), and it is required for schedule helpers: `schedule/list`, `schedule/create`, `schedule/update`. There is no REST service to edit schedule blocks.
- `weather.get_forecasts` returns 400 in this install; the planner uses Open-Meteo instead.
- `calendar.get_events` also returns 400.

### Entities used (configurable in `model.py` DEFAULT_CONFIG["entities"])
| Role | Entity |
|---|---|
| Outdoor temp (heat pump) | `sensor.heat_pump_climate_control_outdoor_temperature` |
| Room temp (thermostat sensor) | `sensor.heat_pump_climate_control_room_temperature` |
| Heating kWh today (counter, resets midnight, updates ~every 2 h, 1 kWh resolution) | `sensor.heat_pump_climate_control_heating_daily_electrical_consumption` |
| Hot water kWh today | `sensor.heat_pump_water_tank_heating_daily_electrical_consumption` |
| Daikin calculated COP | `sensor.daikin_calculated_cop` |
| Leaving water temp | `sensor.heat_pump_climate_control_leaving_water_temperature` |
| Heating running (on/off) | `binary_sensor.heat_pump_heating` |
| Octopus current rate | `sensor.octopus_energy_electricity_20e5320146_1610023144959_current_rate` |
| Scheduled target (from live schedule) | `sensor.heat_pump_scheduled_target` |
| Battery SoC (%) | `sensor.marstek_m1_battery_state_of_charge` (Anker battery; entity name is Marstek-style) |
| Presence | `person.arthurs_lane` |
| Climate entities | `climate.altherma_room_temperature` (setpoint), `climate.altherma_leaving_water_offset` |
| Daikin API budget | `sensor.altherma_gateway_ratelimit_remaining_day` |
| Live schedule (read-only reference) | `schedule.heat_pump_schedule_2` |
| Planner output helper | `schedule.heat_pump_planner` (created by the planner, id `heat_pump_planner`) |

Daikin cloud limit is about 200 calls a day. HA's own polling already uses about 165 of them (observed ~7 calls/hour), so only about 35 calls a day are spare. Every setpoint write costs calls.

### Live schedule shape (the owner's current routine, per day)
`00-04: 18, 04-07: 20, 07-13: 18, 13-16: 20.5, 16-22: 19, 22-24: 18` (block `data: {temperature: X}`). The room thermostat setpoint is what these values are.

## 3. The hardware and tariff
- Heat pump: **Daikin Altherma 3 M monobloc EDLA11DA3V3**, 11 kW, heating only, R32. Published: COP 4.83 at A7/W35 (10.6 kW out, 2.18 kW in); COP 3.66 at A7/W45 (9.82 kW out, 2.68 kW in); SCOP 4.64 at 35 °C, 3.32 at 55 °C. Leaving water temperature varies with weather.
- Tariff: **Octopus Cosy**. 04:00-07:00, 13:00-16:00, 22:00-00:00 cosy at about 14.07p/kWh; 16:00-19:00 peak at about 43.02p; everything else about 28.68p. The planner auto-detects these from the rate sensor history.
- Battery: Anker Solix 10 kWh, max 2.5 kW charge and 2.5 kW discharge. The house also draws from it (average 250 W house load, owner's figure). Assumed round-trip efficiency 90% (assumption).
- House: owner's comfort level 20 °C. Home all day except Mon-Tue daytime (assumed 08:00-16:00, unconfirmed). Log fire used only on weekend evenings after 18:00 (modelled +1 kW, unconfirmed).
- Observed: room sensor swings roughly 18.4 °C (04:00 trough) to 21.2 °C (15:00 peak); hallway and landing sensors read about 14 °C, so the thermostat room is much warmer than the rest of the house.

## 4. Where everything runs
- **LXC 122 `debian`, 192.168.1.152**, Debian 13, Python 3.13, TZ Europe/London. Proxmox host 192.168.1.130 (node `maplebox`); owner's other LXCs on the same LAN.
- Dashboard: **http://192.168.1.152:8110** (no authentication, LAN only). Read-only except the "Who is home?" form.
- Code: `/opt/heatplanner/heatplanner/` (stdlib only, no pip dependencies). Data and config: `/opt/heatplanner/data/`. Service user `heatplanner`.
- systemd units (all created by this project):
  - `heatplanner-web.service` runs `python3 -m heatplanner.server` on port 8110.
  - `heatplanner-nightly.timer/.service` runs 22:00 daily: `heatplanner.cli nightly` (collect 3 days, forecast, optimise, push).
  - `heatplanner-learn.timer/.service` runs 03:30 daily: `collect 28` then `learn`.
- Other apps on the same LXC (do not touch): `ha-compare` (8099), `prep-box` (8080), `emergency-prep`.
- Access: `ssh -i ~/.ssh/proxmox-hermes hermes@192.168.1.152` (user `hermes`, sudo NOPASSWD) from the Hermes agent host (LXC 118, 192.168.1.148). A human can use the Proxmox console or `pct exec 122 -- bash`.

## 5. Code map (all under `heatplanner/`)
- `ha.py`: minimal HA client. REST plus a hand-written stdlib websocket client. Schedule helpers: `schedule_list/create/update`.
- `model.py`: defaults (`DEFAULT_PARAMS`, `DEFAULT_CONFIG`), heat pump COP/capacity, tariff slots, comfort and occupancy (`day_status`, `comfort_class`), `simulate()`, `optimise()` (coordinate descent over setpoints 16-22 °C in 0.5 steps), `tidy()`.
- `data.py`: observation store (`obs.json` of 30-min rows), `collect()` from HA history (heating runtime weights the kWh counter, presence fraction, Open-Meteo past weather), `fetch_weather()`, `series_fn()`.
- `learn.py`: daily refit of heat loss, free gains, solar gain, thermal mass, COP bins, outdoor bias; backtest; presence hint; writes `params.json`.
- `planner.py`: `build_plan()`, strategy comparison, `build_outlook()` (6 more days), `validate()`, `push()`, helper creation/seeding.
- `cli.py`: `collect [days] | learn | plan [--push] | nightly | seed | status`.
- `server.py`: dashboard + API (`/api/plan`, `/api/params`, `/api/plans`, `/api/rerun-status`, POST `/api/occupancy`, POST `/api/rerun`).
- Data files in `/opt/heatplanner/data/`: `config.json` (owner overrides), `params.json`, `obs.json`, `plan_latest.json`, `plan_YYYY-MM-DD.json`, `plans_index.json`, `log.jsonl`.

## 6. The model in brief
- One-node room model: `C dT/dt = Q_hp + gains - UA (T_room - T_out)`, 15-minute steps. Gains = base + solar term (forecast shortwave radiation) + log fire on weekends.
- Heat pump acts like a room thermostat: heats when room < block setpoint, up to capacity `10.6 - 0.17*(7 - T_out)` kW (the slope is an assumption).
- COP = 50/50 blend of Daikin's published model and the HA-measured COP by 2 °C outdoor bin (the owner explicitly asked for this average). Published model: `(4.83 - 0.117*(LWT-35)) * f(T_out)` with a 2.7% per °C drop below 7 °C (assumption).
- Cost: grid import at the slot rate, battery charges 2.5 kW in cosy windows, discharges to cover house + pump in other slots, reserve 1 kWh, leftover battery energy valued at the cosy rate.
- Comfort: priced shortfall. Soft floor 19.5 °C, hard floor 19.0 °C while awake and home (0.15 and 0.60 £/K·h), sleep floor 17.5 °C, away floor 16 °C. Terminal value of heat in the house valued at cosy rate and COP 4.
- Strategies compared nightly: Current routine (read from the live schedule), Steady comfort temp, Cosy-windows-only, Optimised. The planner only deviates from the current routine if it saves at least 2p.
- Horizon: from tonight 22:00 to 04:00 the day after tomorrow (about 30 h). Blocks pushed: 00-04, 04-07, 07-13, 13-16, 16-19, 19-22, 22-24 (neighbouring equal values merged).
- Occupancy: weekly pattern (default Mon/Tue daytime away) plus per-date overrides `home | away_day | away_all`, saved in `config.json`.

## 7. Current state (2026-11-21)
- Fitted parameters: UA 0.283 kW/K, base gains 1.42 kW, solar factor 2.0 (default, not yet fitted), thermal mass 9.8 kWh/K (default; the fit was rejected, c=23, R2 0), outdoor bias +0.58 °C. Only **9 complete days** of data exist (HA history starts about 11 Nov), heat-loss fit R2 0.39. Daily backtest error about 3 kWh/day against roughly 15 kWh actual. Treat every number as indicative.
- Earlier estimate of heating electricity per day vs outdoor mean (average of HA-COP and published-COP calcs): -4 °C ~40 kWh, -2 ~33, 0 ~26, 2 ~22, 4 ~17.5, 6 ~14, 8 ~11, 10 ~7.5, 12 ~4.5. This was used to seed the model.
- First plan (Sun 22 Nov) pushed and verified: 00-04 17.5, 04-07 20, 07-13 19.5, 13-22 20, 22-24 17.5 (changes with each run).
- Tested end to end: override "away all day" -> replan -> push -> revert.

## 8. What the owner still has to do / likely next steps
1. Connect `schedule.heat_pump_planner` to the heat pump with an HA automation that triggers on the schedule's state/attribute change, reads `state_attr('schedule.heat_pump_planner','temperature')`, and calls `climate.set_temperature` on `climate.altherma_room_temperature`. Guard it: skip if the new value is within 0.5 °C of current, at most one write per hour, only at block boundaries (5-7 writes a day) because of the Daikin API budget. Note that a block with no `data` (off) must be handled.
2. Confirm assumptions: away hours (08:00-16:00), who is home, log-fire gain (1 kW), battery efficiency (90%), house load (250 W), comfort floors.
3. Possible improvements, roughly in order of value:
   - A power meter on the heat pump (CT clamp / smart plug) to replace the 1 kWh-resolution counter.
   - Temperature sensors in more rooms, so comfort reflects where people actually are.
   - Fix HA solar forecast sensors (they show unavailable) and add PV into the battery model if panels exist.
   - Optional 12:30 mid-day check that re-plans only if the room is more than 1 K off prediction or the forecast moved more than 3 °C.
   - Fit wind/humidity effects (wind and humidity are already stored in `obs.json`) and defrost penalties.
   - Authentication for the dashboard, or put it behind the existing nginx proxy manager (192.168.1.142).
4. After 3-4 weeks of data, re-check the backtest table on the dashboard and compare predicted vs actual kWh and room temperature before trusting the savings numbers.

## 9. How to work on it
- Dev copy (Hermes host): `~/heatplanner` with `devrun.sh` (uses `data-dev/`; loads HA creds from the Hermes `.env`). Deploy: `tar czf` the `heatplanner/` folder, `scp` to the LXC, extract into `/opt/heatplanner`, `chown -R heatplanner:heatplanner /opt/heatplanner`, remove `__pycache__`; timers pick up new code on their next run; `sudo systemctl restart heatplanner-web` only for dashboard changes.
- Run a command on the LXC: `sudo -u heatplanner bash -c 'set -a; . /etc/heatplanner.env; set +a; cd /opt/heatplanner && python3 -m heatplanner.cli plan'` (add `--push` to write to the helper, use `nightly` for the full job).
- Check logs: `/opt/heatplanner/data/log.jsonl`, `journalctl -u heatplanner-nightly`, dashboard "Log" section.
- Schedule helper edits must use the websocket API and send the full week (all seven day arrays) on `schedule/update`.
- Pitfalls hit so far: HA is on port 80; heating counter is sparse (spread by `binary_sensor.heat_pump_heating` runtime); the rate-sensor history contains transition averages, so the tariff is detected by taking the most common value in each rate band; each daily fit must be computed from all data against the prior, not from yesterday's fit.

## 10. Maintenance flags (for the user, not secrets)
- The HA token on the LXC is a copy of the one the Hermes agent uses. Consider creating a dedicated long-lived token for the planner in HA (Profile -> Security) and swapping it into `/etc/heatplanner.env`.

