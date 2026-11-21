# Heat pump planner: handover back to Hermes (v2)

Written 2026-11-21. Read this together with `HANDOVER.md`, which is the original handover and is still
true for anything not mentioned here: hosts, entities, ground rules and how the model works. This file
covers what changed since then and how to deploy it to LXC 122 safely.

## 0. Ground rules (unchanged, plus three)
- Never stop, restart or reconfigure existing services, containers or VMs without the owner's explicit
  permission. Restarting **only** `heatplanner-web` is fine. The timers pick up new code on their next run.
- Never write to `schedule.heat_pump_schedule_2`. The planner writes only `schedule.heat_pump_planner`.
- No secrets in files, chat or git. The HA token stays in `/etc/heatplanner.env` on LXC 122 only. The repo
  contains no secrets; keep it that way, and never commit `data/` or `.env`.
- **New:** do not create or enable Home Assistant automations yourself. The two YAML files in `deploy/ha/` are
  for the owner to review and paste in. Keep `automation.heat_pump_schedule_temperature` off once they are
  live, so two automations never fight over the thermostat.
- **New:** hot water is out of scope (owner's decision). Don't add DHW planning.
- **New:** the battery (Anker Solix, 10 kWh, 2.5 kW each way) is managed in the **Anker app**, not HA. Ignore
  the Marstek/HA battery helpers (`input_number.*`, `number.marstek_*`, `input_select.house_battery_*`).
  They are not authoritative.

## 1. Where the code is now
- GitHub: **https://github.com/jmaquarrie/Heat-Eatimator**, branch **`claude/inspiring-mendel-5cg8xp`**
  (not merged to `main` yet; merge it, or open a PR, if the owner asks).
- Commit `5d97f9a` is the exact code that was running on LXC 122 at the first handover. Everything after it
  is new work:

| Commit | What |
|---|---|
| `7afda9a` | New dashboard (single page), what-if API, live status, predicted-vs-measured, HA sensors, demo data and tests |
| `59a5f69` | `.gitignore`, tidy-ups |
| `266593b` | Phone-first layout: Now screen, tab bar, drag-to-edit on the chart, undo, two-week Who's-home calendar, grid meter, app manifest and service worker |
| `ccd566e` | **Leaving-water offset planning**, end-of-window fix, battery history collection, more entities |
| `ca7443c` | Battery back to 2.5 kW both ways (stop reading the HA discharge number) |
| `b4496da` | Editor columns equal width |

- Repo layout: `heatplanner/` (the package, deploy this), `heatplanner/static/` (new: dashboard files),
  `deploy/systemd/` (unit files as deployed, unchanged), `deploy/ha/` (automation and card YAML for the owner),
  `dev/make_demo_data.py` (builds a fake data folder, no HA needed), `tests/test_offline.py`, `README.md`.
- Still stdlib only (Python 3.13). No pip installs.

### Getting it onto the Hermes host
The repo may be private. Use whichever works:
1. `git clone -b claude/inspiring-mendel-5cg8xp https://github.com/jmaquarrie/Heat-Eatimator.git ~/heatplanner-v2`.
   If that asks for credentials, ask the owner for a fine-grained GitHub token with read-only "Contents" access
   to this one repo. Use it for the clone only, and don't store it in the repo or in any file you hand back.
2. Or ask the owner to download the branch as a zip from GitHub and drop it on the Hermes host.

Before deploying, check nobody changed the live code since the first handover. If this diff shows anything,
stop and tell the owner:
```bash
# on the Hermes host, with the repo checked out
git -C ~/heatplanner-v2 show 5d97f9a:heatplanner/planner.py > /tmp/base_planner.py
ssh -i ~/.ssh/proxmox-hermes hermes@192.168.1.152 'cat /opt/heatplanner/heatplanner/planner.py' > /tmp/live_planner.py
diff /tmp/base_planner.py /tmp/live_planner.py && echo "live code matches the baseline"
# repeat for model.py, data.py, learn.py, server.py, cli.py, ha.py (or diff the whole folder after a tar copy)
```

## 2. What changed, in plain terms

### Dashboard (port 8110, same service)
- New single-page UI served from `heatplanner/static/`. The old page is still at `/classic`.
- **Phones:** bottom tabs (Now · Plan · Week · Home · More). **Desktop:** one page with a top nav.
- **Now:** room now vs target and vs prediction, next setpoint change, today's setpoint strip with a "now"
  marker, tariff now and next, battery, outside temperature, water offset, hallway and landing temperatures.
- **Plan:** headline in real pounds, with comfort kept separate (the old "saving" mixed electricity with a
  priced comfort penalty). There's a shared-time chart: room, water temperature, outside, pump kWh, battery.
  Drag handles on the chart's setpoint line change a block.
- **Edit:** a ±0.5 °C setpoint and ±1 water offset per block, re-simulated instantly through `/api/simulate`
  (no HA calls). "Push to Home Assistant" writes only the plan's weekday in `schedule.heat_pump_planner`,
  after a confirmation. "Undo" (`/api/restore`) puts the optimiser's choice back.
  **Which day is shown:** the Plan view and editor open on the **next day**. A day picker and the Week cards open
  any week-ahead day (`GET /api/day?date=`, rebuilt from the stored forecast with the outlook's assumptions).
  Pushing a week-ahead day writes that weekday now **and pins it** in `data/day_overrides.json`, and
  `build_plan()` uses a pinned edit for its day instead of re-optimising (chosen = "Your edit",
  `manual.pinned`). Undo drops the pin. Today is only shown via **Edit today's plan** on the Now card.
  **Today's routine can be edited for blocks not yet ended.** During the day the plan is for today, so the
  editor locks ended blocks, and `push_values` refuses changes to them. After 22:00 (the plan is for
  tomorrow) a Today/Tomorrow switch appears: "Today" posts `{"day":"today", "today_values", "today_offsets"}`
  to `/api/push`, which calls `planner.push_today()`. That writes only today's weekday in the helper, updates
  the plan's `sim.fixed`, and leaves tomorrow's plan alone.
- **Compare:** strategy cards led by electricity in pounds and comfort as "time below 19.5°". The model score
  is folded away.
- **Who's home:** weekly pattern plus a two-week tap-to-cycle calendar (same `/api/occupancy` as before).
- **Model:** maturity banner, backtest, yesterday predicted vs measured, and measured grid import and cost from
  the meter.
- Optional `HEATPLANNER_WRITE_KEY` in `/etc/heatplanner.env` makes push, restore, occupancy and rerun need a
  key. Not set by default (same behaviour as before). Proper protection would be the nginx proxy manager at
  192.168.1.142 with auth; that also turns on HTTPS, the phone app install and offline view. Owner's call.

### Model and planner
- **Leaving-water offset planning (the main change).** The pump is in weather-dependent mode
  (`sensor.heat_pump_climate_control_setpoint_mode` = `weatherDependent`), so
  LWT = curve(outdoor) + offset. The model now caps radiator output at `k_emit * (LWT - 2.5 - room)` and takes
  COP at that LWT. The HA-measured COP is scaled by Daikin's published LWT sensitivity. The optimiser picks a
  setpoint **and** an offset per block; each offset change costs 1p in the objective (Daikin API budget).
  Offsets ride in the helper block data: `data: {temperature: 20, lwt_offset: -3}`.
  Config: `"lwt_offset": {"enabled": true, "min": -5, "max": 5, "step": 1, "write_cost_gbp": 0.01}`, clipped to
  the entity's own `min_temp`/`max_temp`. Set `"enabled": false` in `data/config.json` to go back to
  setpoints only.
- New fitted parameters (refit daily in `learn.py` from stored LWT, room and heating data, blended with the
  prior): `lwc_a` 36.4 and `lwc_b` −0.55 (weather curve, assumption until fitted), and `k_emit_kw_per_k` 1.0.
  0.4 could not reproduce the observed 18.4 → 21 °C afternoon rise; 1.0 can. The backtest now runs in the
  same weather-compensation mode the planner uses.
- **End-of-window fix:** the "heat left in the house" term now only charges for ending colder than 19.5 °C.
  It gives no credit for ending warmer. The old fixed-COP credit made overheating at 22:00–24:00 look
  profitable once offsets raised the real COP.
- New per-strategy output `hours_below` (time at home and awake below 19.5 °C) and `writes`. There's a new
  reference strategy, "Setpoints only (offset unchanged, for reference)", so the dashboard can show what the
  offset adds.
- Battery: separate `max_charge_kw` / `max_discharge_kw` (both 2.5). `discharge_limit_entity` exists but is
  empty on purpose.
- Each saved plan now carries a `sim` block (forecast, start state, today's fixed blocks), so the dashboard can
  re-simulate without HA. Plans written before the upgrade are re-simulated approximately (no solar term) until
  the next 22:00 run.
- After every plan the CLI posts `sensor.heatplanner_status|saving|cost|kwh|min_temp` into HA
  (`POST /api/states`; state only, no config change). Status shows `error` if a run fails. To disable:
  `"publish_sensors": false`.

### Data collection (`data.py`), new optional fields in `obs.json`
- `grid_kwh` from `sensor.octopus_energy_electricity_20e5320146_1610023144959_current_total_consumption`
  (Home Mini lifetime counter; spread linearly so it stays fast with dense readings).
- `lwt_offset` history of `climate.altherma_leaving_water_offset` (its `temperature` attribute), via the new
  `HA.history_attr()`.
- `battery_soc`, `battery_charge_w`, `battery_discharge_w`
  (`sensor.marstek_m1_battery_state_of_charge`, `sensor.anker_solix_battery_charging_power`,
  `sensor.anker_solix_battery_discharging_power`), stored for a future "learned battery" model.
- Live-only (dashboard): `sensor.anker_solix_grid_import_power` and the hallway and landing motion-sensor
  temperatures.
- Old rows simply lack the new fields; nothing needs migrating.

### New endpoints
`GET /api/status`, `/api/actuals?date=`, `/api/config`, `/api/log`, `/sw.js`, `/manifest.webmanifest`, `/static/*`;
`GET /api/day?date=`; `POST /api/simulate`, `/api/push`, `/api/restore`, all taking `"date"` (`occupancy`, `rerun` as before). New data file: `data/day_overrides.json` (pinned future edits; safe to delete). See `README.md`.

## 3. Deploy steps (Hermes → LXC 122)
1. **Back up first:**
   `ssh hermes@192.168.1.152 'sudo tar czf /opt/heatplanner-backup-$(date +%F).tgz -C /opt heatplanner'`
   (includes `data/`).
2. **Run the tests locally** on the Hermes host: `cd ~/heatplanner-v2 && python3 -m unittest discover -s tests`
   (8 tests, about 15 s, no HA needed).
3. **Copy only the package:** `tar czf hp.tgz heatplanner` → scp → extract into `/opt/heatplanner` (replacing
   `heatplanner/`, **leaving `data/` alone**) → `sudo chown -R heatplanner:heatplanner /opt/heatplanner` →
   remove `__pycache__`. Make sure `heatplanner/static/` came across, PNGs included.
4. **Check the new entities answer** (read-only), as the service user with the env loaded:
   ```bash
   sudo -u heatplanner bash -c 'set -a; . /etc/heatplanner.env; set +a; cd /opt/heatplanner && python3 - <<EOF
   from heatplanner.ha import HA; from heatplanner import data
   ha=HA(); e=data.load_config()["entities"]
   for k in ("lwt_offset","grid_import_kwh","grid_power","battery_charge_w","battery_discharge_w"):
       s=ha.state(e[k]); print(k, s["state"], {a:s["attributes"].get(a) for a in ("temperature","min_temp","max_temp")} if k=="lwt_offset" else "")
   EOF'
   ```
   `lwt_offset` must show a numeric `temperature` (the current offset) and `min_temp`/`max_temp`. If it
   doesn't, set `"lwt_offset": {"enabled": false}` in `data/config.json` and tell the owner.
5. **Dry run, no push:** `... python3 -m heatplanner.cli plan`. It now takes roughly 2–3× longer than before
   (the outlook also tunes offsets); the unit's 600 s timeout is plenty. Check the printout shows
   `Setpoints only (offset unchanged, for reference)` and an `Optimised` line, and that the optimised blocks
   carry `lwt_offset` values within −5..+5.
6. **Restart the dashboard only:** `sudo systemctl restart heatplanner-web`. Open http://192.168.1.152:8110.
   Check the status chips fill in and `/api/status` shows `"available": true`.
7. **Leave the 22:00 nightly to push** as normal. Don't push by hand unless the owner asks.
8. **Optional, to see the new fits straight away:** `... python3 -m heatplanner.cli learn`, then read the
   `notes` in `data/params.json`. Look for "weather curve fitted …" and "radiator output fitted …". Sanity
   ranges: `lwc_b` −1.2…−0.1, `k_emit_kw_per_k` 0.3…3. Tell the owner the fitted values.

**Rollback:** stop nothing; extract the backup tarball's `heatplanner/` back over `/opt/heatplanner/heatplanner`,
chown, then restart `heatplanner-web`. `data/` is forward and backward compatible (new keys are ignored by the
old code).

## 4. What the owner still has to do in HA
1. Review and paste `deploy/ha/planner_to_thermostat.yaml` (setpoint → `climate.altherma_room_temperature`).
2. Review and paste `deploy/ha/planner_offset_to_daikin.yaml` (lwt_offset → `climate.altherma_leaving_water_offset`).
   Both skip unless the value changes, write at most once per 55 min, and stop below 15 Daikin calls left.
   `sensor.altherma_gateway_ratelimit_remaining_day` showed 130 at 16:40 on 21 Nov, so there's more headroom
   than the first handover's "35 spare".
3. Keep `automation.heat_pump_schedule_temperature` off once these are on.
4. Optional: `deploy/ha/dashboard_card.yaml` for an HA card with the `sensor.heatplanner_*` values.

## 5. Open points and assumptions to watch
- `k_emit` and the weather curve drive the offset benefit. In the demo, offsets save about 28p a day of
  electricity over setpoints alone, with the same comfort. Check the backtest table on the dashboard after a
  week of offset data before trusting that.
- The offset range is −5…+5 for safety; the Daikin allows −10…+10. Widen it only if the backtest is good and
  the owner agrees.
- The optimiser often sits at −5 overnight and in the morning. If the house feels slow to warm, raise
  `lwt_offset.min` (e.g. −3) in `data/config.json`.
- `sensor.average_house_sensor` reads 23.9 °C, warmer than any listed room. It may be averaging the wrong
  sensors; it isn't used by the planner.
- Ideas the owner liked but not built yet: Octoplus **Power Down** events
  (`calendar.octopus_energy_a_f96d88d6_octoplus_power_down`: price those windows high so the plan pre-heats
  before them), checking tomorrow's rates via
  `event.octopus_energy_electricity_20e5320146_1610023144959_next_day_rates`, "away" events from
  `calendar.family` feeding Who's home, and a learned battery model from the new battery history.
- Dev without HA: `python3 dev/make_demo_data.py demo-data && HEATPLANNER_DATA=demo-data HEATPLANNER_PORT=8111 python3 -m heatplanner.server`.
