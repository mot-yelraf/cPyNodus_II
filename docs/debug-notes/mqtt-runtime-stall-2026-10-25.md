# AVPD Runtime MQTT Stall and Platform Probe Review - 2026-10-25

## Scope and evidence

Device: `avpd-zbcalz`, Pico 2 W, CircuitPython `9.2.8`, firmware reporting
`v0.26.289.5`, BME280/AVPD plus one switch channel `S1-zbcalz`.
Broker: `sensorius-hub-1.local`, initially including Ethernet `10.0.0.242`
and Wi-Fi `10.0.0.241`; the later operator-controlled run used Wi-Fi only.
The Nodus station address was `10.0.0.225`.

Evidence reviewed:

- `~/cu.usbmodem21234201.log`, initially lines 2111-10158, with follow-up
  review through line 10620 (`2026-10-25 16:51:06`). Line numbers refer to
  that captured file and may change if the capture is rotated.
- Operator-provided broker captures for the platform tests at 16:33-16:37.
- Current network, MQTT adapter, app recovery, and platform-probe source.
- Matching local Adafruit bundle Python source and the local CircuitPython
  repository's `9.2.8` TCP-send implementation. Deployed library/source
  equality was not independently verified; a firmware version string alone
  does not prove artifact equality.

This investigation made no runtime changes or device-volume writes. The
operator performed deployment, testing, and the reported complete reflash.
The approximately weekly early-morning Xfinity Wi-Fi outage was identified by
the operator as expected router maintenance, separate from this investigation.

## Runtime failure without Sensorius: roughly an hour before heartbeat stall

The operator stopped Sensorius on the Pi while leaving the MQTT broker running
on `.241`, then allowed Nodus to run. The operator described the interval as
approximately 58 minutes. Serial provides these more precise MQTT checkpoints:

| Time | Observation |
| --- | --- |
| 15:05:40 | MQTT connects to `.241`. |
| 15:05:51 | Device and switch subscriptions drain; generation 1 is operational. |
| 15:10-16:00 | Health reports ready Wi-Fi and connected MQTT, with periodic GC. |
| 16:01:54 | Retained heartbeat publish stalls, then recovery requests hard reset. |
| 16:02:19 | MQTT connects after reboot. |
| 16:02:29 | First device `config/set` subscription times out waiting for SUBACK. |
| 16:02:30 | MQTT rebuild encounters `ENOMEM`; recovery selects station reset. |
| 16:02:38 | MQTT connects after station reset. |
| 16:02:42 | Generation 2 is operational. |
| 16:21:58 | Last health check in that review remains normal, queues empty. |

The failure was logged 56 minutes 3 seconds after the operational checkpoint,
or 56 minutes 14 seconds after MQTT CONNECT. It was a steady-state runtime
failure, not just an initial startup failure:

```text
16:01:54 mqtt send_chunk_slow packet_bytes=137 chunk_bytes=137
    bytes_returned=137 elapsed_ms=9970
16:01:54 mqtt sync phase=error op=publish
    topic=nodus/avpd-zbcalz/status/heartbeat bytes=98
    elapsed_ms=9978 errors=mqtt_publish_slow:...
16:01:54 recovery mqtt close errors=mqtt_close_failed:Socket not managed,
    mqtt_socket_close_failed:Socket not managed
16:01:54 runtime action=reset reason=recovery:mqtt_native_socket_poison
```

Post-GC Python free heap stayed around 50.5-51 KB through most of this run,
with 48.1 KB at 16:00:19. No progressive Python-heap exhaustion or Wi-Fi loss
was logged before the stall. This does not establish native TCP-buffer
availability, continuous end-to-end connectivity, or broker delivery.

Active Sensorius execution is not necessary to reproduce this failure under
the operator's reported conditions. The experiment does not rule out an effect
on failure frequency, retained broker messages, or other broker/network causes.
The broker capture supplied later covers the short platform tests, not the
16:01:54 heartbeat; the heartbeat's delivery remains unconfirmed.

## Platform tests: distinguish stack exhaustion from transport failure

The operator copied the repository's root-importable `platform_test.py`
(`PLATFORM_TEST_VERSION = "v0.26.149.10"`) and ran:

```python
import gc
gc.collect()
import platform_test
platform_test.run(mqtt_mode="app_startup", mqtt_rounds=1, scans=0,
                  reset_station=False, sync_ntp=False)
```

All three initial runs connected successfully and sent the first 1402-byte
QoS 1 metadata packet in 2-3 ms, then failed reading the PUBACK header with
`mqtt_puback_stage=header:pystack exhausted`. Broker captures at 16:33:14,
16:34:57, and 16:35:12 confirm that each metadata publication arrived despite
serial reporting `published=0`. These captures do not show whether PUBACK
reached the device.

The probe adds the following nested calls ahead of the adapter's receive
helpers:

```text
run -> _mqtt_platform_round_trip -> _mqtt_app_startup_sequence_probe
    -> _drain_app_startup_mqtt_work -> sync_transport_to_client
    -> _publish_mqtt -> _publish_mqtt_qos1_verified
    -> _read_mqtt_puback -> _recv_mqtt_byte
    -> _recv_socket_exact -> _recv_socket_bytes -> recv_into
```

Treat this repeatable exhaustion as a limitation of this deep probe path on
the tested configuration, not evidence that the broker failed or that the
production ten-second send stall is a Python-stack failure. It is not an
intentional passing test outcome. Existing May investigation notes already
distinguish standalone adapter-probe stack pressure from normal app operation.

The operator then ran `app_subscribe_probe` three times with the same other
arguments. This mode uses a shallower path, sends retained publications at
QoS 0, and validates the first device subscription's SUBACK:

| Broker capture time | Publications delivered | SUBACK elapsed |
| --- | --- | --- |
| 16:37:24 | `meta`, heartbeat, availability, `meta/switch` | 57 ms |
| 16:37:37 | Same four topics | 40 ms |
| 16:37:53 | Same four topics | 46 ms |

All three returned `True`, with 12 broker-visible publications in total and no
stack exhaustion or ten-second stall. All three still logged both
`Socket not managed` cleanup errors. The probe's pass result therefore does
not establish clean shutdown. Only the first device subscription was checked;
these tests did not validate all command subscriptions, command execution,
physical relay state, QoS 1 PUBACK handling, or long-duration stability.
Both stack depth and QoS differ between the two modes, so their comparison
does not isolate QoS as the cause.

Probe metadata intentionally uses stubbed snapshots and the probe version.
Its empty IPv4 metadata field and year-2000 timestamps with NTP disabled do
not establish loss of the live station address or a firmware downgrade.

## Post-reflash observations

After the operator reported a complete reflash, another `app_startup` run
still failed at the PUBACK header with `pystack exhausted`. It had about
211 KB free heap before queueing and sent the packet in 3 ms. This supports
the stack-depth interpretation rather than ordinary heap exhaustion.

The subsequent normal application run also reproduced transport trouble:

- A soft reboot led to logged startup conditioning and an ROFS boot reporting
  `mqtt_warm_attempts=255`. This was not a pristine cold-start comparison.
  The marker value's origin and effect were not established in this review.
- At 16:49:56, 16:50:06, and 16:50:16, the first three 256-byte chunks of a
  1412-byte startup `/meta` packet each returned their full byte count after
  approximately ten seconds. The capture then jumps to another boot without
  a recorded reset reason; do not infer the reset mechanism.
- At 16:50:53 on the next boot, a 581-byte QoS 1 `meta/switch` packet timed
  out waiting for the PUBACK header. Cleanup reported ownership errors, the
  next reconnect encountered `ENOMEM`, and a station reset restored service.
- At 16:51:05, subscriptions drained and MQTT became operational. No
  `pystack exhausted` was reported in these normal-runtime failure sequences.

A full reflash is therefore not sufficient remediation for the currently
observed symptoms. The old oversized-startup-packet explanation also cannot
explain them all: 1412-byte and 581-byte packets are below the historical
1460-byte boundary, and the earlier steady-state heartbeat was only 137 bytes.

## First standalone soak attempt: TCP progress retry

At approximately 17:10, `mqtt_soak_test` version `2026-10-25.1` joined Wi-Fi
but its first TCP preflight returned errno 119 (`EINPROGRESS`). It stopped at
7.5 seconds with zero MQTT connection generations and zero MQTT operations;
cleanup was clean. This was neither the hour-delayed publish stall nor stack
exhaustion. The operator confirmed that errno 119 commonly needs a retry.

The first apparatus implementation used a strict single preflight attempt,
unlike production's bounded retry. Test version `2026-10-25.2` corrects that
mismatch: up to three attempts for socket-progress errors, 0.5 seconds apart,
with every error logged and counted. Other preflight errors and publication
failures remain stopping conditions by default. This is an apparatus-only
change, not a firmware network or recovery-policy change. A new on-device
attempt is needed to establish whether the retry reaches the soak phase.

## Completed two-hour adapter soak

The next standalone run completed in 7200.7 seconds on the same Pico 2 W,
firmware reporting `v0.26.289.5`, ROFS, broker `.241`, and the diagnostic
`probe` topic namespace. The start record reports apparatus `2026-10-25.1`,
not `.2`; TCP preflight succeeded on its first attempt, so this run did not
exercise or validate the new errno 119 retry behavior.

Serial evidence from line 10675 onward:

- One connection generation and one operational generation; no operation
  failures, reconnects, stack exhaustion, or slow-send records.
- 818 sync operations: 816 publications and two subscription batches covering
  four device topics and one switch topic.
- 232 heartbeat publications, 232 sensor-availability publications, 231
  switch-availability publications, 118 synthetic sensor-data publications,
  and one publication each of `meta`, `meta/switch`, and `meta/config`.
- The largest logged operation duration was 119 ms. Startup metadata packets
  of 1412 and 581 bytes succeeded through the adapter, as did repeated
  137-byte heartbeats, matching sizes seen in failing normal-runtime records.
- After-GC free heap stayed between 155872 and 156256 bytes throughout the
  measured periodic collections. This is materially more headroom than the
  approximately 48-51 KB in the earlier failing full-app run.
- Final cleanup again logged both `Socket not managed` errors. The returned
  summary correctly separated `transport_ok=True` from `cleanup_ok=False`.

Broker evidence: `~/mqtt-soak-messages.log`, 806 messages captured from
17:15:10 through 19:12:49 local time. Capture began after startup because the
initial subscriber selected `nodus/#` rather than `probe/#`. The three startup
metadata messages received at subscriber startup are retained snapshots and
must not be counted as live-captured startup sends. The remaining capture
contains 229 heartbeats, 229 sensor-availability messages, 229
switch-availability messages, and 116 sensor-data messages. Payload timestamps
show a maximum gap of 32 seconds within each heartbeat/availability stream
and 61 seconds within the data stream. No broker-visible publication gap was
identified in that captured steady-state interval. Initial missing messages
are consistent with the known capture gap; full startup delivery was not
independently captured live. No TCP packet capture was inspected for this run.

This is a broker-supported successful standalone adapter baseline beyond the
previous roughly hour-delayed failure window. It does not establish that the
full application is stable or rule out earlier switch-command work. Synthetic
sensor data, substantially higher heap headroom, absent sensor/GPIO services
and command execution, omitted NTP traffic, diagnostic topics, and a shallower
runtime call path remain material differences. The cleanup error also persists
after a sustained successful connection. A further comparison should isolate
one of these differences rather than treating this soak as a transport fix or
repeating the same baseline without a new hypothesis.

## October 26: normal-app sensor-only overnight control

The operator disabled the S1 enable gate and ran the normal application, not
the synthetic soak. Review of serial lines 12390-12681 confirms:

- October 25 at 19:22:24: firmware `v0.26.289.5`, profile `sensorius`, ROFS,
  BME280 at `0x76` active with eight metrics, `switch enabled=False channels=0`,
  switch adapter/service inactive, and no channel IDs.
- The run followed warm-start cleanup and logged startup conditioning. It is
  not a cold-start control. Broker remained `.241`, station address `.225`.
- MQTT became operational at 19:22:33. Through October 26 at 06:12:18,
  10 hours 49 minutes 45 seconds later, there was one boot, one operational
  generation, 130 health records in recovery phase `idle`, and no logged MQTT
  errors, slow sends, allocation failures, stack exhaustion, reconnects,
  recovery resets, or tracebacks in this interval.
- Periodic post-GC free heap stayed between 69920 and 70608 bytes. This is a
  measured configuration difference, not evidence that the switch-enabled
  configuration exhausted Python heap.

No broker capture for this overnight sensor-only interval was available in the
reviewed local files. This establishes serial-side continuity; it does not
independently establish delivery of every overnight publication. The last
available `mqtt-soak-messages.log` covers the earlier diagnostic-topic soak.

The sensor-only result repeats a recorded discriminator rather than revealing
a new generic Pico2 W problem. Removing switch capability simultaneously
removes GPIO/service initialization, switch metadata, channel subscriptions,
availability/state work, and switch command/deferred reporting paths. It does
not isolate which of these initiates failure or prove that a toggle is required.
The switch-enabled normal app had already failed in ROFS after reflash, so
filesystem writability alone does not account for the contrast. Deployed
artifact equality beyond the reported version remains unverified.

### Prior switch investigations and resolution limits

These records must guide the next investigation rather than restarting from
generic broker reachability, free-heap, or reflash hypotheses:

| Record | Established result and limit |
| --- | --- |
| [May 8 switch-shape experiments](mqtt-recovery-2026-05-07.md) | Sensor-only startup succeeded while even one switch changed the failing path. Deferring switch subscriptions and suppressing switch startup state/availability did not by themselves resolve it. Compacting embedded channel metadata improved broker visibility, but repeated trials still exposed startup sensitivity. |
| [May 13 metadata split](meta-switch-split-2026-05-13.md) | Detailed channel metadata moved from main `meta` to `meta/switch`, with identity/liveness publishing and subscription sequencing adjusted. These are implemented changes, not a reason to assume every switch failure was permanently resolved. |
| [May 30 AVPD plus S1 comparison](avpd-bme280-mqtt-startup-2026-05-29.md) | The same sensor/broker combination worked without S1 and failed with S1. Extra GC, changing BME280 driver variants, and longer SUBACK timeout did not resolve that case. Packet capture located an earlier incomplete retained publish, so later SUBACK failure was not the initiating operation. |
| [May 30 compact-packet fix and May 31 rollback](avpd-bme280-mqtt-startup-2026-05-29.md) | Reducing a 1464-byte startup packet to 1443 bytes had matching broker/PUBACK validation. Later soak regressions led to a narrow rollback toward `148.1` QoS 0 steady-state behavior; PUBACK/chunking changes were not accepted as a broad steady-state cure. Today's small heartbeat stalls remain outside that size-only explanation. |
| [May 11 churn correction](mqtt-recovery-2026-05-07.md) | Slow operations followed by disconnect/rebuild/startup replay were recognized as a feedback loop. `131.1` changed queue drain and slow-success handling. This is historical evidence about recovery amplification, not authorization to restore old policy without current broker evidence. |
| [October 16 toggle correction and acceptance](../../testApparatus/switch_sync_validation.md#pico2-w-toggle-regression-comparison-2026-10-16) | `289.5` removed per-command channel/switch/runtime-config reconstruction and guarded the lazy helper import. Four alternating ON/OFF commands had correlated broker ACK/result/event/state/patch and persistence confirmation. The operator accepted toggle behavior; the record explicitly leaves the initiating native fault and long-soak stability unconfirmed. |

The October 16 record resides in `testApparatus/switch_sync_validation.md`,
not only in `docs/debug-notes/`. It is essential continuity for this issue.
That accepted command-path correction must not be described as absent, nor
overstated as proof that the later switch-enabled runtime stall was fixed.
The earlier May notes also document long-working switch-enabled baselines and
subsequent regressions; the operator has approximately a dozen operating Pico2 W
Nodus devices. Avoid generalizing the current failure to all such devices.

Current interpretation: the failing full-app switch-enabled configuration,
successful two-hour synthetic adapter soak, and successful overnight sensor-only
normal app are distinct controls. Together they keep switch-enabled runtime
work and its downstream effects central to the investigation, with the exact
initiating mechanism still unconfirmed. A heartbeat or availability timeout
identifies where failure became visible; it does not exonerate earlier work.
No runtime behavior changes were made during this review.

## Source findings and remaining uncertainty

The raw-connect adapter creates a socket directly and assigns it to MiniMQTT
without registering it with the Adafruit connection manager. Cleanup calls
MiniMQTT's manager-based close methods, which reject an unregistered socket
with `Socket not managed`. A host reproduction using the matching bundle's
close methods produced both errors on a healthy simulated socket, although
direct fallback successfully closed it. Today's successful on-device
publish/SUBACK probes independently show the errors without a stalled send.

Consequently, the current recovery classification of slow publish plus
`Socket not managed` as native socket poison is not reliable proof of native
corruption. This ownership mismatch does not establish what initiates a stall.

The local CircuitPython `9.2.8` TCP-send implementation contains a native
`tcp_write()` memory-error retry loop of up to 200 iterations with 50 ms
delays. Its `tcp_output()` result can overwrite the failed-write result and
allow a byte-count return after exhausted write retries. This is a source-level
mechanism consistent with the approximately ten-second false-success pattern,
not proof that this branch executed on the device or identification of the
original resource-pressure trigger.

Historical PUBACK experiments and rollback must be considered before further
transport changes. Verification was disabled by default in `v0.26.151.2`,
then deliberately re-enabled for retained metadata by commit `0f5fa0b` on
July 28 (`MQTT_RAW_VERIFY_PUBLISH_BYTES = 1`), which remains in current source.
Do not treat repeating that experiment as a new mechanism-isolation result.

The next useful evidence is broker-side TCP capture aligned with an actual
production stall, distinguishing complete MQTT delivery, acknowledgements,
retransmissions, and connection closure. Keep cold versus soft startup,
filesystem mode, artifact provenance, and Sensorius state explicit. Do not
change timeouts, startup order, QoS, or reset policy without a separate
evidence-based proposal and operator approval.

## Verification and related history

Earlier in this investigation, 224 host tests passed across
`test_network_stack.py`, `test_recovery_policy.py`, `test_app_startup.py`, and
`test_mqtt_client_adapter.py`; the healthy-socket cleanup reproduction also
passed. Those checks do not establish CircuitPython stack headroom, native
socket health, or broker delivery. The initial review's 29 explicit recovery
restarts and the later 16:01:54 hard reset precede the post-reflash observations
and must not be pooled as one controlled experiment.

- [Adapter stack-depth history](mqtt-merge-redo-plan-2026-05-20.md)
- [AVPD startup, packet captures, PUBACK experiments, and rollback](avpd-bme280-mqtt-startup-2026-05-29.md)
- [Earlier cleanup errors during a successful recovery](mpy-calibration-heap-2026-06-03.md)
- [Current publish-stall troubleshooting](../mqtt.md#mqtt-publish-stall-with-false-local-success)
