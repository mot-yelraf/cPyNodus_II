# NodusWeb Switch Automations

Local switch automations run only when `Profile.ACTIVE_PROFILE = "nodusweb"`
and `switch.toml` enables at least one physical channel. MQTT profiles do not
load or execute the evaluator, do not register its HTTP API, and remain
headless in normal mode.

Rules are stored in `automations.toml` beside the normal live settings files.
The file uses Sensorius-compatible `[Meta]` and `[Advanced]` sections with a
compact `script_json` value. Rules copied from Sensorius may use local sensor
metrics, time windows, timers, AND/OR condition groups, multiple On/Off
actions, hysteresis, delays, `previous_state`, and `do_nothing`. Astral
conditions and non-local sensor or switch targets are rejected.

The private `.nodus_automation_state.json` file stores only active
previous-state ownership. This lets a rule restore the state that preceded its
action after a controlled restart. It is written on ownership transitions,
not on every evaluation.

## Conditions and actions

- `sensor`: local metric, operator, threshold, and optional hysteresis.
- `time`: local `HH:MM` start/end and weekdays `0` through `6`, Monday through
  Sunday. Equal start/end means all day; windows may cross midnight.
- `timer`: active `duration_min` within a repeating `period_min`.
- `or`: begins another group; conditions within a group are ANDed.

Each action selects a local switch channel, an absolute On/Off state, a delay
from zero through 60 seconds, and either `previous_state` or `do_nothing` when
the rule becomes false.

An enabled rule owns its target channels. The status page identifies that
ownership and rejects manual changes until the rule is disabled. Unowned state
buttons can be clicked to toggle a channel; the browser and backend enforce a
per-channel five-second manual guard.

## Runtime and limits

The evaluator runs in the existing cooperative main loop at most once every
five seconds. It adds no task, sensor poll, socket, MQTT subscription, or MQTT
publish. Sensor conditions consume the latest successful sample from the
existing 60-second NodusWeb cadence.

To preserve Pico2 W heap headroom, one device accepts at most 12 rules, 12
conditions per rule, four actions per rule, and 1,536 serialized script bytes.
The local API is available only for an active NodusWeb switch runtime:

- `GET /automations-ui`: load the on-demand local rule editor.
- `GET /automations`: list rules and local target catalogs.
- `POST /automations`: validate and save one rule.
- `POST /automations/delete`: remove one rule and release its actions.

Rule replacement is persistence-first: a failed file update leaves the active
rule and switch ownership unchanged.
