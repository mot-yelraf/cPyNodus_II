# MQTT Socket Investigation and Implementation Handoff — 2026-10-29

## Purpose and status

Handoff for the VS Code Codex agent implementing narrowly scoped MQTT adapter
corrections. This note records observations, source findings, candidate changes,
and validation requirements. It is archival investigation context, not a new
runtime contract or proof of a root cause.

The operator requested this note after a read-only investigation. No runtime
code, configuration, or device files were changed during that investigation.
Writing this note does not select a recovery-policy change. Apply the repository
AGENTS.md investigation/proposal/approval rules before editing stability-sensitive
runtime code, accounting for any subsequent explicit operator authorization.

Repository HEAD reviewed: `51d8294` (merge of socket-stability apparatus work).
Working tree was clean before this documentation addition. Current source reports
`v0.26.289.5`; deployed source/MPY/library equality was not independently verified.
A version string alone is insufficient artifact provenance.

## Evidence and current contract

Serial captures reviewed on October 29, through approximately 13:29 local time:

- `/Users/twfarley/cu.usbmodem21233401.log`: CO2 device `co2-pmoopn`, Pico2 W,
  CircuitPython 9.2.8, station address `10.0.0.237`.
- `/Users/twfarley/cu.usbmodem21234201.log`: AVPD/BME280 device `avpd-zbcalz`,
  Pico2 W, CircuitPython 9.2.8, station address `10.0.0.225`.

The files are active captures; counts and line references below describe the
inspected snapshot and may change after rotation or further testing.

Contract docs reviewed: `docs/README.md`, `docs/architecture.md`,
`docs/configuration.md`, `docs/sensorius_contract.md`, and `docs/mqtt.md`.

- Normal MQTT profiles are headless; web listener absence is intentional.
- Network/socket lifecycle belongs in core networking, the MQTT adapter, and
  app-owned recovery, not feature services.
- MQTT CONNECT alone is not operational success: startup publications and
  command subscriptions must drain for the generation to become operational.
- Current diagnostic metadata publications use QoS 1; ordinary heartbeat,
  availability, and sensor publications use the existing QoS 0 path.
- Broker-visible output is required for delivery validation. Serial success,
  empty queues, and host tests cannot establish MQTT delivery or native health.

Read these related records before choosing an implementation:

- [October runtime stall and controls](mqtt-runtime-stall-2026-10-25.md).
- [May recovery regressions and stable baseline](mqtt-recovery-2026-05-07.md),
  particularly the May 11 `125.7` versus `130.1` comparison and churn findings.
- [AVPD startup packet captures and rollback](avpd-bme280-mqtt-startup-2026-05-29.md).
- [Lazy-command memory and stack findings](command-lazy-memory-2026-06-01.md).
- [Accepted October 16 toggle correction](../../testApparatus/switch_sync_validation.md#pico2-w-toggle-regression-comparison-2026-10-16).
- [Current soak apparatus](../../testApparatus/mqtt_soak_test.md).

## Serial observations

The inspected CO2 capture contains 21 steady-state `mqtt_publish_slow` events
followed by `mqtt_native_socket_poison` hard resets; AVPD contains 36. These are
whole-capture event counts across different runs, not a controlled failure rate.
Both devices exhibit approximately 9.97-second sends of small heartbeat or
availability packets, with the socket returning the full byte count.

Examples:

- CO2, October 29 13:27:46, lines 6605–6610: a 134-byte availability packet
  returns 134 after 9967 ms; adapter reports slow publish and requests reset.
- AVPD, October 28 15:39:21, lines 32120–32126: a 137-byte heartbeat packet
  returns 137 after 9964 ms; the same reset sequence follows.
- CO2 also fails under reported `288.1`, and failures occur in ROFS and RWFS.
  Neither the latest toggle correction nor filesystem writes alone explain them.
- CO2 calibration commands at October 26 08:48:26 and 08:48:32 report
  `calibration_offset_persist_pystack`; a heartbeat stalls at 08:49:50.
  Preserve this correlation, but later fresh-boot failures do not require
  another logged command. Absence of a serial command record does not replace
  broker capture of inbound traffic.

The operator clarified in the prior chat that reconnecting broker-host Ethernet
triggered the shared October 29 08:09 event. AVPD stalls at 08:09:04; CO2 reports
`EBADF` at 08:09:07, then broker resolution changes from `.241` to `.242` during
recovery. Analyze this as an interface-transition/recovery case, separately from
the spontaneous stalls. Do not use it to explain all earlier failures.

Useful controls, with their limits:

| Run | Serial result | Limit |
| --- | --- | --- |
| Normal CO2, real sensor, switches disabled | Approximately 13 hours without logged MQTT failure; post-GC heap about 74 KB | Different resident footprint and workload; no matching broker validation reviewed here |
| Normal CO2, two switches, sensor disabled | Approximately 11.5 hours without logged MQTT failure; post-GC heap about 75–77 KB | Does not exercise combined sensor/switch work |
| Synthetic AVPD adapter soak | Completed 14 hours, 4083 operations, no transport failures | Synthetic data, no command execution or NTP; cleanup errors remain |
| Real AVPD sensor and two real switch services, apparatus `2026-10-28.2` | 12803 seconds (about 3h33m), 1891 operations, 212 sensor reads; operator interrupted | No command execution, NTP, or full-app coordinator; diagnostic topics; post-GC heap about 137 KB |
| Combined normal application | Some runs survive more than 12 hours before failure | A short passing candidate run cannot establish stability |

Real switch apparatus mode initializes outputs at saved states and reads GPIO;
it does not exercise commands or independently verify physical relay contacts.
The real-service run ends around AVPD line 31954. Earlier `switch_mode` calls
failed because imported apparatus version `.1` lacked the argument; the measured
real-service run explicitly reports `.2`. Do not count the failed calls as tests.

Latest intervals above are serial-side observations. This review did not inspect
matching broker captures for them. The older October 25 note separately records
broker-supported continuity for its two-hour synthetic soak.

## Source findings, ordered by evidential role

### Native send behavior matches the ten-second signature

The local CircuitPython checkout is
`/Users/twfarley/Projects/mcu_libs/circuitpython`. Inspection used
`git show 9.2.8:<path>` to avoid assuming checkout HEAD matches the device runtime.

In `ports/raspberrypi/common-hal/socketpool/Socket.c`, `lwip_tcp_send()`:

1. `tcp_write()` can return `ERR_MEM` despite available TCP send-buffer space.
2. A loop retries up to 200 times with 50 ms delays.
3. `tcp_output()` overwrites the same `err` variable after a failed write.
4. If retries exhaust with the output result `ERR_OK`, the function can return
   `write_len` although the write did not successfully enqueue those bytes.

This is a concrete source-level mechanism consistent with the repeated ten-second
full-byte-count returns. It is not proof that this branch executed on the board,
nor identification of the initiating resource or network condition.

The same tag's `ports/raspberrypi/lwip_src/lwip_mem.c` routes network allocations
through `port_malloc()`, backed by TLSF in `ports/raspberrypi/supervisor/port.c`.
Python `gc.mem_free()` is not a measurement of native allocator availability.
Neither a stable Python heap nor a low Python heap proves native transmit health
or exhaustion. Do not propose a CircuitPython/library patch as an already
validated firmware fix; that would be a separate build and experiment.

### Definite adapter defect: orderly EOF is classified as an idle poll

File: `cpynodus_ii/core/mqtt_client.py`.

- `_poll_mqtt_socket_once()` raises `mqtt_socket_recv_empty` on zero-byte receive.
- `_is_mqtt_poll_no_data_error()` explicitly returns true for that marker.
- `_poll_mqtt_client_socket()` consequently records loop success and leaves the
  transport connected after an orderly closure.

A host fake whose `recv_into()` returns zero reproduced a poll return of zero
with `transport.connected=True`. On the inspected CircuitPython native TCP
receive implementation, an empty receive can represent peer closure; idle reads
are represented by timeout/would-block errors.

Candidate correction: distinguish EOF from idle timeout at the adapter boundary.
Cover both recv and recv_into paths and ensure failure reaches existing recovery.
This is a plausible way to conceal a dead session until a later send, but no
zero-byte-read telemetry ties it to a particular captured spontaneous stall.

### Definite ownership mismatch and unreliable reset classification

Files: `cpynodus_ii/core/mqtt_client.py`, `cpynodus_ii/app.py`.

- `_connect_mqtt_client_raw()` obtains a direct pool socket.
- `_adopt_raw_mqtt_socket()` assigns it to MiniMQTT without manager registration.
- MiniMQTT disconnect/close uses its connection manager; matching local bundle
  source raises `Socket not managed` for unregistered sockets.
- `_force_close_mqtt_client_socket()` has a direct-close fallback. The mismatch
  therefore does not by itself establish a leaked socket.
- `_mqtt_native_socket_poison_detected()` combines a slow publish with
  `Socket not managed` or `Out of sockets` to request a hard reset.

Successful on-device apparatus runs still report two manager cleanup errors;
short direct-cleanup runs report none. A host reproduction confirms that the
manager registration error plus a slow publish selects native-poison recovery.
That error cannot reliably establish native corruption.

Candidate correction: make ownership and cleanup consistent for directly adopted
sockets. Specify how ownership is tracked without retaining extra long-lived
state unnecessarily. Avoid blindly registering private manager internals or
changing TLS/managed-socket cleanup. Review reset classification separately:
correcting the misleading signal does not prove that a hard reset is unnecessary
after every real native stall. A reboot-depth change needs its own explicit
proposal, approval, and hardware/broker validation.

### PUBACK reader cannot accommodate interleaved incoming traffic

File: `cpynodus_ii/core/mqtt_client.py`, `_read_mqtt_puback()`.

The next packet must be `0x40`; an incoming PUBLISH raises
`mqtt_puback_unexpected:30`. The SUBACK path already handles bounded interleaved
packets, but the PUBACK path does not. Once subscribed, incoming commands can
interleave with deferred QoS 1 metadata refreshes.

A host fake returning a PUBLISH header reproduced
`mqtt_puback_stage=header:mqtt_puback_unexpected:30`. The latest inspected failure
records primarily show PUBACK timeouts rather than this error, so this is a
protocol robustness gap, not an established explanation of those timeouts.

Candidate correction: bounded packet dispatch while awaiting the matching PUBACK,
with an absolute wait bound and explicit handling of packet IDs and incoming
QoS. Queue commands for normal shallow dispatch; do not execute feature command
handlers inside the ACK reader. Avoid helper extraction that deepens the Pico2 W
call stack or allocates large packet buffers. Preserve existing QoS and payloads.

## Implementation sequence and approval boundary

Before runtime edits, run `git status --short`, reread relevant contract docs,
and present exact files, intended behavior, MQTT-visible differences, resource
and timing effects, and validation plan. Resolve approval using the current
operator conversation; this handoff is not blanket authorization to alter
startup, timeouts, QoS, reconnect policy, or reset depth.

Recommended order is independently reviewable changes, one mechanism at a time:

1. Add host fault cases and, where appropriate, a shallow apparatus mode that
   demonstrates EOF and interleaved PUBACK behavior without automatic relay actions.
2. Correct EOF classification in the adapter, subject to runtime-edit approval.
3. Correct raw-socket cleanup ownership; evaluate reset classification separately.
4. Address bounded PUBACK interleaving if its stack/heap budget can be justified.
5. Continue production-stall investigation with broker TCP capture rather than
   declaring any of these changes a proven cure for native send stalls.

Expected external differences must be explicit: EOF correction detects closure
earlier and invokes existing recovery; cleanup correction removes spurious
manager errors; PUBACK correction avoids reconnect solely because valid incoming
traffic precedes an ACK. None requires new public config keys, topic shapes, or
publication QoS. Do not bundle unrelated refactors into these corrections.

Current `289.5` already removes per-command reconstruction of switch/channel/
runtime config and guards the lazy switch-helper import. Four alternating
commands received broker-visible confirmation on October 16. Preserve that
accepted correction; it neither identifies the native cause nor validates long
soaks. Include deferred metadata after command replies in any command-path review.

Do not repeat longer SUBACK timeouts, extra GC, driver substitutions, reflashing,
startup conditioning, broad packet chunking, or QoS changes as new isolated
solutions. Prior notes record their limits and regressions. Historical `125.7`
has a documented approximately 39h45m stable switch-enabled baseline; use an
artifact-controlled comparison where available, not a version-label comparison.

## Verification and delivery requirements

Investigation verification completed on October 29:

- `pytest -q tests/test_mqtt_client_adapter.py tests/test_app_startup.py`:
  **175 passed** on unchanged runtime source.
- Three inline host reproductions confirmed EOF concealment, PUBLISH rejection
  by the PUBACK reader, and manager-error native-poison classification.
- No new on-device routines, live MQTT commands, or relay actions were performed.

For implementation, add focused behavioral tests for each changed mechanism,
including managed/raw cleanup and valid idle timeouts. Run the applicable suites:

```text
pytest tests/test_mqtt_client_adapter.py tests/test_command_intake.py tests/test_publish_cycle.py
pytest tests/test_network_stack.py tests/test_recovery_policy.py tests/test_app_startup.py
pytest tests
git diff --check
```

Host tests cannot establish CircuitPython heap/stack headroom, native allocator
health, socket recovery, or broker delivery. Keep hardware results separate.
Update firmware version once for runtime behavior changes per AGENTS.md; do not
bump it for this note or test-only/apparatus-only work.

For hardware comparison, hold board, CircuitPython, artifacts, configuration,
profile, filesystem mode, broker interface, startup history, command sequence,
and pacing constant. Record cold start versus REPL/soft restart. A fault may take
over 12 hours to emerge. Broker-side TCP capture aligned to serial should show
MQTT packet completeness, TCP ACKs/retransmissions, FIN/RST, and publication gaps;
subscriber capture alone may not identify the transport mechanism. Native-memory
instrumentation requires a separate feasibility/budget assessment.

The operator owns deployment and approval of physical relay actions. Never write
to `/Volumes/CIRCUITPY`. For approved toggle validation, use fresh command IDs,
correlate ACK/result/event/state/patch and deferred metadata, follow subsequent
availability cycles, and check persistence and physical output independently.
Do not call the spontaneous-stall regression fixed until relevant on-device and
broker-visible sequences pass.
