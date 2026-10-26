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
web services, command handlers, or OTA processing, and does not write settings,
logs, NVM markers, or device files. It never requests an MCU reset or soft reload.
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
metrics are representative AVPD values. Pass `metrics={...}` to match an
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
