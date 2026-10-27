# Long-running MQTT soak apparatus

`mqtt_soak_test.py` investigates the October 25 failure in which Nodus ran for
roughly an hour without Sensorius, then stalled for approximately ten seconds
on a small heartbeat publication. It separates this runtime transport symptom
from the old `platform_test` harness's deep-call `pystack exhausted` failure.
See the [investigation record](../docs/debug-notes/mqtt-runtime-stall-2026-10-25.md).

## What runs

The normal application must be stopped. The apparatus reads existing settings
and uses core network ownership, the firmware MQTT adapter, and firmware
payload/subscription builders. It does not start sensor drivers, relay GPIO,
web services, command handlers, or OTA processing. The default writes no files.
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

The snapshots are synthetic and do not confirm physical switch state. Default
metrics are representative AVPD values, with `CO2` replacing `Pressure` for
a configured CO2 sensor. These are eight fixed synthetic values, not measured
values or executions of the sensor derivation code. Pass `metrics={...}` to match an
observed sensor payload more closely; packet sizes are logged. RTC time is used
as-is; no NTP sync is attempted. Record whether RTC was already synchronized,
because timestamp width can affect payload sizes. Firmware version and test
version are both printed. Unlike the old harness, metadata uses the firmware
version rather than the probe version.

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
complete application replica: feature tasks, sensor reads, command mutations,
real relay reports, NTP traffic, and full app heap pressure are absent. Metadata
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
failures do not reproduce the native initiating mechanism. No mode reads real
CO2/BME280 sensors, derives live metrics, operates relay GPIO, executes MQTT
commands, or reproduces full-app heap/stack pressure. A successful synthetic
run cannot exclude those mechanisms. On-device validation of this revision is
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
