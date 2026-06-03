# Command Lazy-Import Memory Notes - 2026-06-01

## Context

Devices under test:

- `co2-ykdvea` on `cu.usbmodem133301.log`
- `co2-frank` on `cu.usbmodem1334401.log`
- Broker: `samhain.local` / `10.0.0.248`
- CircuitPython: `9.2.8`

The investigation focused on `config/set` and `calibration/set` commands after
earlier work moved heavy command modules off the steady-state hot path. Startup
stability is the primary constraint: fixes for command handling must not add
imports, queues, or diagnostics to the always-loaded runtime path.

## Observed Version Boundary

- `v0.26.151.2` started and synced NTP on `co2-frank`, with MQTT connect
  context around 102 kB free and post-connect memory around 103 kB free.
- `v0.26.152.2` introduced explicit calibration and device-config prestage
  attempts. On `co2-ykdvea`, both prestage paths failed with `MemoryError`, and
  NTP began failing the 2560-byte allocation.
- `v0.26.152.3` removed device-config prestage and added extra instrumentation
  plus a display config fast path, but still regressed startup footprint. On
  `co2-frank`, MQTT preflight failed with `RuntimeError:Out of sockets` before
  command handling or NTP could run.
- `v0.26.152.4` rolled back the always-loaded additions. Startup/MQTT/NTP
  behavior recovered on the affected devices.

## Lessons Learned

- Do not prestage command modules after startup. The attempted calibration and
  device-config prestage imports consumed heap and failed under long-soak
  fragmentation.
- Do not add command diagnostics to `MQTTTransport` or the app loop unless there
  is a bounded hardware validation reason. Even compact queues and per-loop
  drains increase the baseline footprint.
- Do not put display/config/calibration helpers into the always-loaded command
  intake path unless they are guarded by a very cheap payload prefilter.
- NTP is best-effort and should not be wrapped in extra GC/logging from the main
  loop. The added instrumentation made the first NTP window noisier without
  fixing the allocation failure.
- Serial-side success is not enough for MQTT validation. Broker-visible
  subscriptions, acks, results, and data remain required.

## Current Direction

The safe pattern is:

1. Keep startup and steady-state imports as close to `v0.26.152.4` as possible.
2. Use cheap topic and payload string prefilters before importing lazy command
   handlers.
3. Split command handlers so the first lazy import is small.
4. Catch `MemoryError` at each lazy-import boundary so command failures do not
   unwind the runtime.
5. Validate after a long soak, not only immediately after boot.

For `v0.26.152.5`, the next calibration step is to keep the apply parser and
publish behavior in `calibration_config.py`, move TOML file mutation into
`calibration_persistence.py`, and route only likely `apply`/`set`/`update`
offset payloads through the fast calibration path. Status and soil pH session
commands still fall through to the full command handler.

Follow-up validation showed that `v0.26.152.5` kept `co2-ykdvea` online after a
long-soak `calibration/set`, but the first lazy calibration import still failed
with `calibration_handler_memory` and published no broker-visible
`calibration/ack` or `calibration/result`. For `v0.26.152.6`, the containment
path now publishes a minimal `calibration/ack` and failed `calibration/result`
directly from `command_intake.py` when the lazy import raises `MemoryError`.

Broker validation after a Sensorius update confirmed that the controller now
sends one offset per remote Nodus apply request and stops after the first
failed result. That reduced payload pressure, but `co2-ykdvea` still reported
`calibration_handler_memory` on the first single-offset command, proving that
the remaining failure was the Nodus handler import boundary, not the size of
the offsets list.

For `v0.26.152.7`, `payload.offsets` calibration applies use a smaller
`calibration_offsets.py` path before falling through to the full calibration
handler. The path parses the offsets envelope by scanning the command text,
uses a tiny single-offset TOML writer for the common Sensorius split-command
case, applies the runtime offset, and publishes the standard
`calibration/ack`, `calibration/result`, and `meta/patch`. The existing
`requested` field in the serial command log now carries compact markers such as
`offset_fast:applied` or `offset_fast:persist` for this path, avoiding a new
always-on diagnostic queue.

Hardware validation showed `v0.26.152.7` still failed at
`calibration_offsets_memory`, with no `offset_fast:*` marker. That means the
new smaller lazy handler still failed at the import/call boundary before the
single-offset parser could report a narrower phase. For `v0.26.152.8`, the
single-offset parser, runtime apply, ack/result publish, and `meta/patch`
builder moved directly into already-loaded `command_intake.py`. The only lazy
import remaining on the Sensorius single-offset path is the single-key TOML
persistence helper after runtime apply succeeds. The serial command log now
uses `offset_inline:applied`, `offset_inline:persist`, and
`offset_inline:runtime` markers.

Hardware validation of `v0.26.152.8` regressed startup: MQTT preflight failed
with `RuntimeError:Out of sockets` at first connect, with connect-context free
memory around 88 kB. That matches the earlier lesson from `v0.26.152.2` and
`v0.26.152.3`: moving command code into always-loaded modules can consume
enough baseline heap/socket room to break startup before command handling is
tested. `v0.26.152.9` backs out the inline `command_intake.py` growth and
restores the staged `calibration_offsets.py` / `calibration_offset_parse.py`
lazy path from `v0.26.152.7` while keeping the version distinguishable for
deployment.

## 2026-06-02 Calibration Appliance Findings

The `testApparatus/calibration_test.py` appliance reproduced the calibration
path with no `code.py` auto-starting the normal app. Broker-loop mode exposed a
separate appliance subscription issue: the first `calibration/set` subscription
could fail during SUBACK handling with `mqtt_suback_stage=remaining:pystack
exhausted` despite more than 220 kB free heap. Direct command-source mode then
bypassed subscription and exercised the real app command path through
`command_intake.process_inbound_messages`.

Direct mode with `persist=False` passed for serialized Sensorius-style CO2 and
altitude offsets. Broker-visible output showed `calibration/ack`,
`calibration/result`, and `meta/patch` for each single-offset command, and the
serial command marker reported `requested=offset_fast:applied`.

Direct mode with `persist=True` failed differently: MQTT connected cleanly,
subscription was skipped, the command was received, and then command handling
raised `RuntimeError: pystack exhausted` while more than 210 kB heap was still
free after GC. That isolated the failure to TOML persistence call depth rather
than heap pressure or MQTT subscription. The single-offset path was still
attempting persistence before runtime apply and publish, so the stack failure
prevented broker-visible success.

For `v0.26.153.1`, calibration apply paths now apply runtime state and queue
`calibration/ack`, successful `calibration/result`, and `meta/patch` before
attempting TOML persistence. Persistence remains best-effort in the current
command model: `MemoryError` and `pystack exhausted` during persistence are
reported through `CommandResult.errors` and `persistence_mode=volatile`, but
they no longer turn a valid calibration apply into a failed MQTT-visible
command. A future lower-risk improvement would add a true post-publish
persistence queue, but that should not add baseline startup imports or socket
ownership to feature code.

The calibration appliance now mirrors that distinction with
`allow_volatile_persistence=True` by default. A published command with
`persistence_mode=volatile` and persistence-only errors is treated as a command
pass, so serialized offset tests continue to the next offset while still logging
the persistence diagnostic.

For `v0.26.153.2`, the single-offset TOML persistence helper was flattened to
reduce CircuitPython Python-stack depth. The Sensorius split-offset path now
uses one shallow function for active sensor filename selection, line scanning,
write-through to a temporary TOML file, and backup/rename. The writer avoids
nested context managers and per-line helper calls so the post-publish
persistence step can succeed instead of reporting
`calibration_offset_persist_pystack`.

## 2026-06-03 Calibration Appliance Heap Pressure Findings

After failed attempts to add automated MQTT heap sweeps, the appliance was
restored to the known-good `mqtt_round_trip()` path with version logging
(`caltest-2026-06-03.4`). The useful manual test shape was direct command
source, `self_publish=False`, `persist=False`, one Sensorius-style
`Calibration.Device.CO2_OFFSET=-130.0` offset, and explicit `pressure_bytes`.

Fresh REPL single-threshold runs produced broker-visible `calibration/ack`,
successful `calibration/result`, and `meta/patch` at these pressure points:

- `0 KiB` pressure, `207696` bytes free before command, passed as
  `calmqtt-753-r1-1`.
- `50 KiB` pressure, `173824` bytes free before command, passed as
  `calmqtt-33-r1-1`.
- `100 KiB` pressure, `120688` bytes free before command, passed as
  `calmqtt-26-r1-1`.
- `150 KiB` pressure, `66688` and then `66848` bytes free before command on two
  fresh runs, passed as `calmqtt-31-r1-1` and `calmqtt-30-r1-1`.

The 150 KiB pressure runs are the most useful lower-bound data. Both completed
the command and drained all three MQTT outputs while free heap dropped into the
mid-35 KiB range during output sync. That means the observed boundary is not
simply "MQTT publish needs about 65 KiB free"; the fragile point in this test is
earlier, around command receipt and command processing.

At `165 KiB` pressure, allocation succeeded (`168960` requested and allocated),
but command processing failed with `mqtt_round_command_memory_error`. The log
showed `53728` bytes free after pressure allocation, `52400` before direct
receive, and `50960` before command handling. No broker-visible ack/result/meta
was produced for that attempt. At `180 KiB` pressure, the pressure allocation
itself failed (`183808` allocated of `184320` requested, `39568` free) and the
command was not attempted.

The current measured command-entry threshold for this appliance path is
therefore between roughly `51 KiB` and `67 KiB` free heap. That does not prove
that the original Sensorius `calibration_offsets_memory` failure was caused by
raw free heap alone: fragmentation, prior imports, warm-start state, and stack
depth can still change the effective headroom. It does show that the direct
single-offset path can apply and publish successfully with substantially less
than 100 KiB free, and repeatably fails once command-entry free heap is near
51 KiB.

A separate top-level loop over multiple pressure points produced
`mqtt_connect_failed:10.0.0.248:raw:OSError:[Errno 116] ETIMEDOUT` on later
points before pressure allocation. Those failures should be treated as
warm-start/MQTT session behavior, not heap-threshold evidence.
