# Hermes ↔ Heat Planner: agent integration

This lets Hermes (the agent on LXC 118) supervise the heat planner during the day. Hermes checks in at
regular times, reviews how today is going, makes small adjustments within guard rails, and writes a short
update explaining each change. The owner can also chat with Hermes from the dashboard: there's an orb in the
bottom-right corner that opens a chat panel.

The planner does the maths and Hermes does the judgement. Hermes asks the planner for a projection or a
re-optimised suggestion (scored in points, where what's in force now = 100), decides whether it makes sense (weather, who's home, what the owner said in chat),
and applies it through one guarded endpoint. Every change is recorded with its reason, drawn on the dashboard
chart as an **H** marker, and posted to the chat as a "Change made" card.

Base URL: `http://192.168.1.152:8110` (LAN). All bodies are JSON.

## Authentication
Set `HEATPLANNER_AGENT_KEY=<random string>` in `/etc/heatplanner.env` on LXC 122, then restart
`heatplanner-web` (with the owner's go-ahead). Hermes sends it as the `X-Agent-Key` header on every
`/api/agent/*` call. If the agent key is unset the write key (`HEATPLANNER_WRITE_KEY`) is used instead, and
if neither is set the agent API is open to the LAN. Keep the key in Hermes's own `.env`, never in chat,
files or git.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/agent/state` | Everything needed for a check-in in one call (see below). Also records "Hermes last seen". |
| GET | `/api/agent/inbox` | The owner's chat messages not yet read, which are then marked read. Add `?peek=1` to look without marking, or `?since=<id>`. |
| POST | `/api/agent/project` | `{"values":[7], "offsets":[7]}`: simulate the rest of today from the **measured room temperature now**. Omit both to project what's in force. Read-only. |
| POST | `/api/agent/suggest` | `{}`: the planner re-optimises today's blocks that haven't ended, from the measured state now. Returns current vs suggested values, a plain list of changes, and the electricity saving. Read-only. |
| POST | `/api/agent/adjust` | `{"values":[7], "offsets":[7], "reason":"…"}`: apply to the rest of today, within the guard rails. Writes `schedule.heat_pump_planner`, records a revision, and posts a "Change made" update to the chat with the reason and the projected effect. |
| POST | `/api/agent/message` | `{"text":"…", "kind":"message"\|"update", "title":"…", "read_upto":<id>}`: reply in chat, or post a written update that isn't tied to an adjustment. |

The seven values always follow the planner's blocks: 00–04, 04–07, 07–13, 13–16, 16–19, 19–22, 22–24.
Setpoints are °C in 0.5 steps (16–22). Offsets are whole °C within the configured range (default −5…+5).
Blocks that have already ended must be sent unchanged; the API refuses otherwise.

### `GET /api/agent/state` returns
- `now`, `tariff_now` (cosy / standard / peak), `occupancy_today`, `comfort` floors
- `live`: room, outdoor, battery %, rate, heating on/off, current offset, Daikin API calls left, whether the
  pump is following the plan
- `today`:
  - `values` / `offsets` / `blocks` in force
  - `ended_blocks` (indices)
  - `room_now` vs `predicted_now`
  - `summary` of the projected rest of the day (electricity £, time below comfort, coldest awake temperature)
  - `projection`: hourly room, target, offset and water temperature from now on
- `revisions_today`: every change so far today (who, when, why, what)
- `unread_messages`: the owner's chat messages since Hermes last read
- `guard_rails` and `agent` (last seen and last adjustment times)

## Guard rails (enforced by the planner, not just requested)
- A `reason` is required. The owner reads it.
- At most **2.0 °C** change per block versus what's in force (`agent.max_setpoint_change_c`).
- At most **8** agent adjustments a day (`agent.max_adjustments_per_day`), and at least **20 min** apart
  (`agent.min_minutes_between`).
- Only blocks that haven't ended. Setpoints 16–22 °C, offsets within the configured range.
- Writes only `schedule.heat_pump_planner`, never the live schedule `schedule.heat_pump_schedule_2`.
- Each push becomes up to two Daikin writes, via the owner's HA automations. Keep an eye on
  `live.api_budget` and skip adjustments below about 25 calls left.

Change the limits in `/opt/heatplanner/data/config.json`, e.g. `{"agent": {"max_adjustments_per_day": 6}}`.

## Suggested check-in routine
Run it at, for example, 07:00, 10:00, 12:30, 15:00, 16:30 and 19:30, plus whenever the owner sends a chat
message.

1. `GET /api/agent/state`.
2. Answer any `unread_messages` first, with `POST /api/agent/message` (pass `read_upto`). If the owner asked
   for something ("warm the snug for 7pm", "we're out until 6"), work out the values and go to step 5.
3. Compare `room_now` with `predicted_now`. As a rule of thumb, act only if they differ by **more than
   0.7 °C**, if the occupancy is different from planned, or if the owner asked.
4. `POST /api/agent/suggest`. Consider applying it if `score_gain_points` is at least 2 (score in points, where what's in force now = 100), or comfort improves
   (`hours_below` drops), and it agrees with what you know. Otherwise do nothing; that's the normal case.
   - **Water offset:** the model may keep the offset at the low end of the range. If the house is lagging
     the prediction (cooler than predicted and the setpoint not being reached), raising the offset by 1–2
     for the next block or two is the right lever. Check with `/api/agent/project` first.
5. `POST /api/agent/adjust` with the values and a one- or two-sentence reason in plain English, e.g.
   "The room is 1.1° below the plan at 15:00 and it's colder than forecast, so I've raised the water offset
   for the peak and evening to keep you at 20°."
6. Don't post when nothing changed, except an optional short evening summary (`kind: "update"`) at 21:30:
   what changed today and how the day went.

## Getting chat messages to Hermes promptly
- **Polling:** `GET /api/agent/inbox` every 1–2 minutes while the owner is likely to be around. Cheap; no
  HA or Daikin calls.
- **Webhook (optional):** set `{"agent": {"webhook_url": "http://192.168.1.148:<port>/heatplanner"}}` in
  `config.json`.
  - The planner POSTs `{"type":"chat","message":{…},"state_url":"/api/agent/state","reply_url":"/api/agent/message"}`
    the moment the owner sends a message.
  - With `"checkin_times": ["07:00","12:30","16:30"]` it also POSTs `{"type":"checkin",…}` at those times.
    Use that if Hermes prefers to be woken rather than run its own schedule.

## Where things are stored (LXC 122, `/opt/heatplanner/data/`)
- `chat.json`: chat and updates (last 500).
- `revisions.json`: every change per day (21 days kept); drives the H/Y markers and the "Changes to this
  day" list.
- `agent_state.json`: last seen, last adjustment, read cursor.
- `day_overrides.json`: pinned edits for future days.
- All are safe to delete; the dashboard just starts with an empty history.

## Example (bash)
```bash
H='X-Agent-Key: '"$HEATPLANNER_AGENT_KEY"
B=http://192.168.1.152:8110
curl -s -H "$H" $B/api/agent/state | jq '.today | {room_now, predicted_now, summary}'
curl -s -H "$H" -X POST $B/api/agent/suggest -d '{}' | jq '{score_gain_points, changes: .suggested.changes}'
curl -s -H "$H" -X POST $B/api/agent/adjust \
  -d '{"values":[18,20,19.5,21,20,20,17.5],"offsets":[-5,-5,-5,-3,-2,-2,0],"reason":"Room is lagging the plan by 0.9°; raising the water for the evening."}'
curl -s -H "$H" -X POST $B/api/agent/message -d '{"text":"Done, the snug should be at 20° by 7pm.","read_upto":12}'
```
