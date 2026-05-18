# Meta and Meta Switch Split - 2026-05-13

## Context

Branch: `mqtt-socket-network-stability`

Base branch: `soil-rwfs-fix`

Task: implement the Sensorius contract split between retained
`nodus/<device_id>/meta` and retained `nodus/<device_id>/meta/switch`.

The purpose of this change is to keep cold-boot retained `meta` compact while
still providing Sensorius with the full switch channel topic map after MQTT
startup publishes begin.

## Implementation

Runtime `meta` now keeps only switch summary fields:

- `switch.device_id`
- `switch.location`
- `switch.channel_count`
- `switch.meta_topic`

Runtime `meta` no longer embeds `switch.channels[*]`.

Added `build_switch_meta_payload()` in `cpynodus_ii/features/payloads.py`.
The new retained `meta/switch` payload uses schema `nodus-meta-switch/v1` and
contains the per-channel control topic map:

- `index`
- `label`
- `channel_id`
- `state`
- `event_topic`
- `state_topic`
- `set_topic`
- `ack_topic`
- `result_topic`
- `availability_topic`

Hardware pin fields are intentionally omitted from MQTT `meta/switch`.

Added `publish_switch_meta_cycle()` in `cpynodus_ii/features/publish_cycle.py`.
It publishes retained `nodus/<device_id>/meta/switch` when switch capability is
present.

Startup retained `meta/switch` is queued with `publish_startup_cycle()` before
runtime command subscriptions can block progress.

Updated feature exports in `cpynodus_ii/features/__init__.py`.

Updated contract docs:

- `docs/sensorius_contract.md`
- `docs/mqtt.md`

Firmware version was bumped from `v0.26.125.7` to `v0.26.133.7` because this is
runtime firmware behavior. The later patch increments on the same date came from
field-test adjustments to startup publish order and cold-start broker hostname
resolution, Wi-Fi recovery hardening after the overnight run, and defensive
MQTT rebuild state clearing and verification.

## Expected MQTT Behavior

On MQTT connect or reconnect:

1. Nodus queues retained `nodus/<device_id>/meta`.
2. Nodus queues retained heartbeat and sensor availability.
3. Nodus queues sensor data when ready.
4. Nodus queues retained `nodus/<device_id>/meta/switch` when switch channels
   are present.
5. Nodus queues runtime subscriptions.

Sensorius should use retained `meta` for quick device/sensor materialization
and retained `meta/switch` for detailed switch control topics.

## Verification

Focused checks:

```text
python -m ruff check cpynodus_ii/app.py cpynodus_ii/features/payloads.py cpynodus_ii/features/publish_cycle.py cpynodus_ii/features/steady_state.py cpynodus_ii/features/__init__.py tests/test_app_startup.py tests/test_feature_payloads.py tests/test_publish_cycle.py tests/test_steady_state.py tests/test_steady_state_transport.py
pytest tests/test_app_startup.py tests/test_mqtt_client_adapter.py tests/test_runtime_config.py tests/test_publish_cycle.py tests/test_steady_state.py tests/test_steady_state_transport.py
```

Result:

```text
ruff: All checks passed
pytest focused suite: 82 passed
```

Full host-side suite:

```text
pytest tests
```

Result:

```text
305 passed
```

## Notes

This implementation intentionally does not pull in broader MQTT recovery
experiments from later commits. It only implements the split metadata contract
and queues `meta/switch` as part of startup identity publishing so subscription
stalls cannot suppress it.

## Follow-up From 2026-05-13 Cold-Boot Test

Serial log review from `/Users/twfarley/cu.usbmodem13401.log`, starting at
line 18368, showed:

- `v0.26.125.7` published embedded switch channels in retained `meta`.
- The first `v0.26.133.1` cold boot published compact retained `meta`, then
  hit `mqtt_subscribe_failed` before retained `meta/switch` was queued.
- Recovery then encountered `socket_resolve_host() returned -2` while using
  broker host `samhain.local`.
- Changing `settings.toml` to use broker `10.0.0.248` avoided the host
  resolution failure and the next cold boot published retained `meta/switch`.

Implementation adjustment:

- retained `meta/switch` now queues in `publish_startup_cycle()` before
  runtime subscriptions;
- the delayed steady-state `switch_meta_generation` gate was removed.

## Follow-up From 2026-05-13 Broker Host Test

Serial log review from line 18547 showed another `samhain.local` cold boot:

- `v0.26.133.2` connected to `samhain.local`;
- retained `meta` and retained `meta/switch` reached the broker;
- startup command subscription then stalled with `No data received from broker
  for 10 seconds`;
- recovery hit `socket_resolve_host() returned -2` resolving `samhain.local`.

The retained startup publish order was adjusted again:

1. retained `meta`
2. retained heartbeat
3. retained sensor availability
4. sensor data when ready
5. retained `meta/switch`
6. retained switch availability and state
7. runtime subscriptions

This keeps compact identity first, then clears stale offline liveness before
larger split switch metadata or runtime subscriptions can consume the unstable
early MQTT window.

## Follow-up From Cold-Start Hostname Resolution

Warm starts can resolve and connect to `samhain.local`, so the desired behavior
is not to replace the hostname with the broker IP. The IP is a fallback only.

Implementation adjustment:

- added a broker-hostname settle gate before MQTT connect fallback;
- when both `MQTT.BROKER` and `MQTT.BROKER_IP` exist and the broker is a
  hostname, Nodus preflights the hostname during MQTT recovery;
- if hostname resolution fails and `MQTT.BROKER_IP` is configured, Nodus logs
  `mqtt connect phase=dns_fallback ...` and allows the normal broker target
  list to try the fallback IP;
- if no broker IP fallback is configured, Nodus can still log
  `mqtt connect phase=dns_wait ...` inside the settle window and retry later.

This preserves normal `samhain.local` operation when it resolves, while keeping
the configured broker IP as the recovery path when hostname resolution fails.

## Follow-up From Overnight Wi-Fi Recovery Test

Serial log review from line 7395 showed an overnight `v0.26.125.7` run where
Wi-Fi/IP disappeared while MiniMQTT still reported connected:

- health logs showed `network_phase=ready`, `ipv4=none`, and
  `mqtt_connected=True`;
- MQTT publish then failed with `EHOSTUNREACH`;
- recovery entered `wifi` and retried one Wi-Fi connect every 5 seconds until
  `wifi_recovery_timeout` soft rebooted the device;
- after reboot, the same loop repeated while the radio reported
  `No network with that ssid`.

Implementation adjustment:

- Wi-Fi link loss now takes priority over a stale MQTT-connected state in
  recovery decisions.
- Wi-Fi recovery keeps the initial 5 second retry cadence briefly, then backs
  off to 30 second retries after 60 seconds.
- Backed-off Wi-Fi retries force a station reset and rebuild socket artifacts
  before reconnecting.

This keeps MQTT from masking a lost station IP and gives the Pico W radio a
stronger recovery path before the existing `wifi_recovery_timeout` soft reboot.

## Follow-up From `v0.26.133.4` MQTT Rebuild Test

Serial log review from line 18700 showed MQTT recovery entering rebuild with
`mqtt_connected=False`, then reconnecting to `samhain.local`. The flag was
being cleared by the prior poll/sync failure path, but MQTT rebuild did not
itself enforce that invariant.

Implementation adjustment:

- MQTT rebuild now calls `transport.mark_disconnected()` before rebuilding
  network socket artifacts and constructing a new MQTT adapter.

This makes the rebuild path defensively clear stale MQTT connection state even
if a future failure path reaches rebuild without first clearing the transport.

## Follow-up From MQTT Rebuild Verification

MQTT transport now tracks recent successful client operations. A successful
connect, publish, subscribe, or poll updates `last_success_at`.

Implementation adjustment:

- MQTT transport also tracks `last_disconnected_at` and
  `last_disconnect_reason`.
- MQTT client failure paths now pass compact reason strings into
  `transport.mark_disconnected(reason=...)`.
- Recovery phase changes log `mqtt disconnected reason=...` when the transport
  is down and a reason is available.
- If MQTT recovery starts shortly after a successful MQTT operation, Nodus logs
  `mqtt action=verify_before_rebuild ...` and gives the existing MQTT adapter
  one reconnect attempt before rebuilding socket artifacts.
- If that verification connect does not recover the transport, the existing
  rebuild cadence still runs on the next MQTT rebuild interval.

This avoids unnecessary socket/client rebuilds after short MQTT blips while
still preserving bounded recovery and makes the serial recovery transition show
why MQTT was marked disconnected.

## Review of Recent Branch Changes

Reviewing the uncommitted branch diff showed the following changes beyond the
initial metadata split:

- Runtime: compact `meta` plus retained `meta/switch` payload and startup
  publish ordering.
- Runtime: cold-start hostname preflight with `dns_fallback` logging when a
  configured `BROKER_IP` is available.
- Runtime: Wi-Fi recovery now prioritizes lost station IP over stale MQTT
  connected state, backs off after 60 seconds, and forces a station reset
  during backed-off retries.
- Runtime: MQTT rebuild defensively clears local connected state before socket
  artifact rebuild.
- Runtime: MQTT transport tracks recent success and disconnect reason, and
  uses recent success to verify before rebuilding the MQTT client/socket path.
- Docs/tests: MQTT contract docs and focused tests were updated for those
  behaviors.
- Local tooling: `.vscode/settings.json` now points to the CircuitPython bundle
  path dated `20260508` instead of `20260424`; this is an IDE path update, not
  firmware behavior.

Version updates:

- `v0.26.133.1` -> `v0.26.133.2`
- `v0.26.133.2` -> `v0.26.133.3`
- `v0.26.133.3` -> `v0.26.133.4`
- `v0.26.133.4` -> `v0.26.133.5`
- `v0.26.133.5` -> `v0.26.133.6`
- `v0.26.133.6` -> `v0.26.133.7`

## Follow-up From `v0.26.133.8` MiniMQTT Socket Arity Test

Serial log review from line 18842 showed MQTT recovery after a successful
`samhain.local` cold start:

- the client connected and published startup MQTT records;
- recovery later reported `mqtt_connected=False`;
- the disconnect reason was
  `mqtt_poll_failed:minimqtt_socket:sock=wrapped backcompat=0 error=function takes 3 positional arguments but 2 were given`.

Implementation adjustment:

- the MiniMQTT socket compatibility wrapper now retries both `recv_into` and
  `send` argument shapes on the visible socket;
- if the visible MiniMQTT wrapper still raises the known arity error, the
  adapter tries the raw inner socket before marking MQTT disconnected;
- if neither path can satisfy the call, the existing compact disconnect reason
  is still reported.

Interpretation:

- this error may be a secondary symptom from a failed or stale MiniMQTT socket,
  not the original socket failure cause;
- earlier notes already showed this class of failure where the station or
  socket path degraded while MiniMQTT still exposed a misleading connected or
  wrapper-level state;
- if the raw inner socket retry also fails, MQTT recovery should proceed to the
  existing socket/client rebuild path rather than treating the arity text as
  proof of a pure API signature mismatch.

Firmware version was bumped from `v0.26.133.8` to `v0.26.133.9`.

## Follow-up From `v0.26.133.8` Socket Churn Test

Longer `co2-ykdvea` observation showed repeated MQTT recovery while Wi-Fi
remained ready:

- `mqtt_poll_failed:minimqtt_socket:... function takes 3 positional arguments
  but 2 were given`;
- `mqtt_publish_failed:nodus/S2-ykdvea/availability:[Errno 5] Input/output
  error`.

Both failure classes recovered quickly, but they indicate the MQTT client/socket
path was unhealthy. Reusing the existing MQTT adapter during
`verify_before_rebuild` is the wrong tradeoff for these hard socket-class
disconnect reasons.

Implementation adjustment:

- `verify_before_rebuild` remains available only for ambiguous disconnects;
- MQTT poll failures, publish failures, subscribe failures, callback failures,
  and MiniMQTT disconnect callbacks now skip verification and force the normal
  socket-artifact rebuild path;
- this should reduce stale MiniMQTT client/socket reuse after `Errno 5`,
  `Errno 9`, SUBACK timeout, and wrapped-socket poll failures.

Firmware version was bumped from `v0.26.133.9` to `v0.26.133.10`.

## CircuitPython 9.2.9 Status

The earlier MQTT recovery note documents why CircuitPython `9.2.9` was tested:
its upstream release notes included Pico W network fixes. That test did not
prove `9.2.9` resolves this MQTT/socket failure mode.

Current `v0.26.133.10` comparison keeps that interpretation:

- `co2-ykdvea` on CircuitPython `9.2.8` reproduced the cold-start SUBACK
  timeout followed by repeated DNS/connect failures before recovering;
- `aht-rvwi73` on CircuitPython `9.2.9` started cleanly in the same short
  observation window;
- this is not enough evidence to credit `9.2.9` with fixing the issue because
  prior `9.2.9` tests reproduced the same class of MQTT/socket failures.

Keep CircuitPython version in the test notes, but do not treat `9.2.9` as a
confirmed fix for the `samhain.local` MQTT instability.

## Follow-up From `v0.26.133.10` Station Scan-Miss Test

Fresh two-device testing ruled out CircuitPython `9.2.9` as a solution:

- `aht-rvwi73` stayed healthy on `PeaceHill`, with IP, DNS, NTP, and MQTT ready;
- `co2-ykdvea` repeatedly reported `No network with that ssid` on the same
  SSID and later recovered after additional warm-boot/recovery cycles;
- this pattern is not a general Wi-Fi outage when a sibling Nodus remains
  online on the same SSID.

Implementation adjustment:

- added `network_error_signature()` so station failures are classified into
  compact signatures such as `station_scan_miss_after_ready`,
  `station_scan_miss`, `station_unknown_after_ready`, and
  `station_auth_failed`;
- Wi-Fi recovery now logs sparse `recovery wifi signature=... count=...`
  entries when reconnect attempts continue failing;
- health logs include `wifi_signature=...` for the current network state;
- entering Wi-Fi recovery now immediately marks MQTT disconnected with
  `wifi_link_lost` so stale MQTT state does not keep publishing while
  `ipv4=none`.

The recovery timeout behavior is intentionally unchanged. The new signature is
diagnostic-first: it preserves recovery from a real AP outage while making the
local station-scan failure mode obvious in serial logs and comparable across
multiple devices.

Firmware version was bumped from `v0.26.133.10` to `v0.26.133.11`.

## Follow-up From Paired `v0.26.133.10` / `v0.26.133.11` Runs

The paired `co2-ykdvea` and `aht-rvwi73` logs showed the branch is unstable in
more than one way:

- `co2-ykdvea` repeatedly failed station joins with mixed
  `station_scan_miss`, `station_unknown`, and `station_auth_failed`
  signatures before eventually recovering;
- `aht-rvwi73` kept Wi-Fi ready, but MQTT recovery logged
  `broker_ip phase=persisted ... ip=10.0.0.220`; this was the firmware's
  learned broker-IP update path, not proof that the final `settings.toml`
  still contained that value;
- while that learned value was active in the runtime, MQTT repeatedly tried
  fallback connects to `10.0.0.220` until `mqtt_recovery_timeout`;
- a later warm boot logged a learned broker-IP update back to `10.0.0.248`,
  and the observed `settings.toml` contains `BROKER_IP = "10.0.0.248"`;
- the problem is automatic broker-IP learning during unstable recovery, not the
  operator-configured `BROKER_IP` itself.

Implementation adjustment:

- automatic persistence of learned MQTT broker IPs is disabled;
- `BROKER_IP` is now treated as operator-controlled configuration only;
- MQTT can still use the configured broker IP as a fallback when
  `samhain.local` resolution fails, but successful hostname connects no longer
  rewrite the settings file.

Firmware version was bumped from `v0.26.133.11` to `v0.26.133.12`.

## Follow-up Broker Fallback Ordering

The `v0.26.133.12` adjustment was too blunt because it removed broker-IP
learning entirely. The intended behavior is a three-tier order:

1. Try configured `MQTT.BROKER`.
2. If that fails, try configured `MQTT.BROKER_IP`.
3. If both configured targets fail, try the runtime-learned IP candidate.

Implementation adjustment:

- hostname connects may keep a runtime-only learned candidate from successful
  resolution;
- that candidate is appended after the configured broker and configured broker
  IP targets;
- hostname success does not write the learned candidate to settings;
- configured `BROKER_IP` success does not rewrite settings;
- only direct success against the third learned-IP target can promote that IP
  into `settings.toml`.

This keeps `settings.toml` authoritative while allowing a learned fallback to
recover only after both configured paths fail.

Firmware version was bumped from `v0.26.133.12` to `v0.26.133.13`.

## Follow-up Startup Wi-Fi Scan Probe

The morning test runs started cleanly, but prior `co2-ykdvea` cold boots showed
`No network with that ssid` before a later reboot ran stable overnight. Startup
can be improved without changing steady-state recovery semantics.

Implementation adjustment:

- when startup station connect raises `No network with that ssid` and another
  startup attempt remains, Nodus now performs a bounded Wi-Fi scan for the
  configured SSID;
- the scan result is logged as
  `network scan attempt=<n> ssid=<ssid> result=<found|missing|unavailable|error>`;
- after the scan probe, station mode is reset and the radio gets at least a
  short two-second settle before the next startup connect attempt;
- final startup failure errors include `scan=<result>` and `scan_count=<n>`
  when a scan probe ran.

This differentiates a true AP outage (`scan=missing`) from a local station
connect-path issue (`scan=found` but `connect()` still failed) and may reduce
cold-boot failures where the radio stack is not fully ready for the first
connect call.

Firmware version was bumped from `v0.26.133.13` to `v0.26.134.1`.

## Follow-up From `v0.26.134.11` / `v0.26.134.12` Warm-Start Attempts

Warm-start testing on `co2-ykdvea` showed that the attempted follow-up fixes
after `v0.26.134.1` made MQTT startup recovery worse.

The `v0.26.134.1` soft-reboot path was slow, but resilient:

- Wi-Fi joined `PeaceHill` and held IP `10.0.0.219`;
- MQTT entered recovery and eventually connected to configured broker IP
  `10.0.0.248` after about 85 seconds;
- retained `meta`, heartbeat, availability, `meta/switch`, and sensor data
  were then published successfully.

The later `v0.26.134.11` and `v0.26.134.12` experiments changed that behavior
from slow convergence into repeated MQTT connect failures:

- `mqtt_connect_failed:10.0.0.248`;
- repeated `dns_resolved resolved_ip=10.0.0.248` followed by connection
  errors;
- in `v0.26.134.12`, the underlying MiniMQTT cause was exposed as
  `No data received from broker for 5 seconds`;
- additional connect-failure rebuild logic caused repeated MQTT/socket rebuild
  loops without restoring the data-publish path during the observed run.

Interpretation:

- the broker IP was not inherently bad, because `v0.26.134.1` eventually
  connected and published using `active_broker="10.0.0.248"`;
- Wi-Fi was not inherently bad, because the station link remained ready with
  IP `10.0.0.219`;
- the attempted fixes were too aggressive and interfered with the recovery
  behavior that previously converged.

Resolution:

- the tracked worktree was reverted to commit `e142cc4`
  (`first over night run without mqtt network instability, at least on one
  Nodus. Improved mqtt and network recovery. Improved wifi scan and startup`);
- treat `v0.26.134.1` as the current known-good baseline for warm-start
  recovery;
- preserve the resilient eventual-connect behavior before attempting any
  targeted reduction in startup latency.

## Follow-up From Broker Hostname/IP Policy Review

The broker hostname and Nodus device hostname are separate concerns. The Nodus
device hostname remains useful for identity, router association, and operator
diagnostics. The startup/MQTT issue is broker target ordering.

Updated policy:

- `MQTT.BROKER` remains the canonical broker hostname supplied by Sensorius.
- MQTT client connections use IP literals only.
- On each startup with a writable filesystem, Nodus resolves `MQTT.BROKER` and
  writes the resolved address to `MQTT.BROKER_IP` when it differs from the
  current value.
- If the filesystem is read-only, deployments should provide `BROKER_IP`
  manually; read-only runtimes only attempt a volatile hostname resolution when
  `BROKER_IP` is absent, and cannot persist it.
- MQTT adapter target construction now uses `BROKER_IP` first and does not use
  broker hostnames as connect targets.

This keeps hostname use for human/system identity and discovery while avoiding
hostname-based MQTT connect attempts during the fragile startup window.

## Follow-up From `v0.26.136.5` Post-Ready Wi-Fi Recovery

The `co2-ykdvea` `v0.26.136.4` run reproduced the earlier post-ready Wi-Fi
failure shape: after a known-good station link and a hard MQTT repeated-connect
reset, serial capture resumed with `recovery phase=wifi`, `wifi_link_lost`,
repeated `No network with that ssid`, and alternating
`station_scan_miss_after_ready` / `station_unknown_after_ready` signatures. The
AHT sibling stayed healthy on the same network and desk, so this path is still
being treated as local station/radio state rather than AP outage.

`v0.26.136.5` adds a targeted recovery path for that condition only: once Wi-Fi
has previously been ready, two after-ready scan-miss/unknown failures trigger a
station reset and socket-artifact rebuild on the next reconnect attempt. If the
same after-ready signatures persist for 90 seconds and at least three failures,
runtime performs a hard reset with reason `wifi_after_ready_failure`. Normal
startup Wi-Fi failures still use the longer generic Wi-Fi recovery timeout.

## Follow-up From `v0.26.136.6` Unattended Serial Capture

Hard recovery resets terminate unattended `screen` capture over USB serial,
which hides the most useful post-reset window during stability tests.
`v0.26.136.6` keeps the same recovery escalation reasons and timing but routes
recovery-owned escalations through `supervisor.reload()` instead of
`microcontroller.reset()`. Manual/web hard restarts and profile-reset hard
reboots are unchanged.

## Follow-up From `v0.26.136.6` Cold-Boot Subscribe Failures

Both 136.6 cold boots connected MQTT quickly, then failed on the first queued
`config/set` subscription and spiraled into repeated direct-IP connect failures.
`v0.26.136.7` restores the targeted MQTT-only subscribe-failure recovery path:
close the current MQTT client without shutdown publishes, rebuild the MQTT
adapter, reconnect, drain the existing subscription queue, and suppress
duplicate startup queueing for the recovered connection generation.

User reported the 136.7 cold start working again. Captured serial tails show
`aht-rvwi73` and `co2-ykdvea` each connected MQTT in `0.1s`, queued startup
subscriptions, and returned to `recovery phase=idle` without the prior 136.6
first-`config/set` SUBACK timeout in the observed window.

## Follow-up From `v0.26.136.7` MQTT Soft-Reload Loop

The 136.7 `co2-ykdvea` run later exposed the cost of making
`mqtt_repeated_connect_failures` a soft reload: after the first repeated
MiniMQTT connect-failure escalation, soft reloads rejoined Wi-Fi and synced NTP
but never restored MQTT publishes. A true cold start at 11:29 produced retained
meta, heartbeat, availability, and data publishes again on the same configured
broker IP, so the broken window is being treated as local runtime state that a
soft reload does not clear.

`v0.26.137.1` kept soft reload for general MQTT recovery timeout and
after-ready Wi-Fi recovery, but routed `mqtt_repeated_connect_failures` back to
hard reset. Powered-hub testing then showed that hard-reset bound still drops
USB CDC and terminates unattended `screen` logging. `v0.26.137.2` therefore
routes `mqtt_repeated_connect_failures` back through soft reload for serial
capture, while retaining the stronger station-reset MQTT rebuild before that
bound is reached.
