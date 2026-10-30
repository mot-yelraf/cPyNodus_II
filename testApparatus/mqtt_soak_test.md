# Long-running MQTT soak apparatus

`mqtt_soak_test.py` investigates the October 25 failure in which Nodus ran for
roughly an hour without Sensorius, then stalled for approximately ten seconds
on a small heartbeat publication. It separates this runtime transport symptom
from the old `platform_test` harness's deep-call `pystack exhausted` failure.
See the [investigation record](../docs/debug-notes/mqtt-runtime-stall-2026-10-25.md).

## What runs

The normal application must be stopped. The apparatus reads existing settings
and uses core network ownership, the firmware MQTT adapter, and firmware
payload/subscription builders. By default it does not start sensor drivers,
relay GPIO, web services, command handlers, or OTA processing. The default writes no files.
Opt-in filesystem comparisons write only `_mqtt_soak_test.log` and its
rotation files in `settings_root`; settings, production logs, and NVM markers
are untouched. It never requests an MCU reset or soft reload.
Incoming command packets are acknowledged where necessary and discarded.

Defaults:

- Two-hour duration, stopping at the first operation failure.
- Plain MQTT on an existing station `sensorius` or `weewx` configuration with
  a configured literal `MQTT.BROKER_IP`; no DNS refresh or persistence.
- A normal TCP preflight and adapter CONNECT; no extra raw CONNECT probe,
  conditioning sequence, or five-second pre-connect delay from the old harness.
  TCP preflight retries `EINPROGRESS` / errno 119 up to three total attempts,
  with 0.5 seconds between attempts, matching the production preflight policy.
  Each error is logged and counted separately in `preflight_errors`. Other
  preflight errors stop immediately. `preflight_attempts=1` restores a strict
  single-attempt control. This retry also applies with `recover=False`;
  recovery controls what happens after an operation or exhausted preflight fails.
- Firmware startup metadata and device subscriptions, followed by deferred
  switch subscriptions. Retained startup metadata uses the adapter's current
  QoS verification threshold and 256-byte chunks, with 350 ms publication pacing.
- Synthetic sensor data every 60 seconds; retained heartbeat, sensor
  availability, and switch availability every 30 seconds, in firmware order.
- Periodic GC every 300 seconds, plus the adapter's existing startup GC.
- A diagnostic `probe` base topic, replacing `nodus` only in the in-memory test
  configuration. Both names have five characters, preserving topic byte lengths
  for the current device's configured `nodus` base. Other configured bases may
  have a different length. MQTT client identity remains the configured identity:
  never run this concurrently with the normal app or another copy of the test.

Default snapshots are synthetic and do not confirm physical switch state. Default
metrics are representative AVPD values, with `CO2` replacing `Pressure` for
a configured CO2 sensor. These are eight fixed synthetic values, not measured
values or executions of the sensor derivation code. Pass `metrics={...}` to match an
observed sensor payload more closely; packet sizes are logged. RTC time is used
as-is; no NTP sync is attempted. Record whether RTC was already synchronized,
because timestamp width can affect payload sizes. Firmware version and test
version are both printed. Unlike the old harness, metadata uses the firmware
version rather than the probe version.

## Real sensor acquisition comparison (October 28)

Apparatus version `2026-10-28.1` adds `sensor_mode="real"`. The default remains
`sensor_mode="synthetic"`; use the same new apparatus version for both controls.
No production firmware modules or firmware version are changed.

For co2-pmoopn, the operator must restore the intended sensor configuration
before testing. The current switch-only configuration cannot supply real sensor
data. Confirm the controls show the expected CO2 sensor and two switch channels.
The normal app must be stopped. Relay GPIO is not initialized with the default `switch_mode="inactive"`,
and commands are still discarded; account for attached loads when stopping the
app. This comparison does not validate relay state.

```python
import gc
gc.collect()
import mqtt_soak_test
result = mqtt_soak_test.run(duration_s=50400, sensor_mode="real")
```

Real mode uses the production sensor planner, runtime builder, hardware binding,
and sensor service, without importing the main app. It initializes the sensor
and takes an initial sample before MQTT connect. Each subsequent 60-second
sensor interval acquires a fresh snapshot, including calibration and derived
metrics, then queues it through the existing sensor payload builder. Sensor
work precedes the availability check, preserving the apparatus's existing
ordering when both timers are due. MQTT sends remain direct calls from `run()`;
the acquisition helper has returned before sending. Reconnect startup reuses the
latest snapshot; MQTT recovery does not restart the sensor hardware.

`sensor_begin` / `sensor_end` report sample number, phase, metric count,
acquisition/processing duration, and errors. `before_sensor` / `after_sensor`
report heap and queue depths without adding GC. A `waiting` sample publishes no
data and does not replay an older sample; liveness scheduling continues. Other
sensor phases or acquisition exceptions stop the run after recording failure
evidence, even with `recover=True`. They increment `sensor_errors`, separately
from MQTT `failures`, and do not trigger station recovery. There is no sensor
reinitialization/reset policy. Real mode requires a configured sensor and rejects
`metrics={...}` overrides. Native sensor calls can exceed the requested duration;
time limits are checked between calls.

The summary adds `sensor_mode`, `sensor_reads` (attempts including the initial
sample), `sensor_skips` (non-ready returned samples), `sensor_errors`, and
`sensor_cleanup_errors`. Sensor cleanup is attempted after MQTT cleanup on normal
exit, failure, or Ctrl-C. Check it independently of `cleanup_ok`, which still
describes MQTT cleanup. The firmware stop helper suppresses individual deinit
exceptions, so zero sensor cleanup errors cannot prove electrical bus release.

Keep board, deployed firmware/libraries, sensor and switch configuration,
filesystem mode, broker, startup method, consumer state, intervals, and capture
filters constant between synthetic and real runs. Capture `probe/#`, device TCP
traffic, and serial. Use the same fresh-start procedure for each run: sensor
modules can remain resident after cleanup, contaminating a subsequent synthetic
control in the same interpreter.

Real mode adds sensor imports, resident driver/bus objects, and acquisition
allocations. It does not equalize heap with the synthetic control, reproduce the
whole app, add NTP, or start switch services unless `switch_mode="real"` is selected. These remain comparison limits.
Host tests exercise changing CO2 readings through firmware acquisition,
calibration, derivation, payload, and adapter paths with simulated hardware.
On-device startup stack headroom, real I2C acquisition, and broker delivery
remain unvalidated. Begin with a short run reaching `event=operational` and
multiple fresh broker-visible data samples before the long comparison.

## Real switch service comparison (October 28)

Apparatus `2026-10-28.2` adds opt-in `switch_mode="real"`. The default
`switch_mode="inactive"` preserves the existing control. Switch availability
was already published every 30 seconds for configured channels in both modes.

Real mode uses the production switch planner, runtime, GPIO binding, and
service without importing the main app. It requires a configured switch and
**applies saved channel states at startup**, which can energize attached loads.
The operator must select safe loads and stop the normal app before running.
The operator owns copying and executing this apparatus on the device.

For the combined acquisition/service comparison:

```python
import gc
gc.collect()
import mqtt_soak_test
result = mqtt_soak_test.run(
    duration_s=50400, sensor_mode="real", switch_mode="real"
)
```

Switch startup precedes sensor startup and MQTT connection. Output snapshots
are read at startup and before each periodic availability refresh (30 seconds
by default). `switch_snapshot` logs states and read duration; `before_switch`
and `after_switch` log heap/queue depths without adding GC. Initial snapshots
feed startup switch metadata. Subsequent snapshots are serial diagnostics;
periodic MQTT topics, payload shapes, and cadence remain unchanged. GPIO output
readback does not establish physical relay contact state. No commands, automatic
toggles, persistence, or additional periodic state publications are introduced.

The service and GPIO handles remain resident across MQTT recovery. Reconnect
startup uses the latest snapshot without reapplying saved states. Switch startup
or read failures stop the run even with `recover=True`, increment `switch_errors`
separately from MQTT `failures`, and do not trigger network recovery. The summary
includes `switch_mode`, `switch_reads` (successful snapshots), `switch_errors`,
and `switch_cleanup_errors`. GPIO deinitialization is attempted on normal exit,
failure, and Ctrl-C; releasing outputs may change relay state. The firmware stop
helper suppresses individual deinit errors, so zero cleanup errors is not proof
that every pin was released. `cleanup_ok` continues to describe MQTT cleanup.

Use the same apparatus version for controls; vary only `switch_mode` while
keeping real sensor acquisition and the other controls constant. Use the same
fresh-start procedure because imported modules can remain resident. Capture
serial, `probe/#`, and broker TCP traffic throughout. Begin with a short run
showing `operational`, multiple fresh sensor publications, switch snapshots,
and fresh per-channel availability messages at the broker. Physical state is
an independent operator check. Host tests cover simulated GPIO and combined
sensor processing; CircuitPython stack/heap headroom and broker delivery still
require on-device validation. This mode does not reproduce command execution,
deferred command metadata, or the full app's memory footprint.

## First run

1. Operator copies `testApparatus/mqtt_soak_test.py` to CIRCUITPY root as
   `mqtt_soak_test.py`, alongside the existing firmware. No changes to `code.py`
   are required. Avoid host file writes during the run.
2. Start serial capture and the broker captures below. Stop Sensorius for the
   first comparison, leaving Mosquitto running on `.241` and Ethernet disabled.
3. Stop the app with Ctrl-C. Record whether the run follows a power-on, hard
   reset, or soft reload; these are different controls. Do not introduce an
   additional conditioning test before the soak.
4. At the REPL:

```python
import gc
gc.collect()
import mqtt_soak_test
result = mqtt_soak_test.run(duration_s=7200)
```

When kept in its repository directory on the device, import it with
`from testApparatus import mqtt_soak_test` instead. It does not import the old
`platform_test.py`.

The first on-device check must establish that startup reaches
`event=operational` without `kind=stack_limit`. A host pass cannot establish
that. If stack exhaustion occurs, stop interpreting that mode as a transport
test; retain its stage and packet evidence.

Ctrl-C attempts MQTT cleanup and returns control via `KeyboardInterrupt`.
An individual native socket call may still block beyond its requested timeout;
the apparatus cannot interrupt the known approximately ten-second native send
loop from Python. Duration and receive deadlines are checked between calls,
so the configured duration is not an absolute wall-clock kill timer.

## Broker capture

On the broker host, capture live messages in one terminal:

```sh
mosquitto_sub -h 10.0.0.241 -v -t 'probe/#' > mqtt-soak-messages.log
```

Use the existing broker's required authentication options if applicable. Keep
the subscriber running throughout one test session; distinguish any retained
snapshot delivered at subscriber startup from fresh test publications. Serial
elapsed time and the TCP capture provide operation timing.

In another broker-host terminal, capture the Nodus connection:

```sh
sudo tcpdump -i any -n -s 0 -U -w mqtt-soak.pcap 'host 10.0.0.225 and tcp port 1883'
```

Stop both captures after the test ends. Use a new capture filename for each
mode and record test start/end time, board, CircuitPython build, firmware and
library provenance, filesystem mode, broker interface, and Sensorius state.
The packet capture distinguishes full delivery, partial TCP writes,
retransmissions, PUBACK/SUBACK, and peer closure. Local byte counts are not
proof of broker delivery. Captures contain MQTT payloads and may contain MQTT
credentials; handle them as device diagnostic data.

## Compare modes one at a time

`mode="adapter"` is the default. Firmware sync and poll functions are called
directly from `run()`, avoiding the old platform dispatch/drain frames. It
preserves adapter behavior, including its existing EOF/idle-poll policy and
cleanup ownership mismatch. Normal publications log payload bytes, expected
wire bytes/QoS, packet ID where applicable, elapsed time, and adapter errors.
Firmware slow-chunk diagnostics supply native send timings when slow. The
apparatus deliberately does not wrap adapter sockets just to trace successful
chunks, since that would add active call frames and change socket capabilities.

For a separate controlled comparison:

```python
result = mqtt_soak_test.run(duration_s=7200, mode="flat")
```

Flat mode uses the same builders, startup priority selection, configured QoS
threshold, connection adapter, and cleanup, but substitutes shallow packet I/O.
It logs every send chunk, handles partial writes, validates PUBACK/SUBACK IDs,
and discards bounded interleaved inbound publications. Receive packets are
limited to 4096 bytes, interleaved ACK traffic to eight packets before the ACK,
and queues to 32 items. EOF is a failure, while only a timeout before the first
packet byte is an idle poll. ACK reads have a one-second deadline between
native calls. Flat subscriptions are processed individually rather than the
adapter's batch; flat mode also omits the adapter's clean-poll gate before
subscription. Flat mode stops immediately after a send chunk taking five
seconds, while the adapter may finish attempting the packet before reporting
a slow operation. These differences must be considered when interpreting a pass.

Do not infer a QoS-specific cause by comparing this with the old QoS 0 flat
probe: both modes here retain current startup QoS selection. Neither mode is a
complete application replica: full app coordination, command mutations, relay
command reports, NTP traffic, and full app heap pressure are absent. Sensor reads are
also absent in synthetic mode. Metadata
is built from runtime config without reading saved configuration snapshots via
`settings_root`. A clean standalone soak does not exonerate the full app.

For the configured production topic namespace explicitly use:

```python
result = mqtt_soak_test.run(duration_s=7200, base_topic=None)
```

This publishes synthetic retained metadata and status on the real device's
topics. Use it only as a deliberate comparison with consumers accounted for;
capture the configured base (normally `nodus/#`) instead of `probe/#`. Restart
the real app afterward to refresh actual retained state. The default diagnostic
namespace avoids replacing those retained device messages.

## Failure evidence and optional recovery

The first failure prints its stage, classification, error, operation count,
elapsed time, heap, and queue depths **before** cleanup or GC. No accumulating
history is kept on-device; serial and broker captures are the record. Expected
classifications include `stack_limit`, `send_stall`, `timeout`, `allocation`,
`socket_progress`, and `peer_closed`. An invalid ownership error is not called
native corruption.

Default behavior stops after preserving and closing the failed connection.
To investigate recovery in a separate run:

```python
result = mqtt_soak_test.run(duration_s=7200, recover=True)
```

After the first failure, it closes MQTT and rebuilds the adapter using the same
network. After a second failure, it cycles station/radio state through core
network recovery and rebuilds socket artifacts. A third failure stops the run.
Stack exhaustion always stops immediately. Each new clean session rebuilds
synthetic startup queues and all subscriptions; it does not replay commands.
`event=operational generation=...` marks completed startup/subscriptions, not
proven later broker delivery. Continued broker-visible traffic must validate
recovery. No reboot/NVM policy from the production app is exercised here.

The returned dictionary separates:

- `completed`: the requested time window elapsed rather than stopping early.
- `transport_ok`: completed with no operation failures and at least one
  operational generation. It is local evidence only, pending broker capture.
- `cleanup_ok` and `cleanup_errors`: independent shutdown results; successful
  traffic followed by `Socket not managed` cannot become an unqualified pass.
- Failure, operation, and generation counts, last failure kind, and elapsed time.
- `preflight_errors`: includes transient TCP startup errors even if the next
  bounded retry succeeds; a locally successful soak can have a nonzero count.

A recovered failure still produces `transport_ok=False`. Injected host-test
timeouts or delayed full-count sends verify detection and sequencing, not
reproduction of the initiating native transport fault. Do not shorten publish
intervals or add artificial heap pressure until the baseline is recorded.

## October 30 normal-app serial review

Read-only review through approximately 16:48 local time covered Pico2 W /
CircuitPython 9.2.8 captures `~/cu.usbmodem21233401.log` (CO2, `303.1`, RWFS)
and `~/cu.usbmodem21234201.log` (AVPD, `303.1` then `303.2`, ROFS then RWFS).
The operator identified an induced restart section; the supplied line range
`57130-37445` was interpreted as AVPD lines 57130-57445 and excluded. Explicit
KeyboardInterrupt sessions were not counted as spontaneous recovery failures.
Version labels do not independently establish deployed source/MPY equality.

- CO2 metadata PUBACK timeouts at 07:15:13, 09:00:19, 10:15:16 and 16:00:19
  each recovered without reboot in 8-10 seconds. An initial reconnect reported
  `Out of sockets`; the subsequent reconnect restored four device subscriptions
  and two switch subscriptions and reached the operational checkpoint.
- AVPD `303.2` at 14:26:59 reported a startup metadata PUBACK timeout, then
  ENOMEM at 14:27:00. Station reset and socket/client rebuild reached the
  operational checkpoint at 14:27:16 without reboot (lines 57474-57503).
- Approximately ten-second send stalls persist: CO2 `303.1` at 11:47:14 and
  15:18:11; AVPD `303.2` at 10:54:44, 13:14:21, 13:40:10 and 15:18:13.
  These entered existing native-poison hard-reset recovery; operational
  checkpoints followed in approximately 30-39 seconds. AVPD `303.1` also
  stalled at 06:52:56. Startup experiments and interrupted runs are separate
  from these steady-state observations and do not establish a failure rate.
- The near-simultaneous 15:18 stalls warrant correlation with broker/network
  evidence; a shared initiating event remains a hypothesis. `Socket not managed`
  remains subject to the ownership-mismatch caveat in the investigation handoff.
- At the snapshot end both devices reported idle recovery, MQTT connected and
  empty queues, with post-GC free heap about 49.7 KB CO2 / 48.9 KB AVPD.

No `mqtt_socket_recv_empty` or `mqtt_puback_unexpected` event was found in the
reviewed normal-app intervals. These captures demonstrate serial-side recovery
completion, but do not directly exercise the EOF correction or establish PUBACK
interleaving. Matching broker/TCP evidence was not reviewed; resumed delivery,
command handling and spontaneous-stall resolution remain unconfirmed. No device
writes or live commands were performed during this review.

## October 30 isolated PUBACK correction

Firmware `v0.26.303.2` adds only bounded PUBACK interleaving to the existing
`303.1` EOF correction. Raw-socket cleanup/manager ownership, app recovery
classification (including the pending `Out of sockets` gap), startup order,
QoS selection, send chunking and socket timeout setup/restoration are unchanged.

The PUBACK reader uses one receive/dispatch frame, replacing its nested
exact-read helpers. It accepts a matching PUBACK after at most eight PUBLISH
or PINGRESP packets. PUBLISH bodies are capped at 4096 bytes before allocation;
QoS 0/1 messages enter the existing inbound queue, and QoS 1 receives its protocol
ACK. Feature handlers, imports and persistence never execute inside this reader.
The one-second deadline is checked before/after native receives and between
packets, without per-byte timeout changes. It cannot preempt native receive or
send calls that overrun; a late PUBACK is not accepted after they return.

Resource effects: the common ACK path holds a one-byte scratch buffer, views
and a two-byte ACK body. An interleaved packet adds at most 4096 body bytes,
receive/decoding copies and its queued message. The outgoing metadata packet
remains resident until the wait returns. Up to eight additional messages can
be queued per wait; this is not a global transport queue bound. No new task,
persistent adapter state or imports are added. The larger reader adds resident
bytecode; Pico2 W heap/stack headroom remains a device validation requirement.
Using the existing `9.2.8-dirty` / MPY v6.3 compiler with identical source labels,
the compiled adapter grows from 30122 to 31402 bytes (**1280 bytes**). This
artifact-size difference is not a measurement of live heap or stack use.

Host fault cases first reproduced PUBLISH rejection on all three supported
receive API forms. Coverage includes ordered QoS 0/1 queueing, protocol ACK IDs,
partial reads, EOF, malformed/oversized packets, unsupported incoming QoS,
interleaving limits, absolute deadlines, and existing timeout restoration.
No new probe mode or unchanged long probe soak is proposed: earlier long runs
did not reproduce the full application failure.

Host verification for this candidate:

- `pytest -q tests/test_mqtt_client_adapter.py tests/test_command_intake.py tests/test_publish_cycle.py tests/test_mqtt_soak_apparatus.py tests/test_network_stack.py tests/test_recovery_policy.py tests/test_app_startup.py --tb=short --show-capture=no`:
  **426 passed**.
- `pytest -q tests --tb=short --show-capture=no`: **1164 passed**, with local
  socket access enabled for the HTTP probe test.
- Ruff checks on the changed runtime/test Python files and `git diff --check`:
  passed.
- `scripts/nodus_mpy.sh --target pico2w --circuitpython-version 9.2.8`:
  **72 modules compiled**, with matching `v0.26.303.2` build metadata.
- Source checks confirmed raw cleanup, SUBACK and shared receive helpers still
  match baseline `51d8294`; app recovery code was not edited.

After operator deployment on CircuitPython 9.2.8, capture serial, broker
subscriber output and broker TCP traffic in the normal combined application.
Confirm startup metadata, subscriptions and ongoing heartbeat/data/availability
before command tests. Use fresh IDs for harmless single-key config commands
during deferred retained metadata work. Require TCP evidence of an incoming
PUBLISH before the outgoing metadata's matching PUBACK; timing commands close
together alone does not establish interleaving. Correlate normal command
ACK/result/patch and deferred metadata, followed by two availability cycles.
For switch commands, use the separate operator-approved safe relay procedure
and check event/state, physical output and persistence independently.

Record exact artifacts, profile, filesystem mode, broker interface and cold
versus warm startup. Stop on missing confirmations, stack/memory errors or
recovery; do not change cleanup or reset policy to compensate. Hardware and
broker validation of this candidate remain pending. No live commands, relay
actions or device writes are performed by the agent.

## October 30 isolated EOF correction

The October 30 review inspected the latest ongoing real-sensor/real-switch runs
using apparatus `2026-10-28.2`, RWFS, broker `10.0.0.241`, `recover=False`:

| Device | Mode / reported firmware | Inspected elapsed time | Operations |
| --- | --- | --- | --- |
| CO2 `co2-pmoopn` | adapter / `v0.26.302.2` | 48749.969 s (13.54 h) | 7198 |
| AVPD `avpd-zbcalz` | flat / `v0.26.289.5` | 48729.609 s (13.54 h) | 7131 |

Neither inspected run contains a failure, error operation, slow-send record,
cleanup or final summary. These are ongoing intervals, not completed 14-hour
results. The operator's pasted subscriber excerpt contains heartbeat, sensor
and channel availability traffic for both devices; it does not establish every
packet's delivery over the entire interval. Exact deployed artifact equality
was not independently verified. The probe again did not reproduce the normal
application's failure; no further unchanged probe soak is proposed here.

With operator authorization to implement the handoff corrections one at a time,
`v0.26.303.1` changes only EOF classification in the adapter. Zero-byte raw
receive now fails the poll and marks the transport disconnected. Existing
recovery receives that failure. The raw cleanup mismatch and PUBACK reader
remain at the rollback baseline, including the current native-poison classifier
inputs. No new imports, buffers, call frames or background state are added.

The new host cases cover both receive APIs at header, remaining-length and
payload EOF, valid idle timeouts, and failure on timeout after a packet begins.
Before the fix, both initial EOF cases failed while the other 16 cases passed.
Hardware and broker validation of this isolated candidate remain pending.

Host verification for the isolated candidate:

- `pytest -q tests/test_mqtt_client_adapter.py tests/test_command_intake.py tests/test_publish_cycle.py tests/test_mqtt_soak_apparatus.py tests/test_network_stack.py tests/test_recovery_policy.py tests/test_app_startup.py --tb=short --show-capture=no`:
  **389 passed**.
- `pytest -q tests --tb=short --show-capture=no`: **1101 passed**, with local
  socket access enabled for the HTTP probe test.
- Ruff checks on the changed Python files and `git diff --check`: passed.

After operator deployment, validate the normal combined app from a recorded
cold start with the same configuration and `.241` broker. Require broker-visible
startup metadata, subscriptions, heartbeat/data and availability. In a separate
controlled peer-close case, correlate broker TCP FIN/EOF with serial
`mqtt_socket_recv_empty`, existing recovery and resumed broker-visible traffic.
Do not treat a timeout or successful local return as proof of EOF handling.
Continue normal-app observation for the spontaneous stalls; retain this one
change until its startup/recovery behavior has been assessed. No live relay
actions or device writes are performed by the agent.

## October 29 candidate withdrawal and rollback

The operator rejected candidate `v0.26.302.1` after reporting regressive behavior
and approved rollback. Firmware `v0.26.302.2` restores the MQTT adapter exactly
to repository baseline `51d8294` (`v0.26.289.5` behavior). EOF classification,
raw-socket cleanup, PUBACK reads and socket timeout handling all return to that
baseline. Candidate-only host tests and changed cleanup expectations were
withdrawn with the implementation.

CO2 serial capture reports candidate MQTT CONNECT success against `10.0.0.241`
at 15:21:39, a metadata PUBACK timeout at 15:21:44, and an approximately ten-second
two-byte DISCONNECT send at 15:21:56. The operator reports Ethernet removed and
only the broker Wi-Fi address `.241` available. AVPD was still running `289.5`
when it also encountered failures against `.241`; the initiating timeout cause
remains unconfirmed without matching broker TCP evidence and verified artifacts.

A host comparison using the existing soak socket/client fakes also confirmed
that candidate cleanup changed effective recovery: the same simulated stalled
publish plus baseline cleanup errors selects native-poison hard reset, while
candidate cleanup without those errors does not. Leaving the app classifier
unchanged did not preserve its outcome. Future cleanup changes require a
separate recovery proposal and operator approval.

The rollback restores a comparison baseline, not a proven native-stall fix.
Operator deployment and device/broker validation remain pending. Keep board,
CircuitPython, artifacts, profile, filesystem mode, broker interface, startup
method, workload and command pacing constant for baseline comparisons.

Rollback host verification: the focused adapter, command intake, publish cycle,
soak apparatus, network, recovery and app-startup suites passed (**371 tests**).
`pytest -q tests --tb=short --show-capture=no` passed (**1083 tests**) with local
socket access enabled for the HTTP probe. Ruff checks on the rollback Python
files and `git diff --check` passed. Adapter, test and contract-doc baseline
restoration was checked byte-for-byte against `51d8294`.

## October 27 filesystem and cleanup comparisons

Test version `2026-10-27.1` adds independent `cleanup_mode` and `fs_activity`
controls. Production firmware is unchanged. Use the same apparatus file on
`co2-pmoopn` and `avpd-zbcalz`. Run the comparison sequence on each device;
comparing CO2/ROFS directly against AVPD/RWFS changes multiple factors.

Before each run, record board identity, exact firmware/library artifacts,
CircuitPython version, filesystem mode, switch gate/channel count, broker,
consumer state, RTC synchronization, and startup method. Keep switches disabled
for the first comparisons. The start record includes configured sensor identity,
channel count, and experiment controls. The normal app must remain stopped.
Use the same startup method each time; Ctrl-C does not undo existing radio state.
Operator deployment and changing the physical filesystem guard remain manual.
No apparatus code writes to the mounted device from the host.

Start separate serial and broker captures for each device and run. Use the
broker capture commands above, substituting `.237` for CO2 and `.225` for AVPD
in the TCP filter. The existing serial captures end in `3401.log` for CO2 and
`4201.log` for AVPD. Broker messages on `probe/#` include each configured device
ID. Never run the normal app and apparatus concurrently with the same client ID.

### Short healthy-connection comparisons

Each call establishes a connection and exercises startup plus normal synthetic
traffic for three minutes. Broker capture must confirm traffic before cleanup;
`transport_ok=True` is only a local observation. Run one case per controlled
session, saving the result and captures before the next case.

```python
import mqtt_soak_test
# A: no writes, firmware cleanup (ROFS or RWFS).
result = mqtt_soak_test.run(duration_s=180)

# B: RWFS only, a diagnostic write immediately before firmware cleanup.
result = mqtt_soak_test.run(duration_s=180, fs_activity="before_close")

# C: no writes, bypass MiniMQTT's manager and close the raw socket directly.
result = mqtt_soak_test.run(duration_s=180, cleanup_mode="direct")
```

Hold filesystem mode constant when comparing A, B, and C on RWFS. Case A versus
C on ROFS is an additional no-write ownership control. Case C sends no MQTT
DISCONNECT: the broker may publish the retained offline Last Will. That is an
expected difference, and the direct-close mode is not a proposed runtime fix.
The adapter result can contain two manager ownership errors even when its
direct fallback closes the raw socket. Do not label that a transport failure.

### Overnight transport comparisons

Use adapter mode, default `recover=False`, and one 14-hour session per case:

```python
# 1: operator selects ROFS; no apparatus writes.
result = mqtt_soak_test.run(duration_s=50400)

# 2: operator selects RWFS; still no apparatus writes.
result = mqtt_soak_test.run(duration_s=50400)

# 3: RWFS; append a diagnostic record every five minutes after startup.
result = mqtt_soak_test.run(duration_s=50400, fs_activity="periodic")
```

For case 3, the production `append_recovery_event` writer is called with a
dedicated path and device label. It appends/closes a recovery-style record and
checks the size. Defaults allow 256 write attempts, five minutes apart, with a
65536-byte cap. They cover a 14-hour run without normally reaching the cap.
There is no explicit sync by default and no GC added around the write. This
models diagnostic file operations, not production write frequency or exact
full-app allocations. Writer imports are loaded before network setup; that
additional resident state is a comparison limitation. When the write limit is
reached, MQTT continues but no more records are written.

For independent later tests, add `write_sync=True` to request `os.sync()` after
each append, or set `write_max_bytes=4096` to exercise log trimming/rotation
after enough records. Do not change sync, trimming, and write cadence together.
Trimming uses the production helper, which can itself synchronize through its
transactional commit path. The cap bounds the live file; `.bak` and `.tmp` can
also occupy space during rotation.

Write-enabled runs require confirmed RWFS and refuse to start if any of
`_mqtt_soak_test.log`, `.log.tmp`, or `.log.bak` already exists. Preserve those
files after each run, then remove them manually before the next write-enabled
run. No automatic deletion or reuse of prior evidence occurs. Write-enabled
initialization checks the path but creates the file only on the first write.
Changing the guard requires the appropriate operator reboot; the apparatus
never remounts the filesystem.

### Failure ordering comparisons

Use these as separate RWFS sessions after the no-write baseline:

```python
# Send-stall ordering: preserve failure evidence, close, then write.
result = mqtt_soak_test.run(duration_s=50400, fs_activity="after_close")

# Poll-failure ordering: preserve failure evidence, write, then close.
result = mqtt_soak_test.run(duration_s=50400, fs_activity="before_close")
```

These options apply their ordering on either a transport failure or a normal
end-of-run close. A healthy ending therefore also produces a diagnostic write.
They deliberately test the selected ordering regardless of which failure kind
occurs; they do not implement the production recovery state machine. Stack
exhaustion skips the optional failure write. With `recover=True`, the selected
write occurs at each failed connection close within the existing recovery
budget; recovery is a separate comparison.

`failure_before_cleanup` and network diagnostics precede every selected
failure write or cleanup. Failure records identify connect/startup/steady stage,
poll versus publication/subscription work, and classification (`bad_descriptor`
for EBADF). `write_begin` / `write_end` include attempt number, phase, duration,
and errors, with heap/queues before and after. `post_write_cycle` marks the
first following locally successful MQTT loop; it does not prove publication
or broker delivery. Correlate subsequent operation records and packet capture.

The summary adds `write_count` (attempts), `write_errors`, `cleanup_mode`, and
`fs_activity`. Check all fields. A periodic write error stops the run and is
reported separately from transport failure count. An end-of-run write error
can coexist with `completed=True` and `transport_ok=True`; it invalidates the
filesystem comparison. A requested write mode with zero writes does not test
filesystem interaction. Existing cleanup ownership errors remain independent.

Host tests inject a delayed send and an EBADF result to verify ordering and
classification. There is no on-device fault-injection mode: injected host
failures do not reproduce the native initiating mechanism. Version
`2026-10-27.1` does not read real sensors or derive live metrics;
the October 28 opt-in sensor mode adds those operations. The default leaves relay GPIO inactive; `switch_mode="real"` initializes it.
No mode executes MQTT commands or reproduces full-app heap/stack pressure. A
successful synthetic run cannot exclude those mechanisms. On-device validation of this revision is
still required on both devices, including startup stack headroom and broker
delivery; the earlier two-hour result below belongs to the older revision.

## Host verification

```sh
python -m pytest tests/test_mqtt_soak_apparatus.py tests/test_mqtt_client_adapter.py tests/test_publish_cycle.py
python -m ruff check testApparatus/mqtt_soak_test.py tests/test_mqtt_soak_apparatus.py
git diff --check
```

On October 25, adapter mode on Pico 2 W / CircuitPython 9.2.8 completed a
7200.7-second run with one connection generation, 816 local publications,
and no operation failures. Broker capture confirmed regular steady-state
traffic after a known startup capture gap. Cleanup still reported ownership
errors. That run reports test version `2026-10-25.1`; `.2`'s errno 119 retry
change has host coverage but was not exercised by that device run. Flat mode,
on-device recovery, and XIAO ESP32-S3 remain unvalidated. See the
[dated result and comparison limits](../docs/debug-notes/mqtt-runtime-stall-2026-10-25.md#completed-two-hour-adapter-soak).
