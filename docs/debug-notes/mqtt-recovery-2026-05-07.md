# MQTT Recovery Debug Notes - 2026-05-07

## Scope

- Device/log basis: `/Users/twfarley/cu.usbmodem13401.log`.
- Firmware context: work had reached `v0.26.126.4`, then reverted/adjusted to
  `v0.26.127.1` to patch a startup recovery regression.
- Current framing: resolve the cold-start startup issue that causes MQTT
  subscription failures and then a network/recovery spiral to reboot.
- Product constraint: users cannot be required to enter broker IP addresses.
  Hostname-based MQTT brokers must work for these devices. Direct-IP testing is
  diagnostic only, not an acceptable final requirement.
- Goal for this note: keep the active debugging state visible across task
  boundaries: hypotheses, tried changes, test/log results, and current
  conclusion.

## Current Observations

- 2026-05-08 direct-IP broker test on `co2-ykdvea`:
  - User changed the volatile ROFS config to `BROKER = "10.0.0.248"` and
    `BROKER_IP = ""`.
  - First complete direct-IP run started at `07:38:10`.
  - Runtime reported `mqtt_connect=deferred:10.0.0.248`.
  - MQTT connected to `10.0.0.248` at `07:38:11` in `0.2s`.
  - Startup queue was `pub=7 sub=5 rx=0`.
  - Instrumentation showed all startup publish calls returning success from
    `07:38:11` through `07:38:18`.
  - `07:38:20` subscription sync completed successfully:
    `published=0 subscribed=5 ... after=queues pub=0 sub=0 rx=0 ...
    op=subscribe topic=nodus/S2-ykdvea/config/set ... errors=none`.
  - `07:39:12` a subsequent `/data` publish also returned success.
  - This complete direct-IP run is not the same failure mode as the earlier
    hostname run, because it received all SUBACKs and drained the subscription
    queue.
  - A later direct-IP run starting around line 5201 was a separate run:
    - Wi-Fi initially had `Unknown failure 205` and an authentication retry,
      then became ready on attempt 3.
    - The run starts with relative-second log timestamps (`0s`, `4s`, etc.);
      `2026-05-08 07:40:11` is the first post-NTP timestamp in that run, not
      the reliable wall-clock start time of the boot.
    - MQTT connected to `10.0.0.248` in `0.1s`.
    - Startup queue was `pub=8 sub=5 rx=0`.
    - Publish calls succeeded through `nodus/S1-ykdvea/state` at `07:40:27`.
    - There was an 11 second gap between the `S2` availability publish
      success at `07:40:16` and the `S1` state publish success at `07:40:27`.
    - The run was interrupted while the app was in the shutdown/finally path,
      flushing MQTT during `disconnect_mqtt_client`; the stack shows
      `client.publish()` blocked in `_send_bytes`.
    - Subscriptions were not reached in this run, so it does not prove the
      original first-SUBACK failure. It does show a direct-IP cold-start publish
      stall/hang can happen after Wi-Fi startup retries.
  - If broker-side MQTT logs show no messages for `07:38:11` to `07:39:12`,
    that is now a broker-log capture/filtering question, because serial proves
    the broker returned SUBACKs during that interval.
- 2026-05-08 `v0.26.128.1` ROFS quick-turn run on `co2-ykdvea`:
  - User reported no broker-visible MQTT messages; run was manually
    terminated after the failure was observed.
  - Serial section starts around line 5095.
  - `07:18:37` booted `v0.26.128.1` in `ROFS` with `persistence_mode=volatile`.
  - `07:18:37` MQTT connected to `samhain.local` in `0.1s`.
  - Startup queue was `pub=7 sub=5 rx=0`.
  - Instrumentation shows all startup publish calls returned success:
    - `07:18:37` `nodus/co2-ykdvea/meta`, retain, 1994 bytes.
    - `07:18:38` `nodus/co2-ykdvea/status/heartbeat`, retain, 97 bytes.
    - `07:18:39` `nodus/co2-ykdvea/availability`, retain, 100 bytes.
    - `07:18:40` `nodus/S1-ykdvea/availability`, retain, 100 bytes.
    - `07:18:41` `nodus/S2-ykdvea/availability`, retain, 100 bytes.
    - `07:18:42` `nodus/S1-ykdvea/state`, retain, 138 bytes.
    - `07:18:43` `nodus/S2-ykdvea/state`, retain, 145 bytes.
    - `07:18:54` `nodus/co2-ykdvea/data`, non-retained, 322 bytes.
  - After these calls, queue state was `pub=0 sub=5 rx=0`.
  - `07:19:15` first subscribe attempt failed on
    `nodus/co2-ykdvea/config/set` with `No data received from broker for
    10 seconds`.
  - Recovery then attempted MQTT rebuild, hostname resolution failed with `-2`,
    and direct socket connect was interrupted manually.
  - This run confirms the firmware can return success from all startup publish
    calls while the broker sees no messages before first subscription failure.
- 2026-05-08 cold-start failure on `co2-ykdvea`:
  - Previous healthy MQTT stream ended with data at `06:33:24` and offline
    heartbeat at `06:33:35`.
  - Serial log around line 4931 shows cold startup:
    - `06:34:16` NTP synced.
    - `06:34:16` MQTT connected to `samhain.local`.
    - `06:34:16` MQTT subscriptions queued:
      `queues pub=8 sub=5 rx=0`.
    - No broker-visible MQTT startup messages were observed after that connect.
    - `06:35:25` first subscription failed:
      `mqtt_subscribe_failed:topic=nodus/co2-ykdvea/config/set:index=0/5:
      ('No data received from broker for 10 seconds.', None)`.
    - At failure, serial queue summary was `before=queues pub=0 sub=5 rx=0`,
      meaning firmware believed the publish queue had drained before attempting
      subscriptions.
    - Recovery then repeatedly failed hostname resolution for `samhain.local`
      and direct-IP connect to `10.0.0.248`.
    - `06:38:27` recovery soft rebooted due to `mqtt_recovery_timeout`.
    - `06:38:43` warm reboot connected cleanly.
    - `06:38:44` broker-visible meta resumed, followed by data, heartbeat, and
      availability.
  - The broker-visible gap was approximately `06:33:35` offline to `06:38:44`
    meta.
- 2026-05-08 comparison cold start on `co2-ph244` was clean:
  - `06:31:16` meta, `06:31:18` heartbeat, `06:31:19` availability,
    `06:31:20` fwupdate result, and recurring data afterward.
  - Serial shows `queues pub=4 sub=3 rx=0` at startup and later health shows
    `queues pub=0 sub=0 rx=0`.
- 2026-05-08 morning soak result:
  - Both serial logs show healthy runs over 14+ hours.
  - MQTT logs also show current healthy operation for both devices.
  - `co2-ph244` continues `/data` publishes at roughly 60 second cadence and
    heartbeat/availability at roughly 2 minute cadence through at least
    `2026-05-08T06:20:18-0600`.
  - `co2-ykdvea` also continues `/data` publishes at roughly 60 second cadence
    and heartbeat/availability at roughly 2 minute cadence through at least
    `2026-05-08T06:21:14-0600`.
  - This confirms `v0.26.127.1` can remain healthy overnight on both the
    Home Assistant broker/profile path and the Sensorius broker/profile/switch
    path.
- `v0.26.127.1` startup reaches Wi-Fi ready, NTP synced, MQTT connected, and
  recovery idle.
- Representative 127.1 boot:
  - `15:17:52` MQTT connected to `samhain.local`.
  - `15:17:53` recovery moved from `mqtt` to `idle`.
  - `15:19:01` first queued subscription failed:
    `mqtt_subscribe_failed:topic=nodus/co2-ykdvea/config/set:index=0/5:
    ('No data received from broker for 10 seconds.', None)`.
  - After that, recovery repeatedly rebuilt MQTT/network artifacts while Wi-Fi
    remained associated at `10.0.0.219`.
  - Hostname resolution for `samhain.local` failed with `-2`, and direct IP
    fallback to `10.0.0.248` failed with repeated connect failures.
  - `15:22:03` recovery timed out and soft rebooted.
  - `15:22:21` the reboot reconnected successfully and published meta again.
- A `15:16:40` 127.1 stop was caused by serial `KeyboardInterrupt` inside MQTT
  polling and is not treated as an autonomous firmware crash.
- Test plan update: `v0.26.127.1` is loaded on both `co2-ph244` and
  `co2-ykdvea`.
- `co2-ykdvea` cold-boot test was manually terminated because MQTT `/data`
  did not continue after the first data publish.
- `co2-ph244` cold booted on `v0.26.127.1` and behaved as desired:
  - `15:43:29` meta published with `version=v0.26.127.1`.
  - `15:43:30` heartbeat online.
  - `15:43:31` availability online.
  - `15:43:33` first `/data`.
  - Continued `/data` at roughly 60 second cadence through at least `15:49:34`.
  - Continued heartbeat/availability at roughly 2 minute cadence.
- `co2-ph244` differs from `co2-ykdvea` in profile and broker:
  - `co2-ph244`: `homeassistant` profile, broker `homeassistant.local`,
    broker IP `10.0.0.208`, MQTT username/password configured, no switch.
  - `co2-ykdvea`: `sensorius` profile, broker `samhain.local`, broker IP
    `10.0.0.248`, switch enabled with two channels.
- Historical `co2-ykdvea` `v0.26.125.7` behavior was mixed:
  - Log section starting near line 187 boots `v0.26.125.7` at `15:25:39`.
  - That first run hit the same first subscription timeout at `15:26:47`:
    `mqtt_subscribe_failed:topic=nodus/co2-ykdvea/config/set:index=0/5:
    ('No data received from broker for 10 seconds.', None)`.
  - The reconnect path was interrupted by a serial `KeyboardInterrupt` in
    broker resolution.
  - The following soft reboot at `15:27:33` booted `v0.26.125.7` again and
    then remained healthy for hours. Periodic health logs show
    `mqtt_connected=True` and queue summaries show `sub=0`, meaning
    subscriptions drained successfully on that run.
  - Later May 7 `v0.26.125.7` runs also show the first-subscribe timeout and
    recovery timeout behavior, so 125.7 was not immune; it was intermittent.

## Working Hypotheses

- A subscription ACK timeout should poison the current MiniMQTT client/session.
- That timeout should not by itself poison the whole device runtime.
- Current evidence suggests the first SUBACK timeout can damage enough of the
  lower network path that a normal MQTT reconnect is not sufficient.
- In the current tree, MQTT recovery already calls
  `reconnect_network_stack(..., rebuild_socket_artifacts=True)`, so recovery is
  not merely preserving the old MQTT client/socket-pool wrapper.
- Remaining likely failure area: the Wi-Fi radio/IP association can still look
  ready while the lower CYW43/socket/network path is wedged below the rebuilt
  socketpool artifacts.
- Because `co2-ph244` on 127.1 continues publishing normally, this is not a
  universal 127.1 startup publish failure. The difference may be related to
  broker behavior, profile behavior, switch subscriptions, retained topic load,
  authentication, or timing on the `samhain.local` path.
- Because `co2-ykdvea` sometimes worked on 125.7 and sometimes failed with the
  same first-subscribe timeout, this does not look like a simple 127.1-only
  regression. It looks timing/state sensitive, with `ykdvea`/`samhain.local`
  able to enter either a healthy subscription state or a poisoned recovery path.
- After the warm reboot path seen in the overnight soak, behavior is healthy:
  recurring sensor data, heartbeat/availability, and switch toggles work.
- The cold-start failure now includes an important publish-path ambiguity:
  serial says the publish queue drained (`pub=0`) before the subscription
  timeout, but broker logs show no MQTT startup messages during that interval.
  This needs instrumentation before behavioral changes.
- `v0.26.128.1` instrumentation resolves that ambiguity at the firmware API
  layer: MiniMQTT publish calls returned success for each startup topic, but
  broker-visible delivery was still absent. The problem therefore lies below
  the application queue semantics: MiniMQTT/socket/radio/broker path can accept
  publish calls and then fail to produce broker-visible traffic/SUBACK.
- The `07:38:10` direct-IP run changes the host-path hypothesis:
  - Direct IP did not hit the first-SUBACK timeout in the complete run.
  - Hostname/mDNS resolution or the hostname connect path remains a stronger
    suspect than switch subscription ordering for this specific reproduction.
  - Direct IP is not a product-level fix because users must be able to use
    hostnames. It is only a diagnostic comparison path.

## Changes Tried / Version Notes

- Earlier work added multiple conditions around subscription behavior.
- Those changes became too complex and were reverted back toward `v0.26.126.4`.
- `v0.26.127.1` is the current baseline for the startup recovery patch.
- `v0.26.128.1` adds instrumentation only:
  - `MQTTClientSyncResult` now carries operation metadata:
    operation, topic, retain flag, payload byte count, and pending count.
  - Serial MQTT sync summaries now include this metadata.
  - The app logs sync operations during a bounded 180 second window after each
    MQTT connect, plus all sync errors.
  - No publish/subscribe ordering, retry, recovery, or queue behavior was
    intentionally changed.
- `v0.26.128.2` adds one more instrumentation-only field:
  - MQTT sync results now include elapsed milliseconds for the actual
    MiniMQTT `publish()` or `subscribe()` call.
  - Serial sync summaries append `elapsed_ms=<n>`.
  - This is intended to distinguish a slow/blocking MiniMQTT call from normal
    application-loop delay between calls.
  - No publish/subscribe ordering, retry, recovery, or queue behavior was
    intentionally changed.
- `v0.26.128.3` fixes instrumentation formatting only:
  - Serial formatting now preserves true `0` values for `elapsed_ms`,
    `pending`, and `bytes` instead of coercing them through fallback values.
  - No MQTT behavior was intentionally changed.

## 128.2 Cold/Warm Comparison

- 2026-05-08 `v0.26.128.2` cold boot comparison begins around line 5248.
- Cold boot run:
  - Device was still using direct-IP broker target `10.0.0.248`.
  - Wi-Fi joined on attempt 1 and reported ready at relative `5s`.
  - MQTT connected at first post-NTP timestamp `08:00:55` in `0.1s`.
  - Startup queue was `pub=7 sub=5 rx=0`.
  - Startup retained publishes were mostly fast:
    - meta: `elapsed_ms=1`
    - heartbeat: `elapsed_ms=1`
    - sensor availability: `elapsed_ms=1`
    - S1 availability: `elapsed_ms=1`
    - S2 availability: `elapsed_ms=1`
    - S1 state printed `elapsed_ms=-1`; with 128.2 formatting this likely
      represents a true `0 ms` measurement.
    - S2 state: `elapsed_ms=1`
  - The first `/data` publish then took `elapsed_ms=9973`, almost 10 seconds,
    before returning success.
  - The first subscribe attempt on `nodus/co2-ykdvea/config/set` failed with
    `No data received from broker for 10 seconds` and reported
    `elapsed_ms=19987`.
  - After the subscribe failure, recovery repeatedly failed direct-IP MQTT
    reconnects with `Repeated connect failures` until manual termination.
- Warm boot run immediately after:
  - Starts around line 5308.
  - Wi-Fi joined on attempt 1 and MQTT connected to `10.0.0.248` in `0.2s`.
  - Startup queue was `pub=8 sub=5 rx=0`.
  - Startup publishes were fast:
    - meta/heartbeat/availability/state/data all reported `elapsed_ms=1` or
      `2`, except one `elapsed_ms=-1` that likely represents a 0 ms formatting
      artifact in 128.2.
  - All five subscriptions completed successfully in one sync call with
    `elapsed_ms=26` and `errors=none`.
- Comparison:
  - The same direct-IP broker and same device can cold boot into a slow
    MiniMQTT send/read path, then warm boot into normal behavior immediately
    afterward.
  - The cold failure is not caused by every publish being slow. It appears
    after several fast startup publishes, at the first `/data` publish and then
    the first subscription wait.
  - Direct IP does not eliminate the cold-start issue; it only removes hostname
    resolution from this specific comparison.

## Web Research Findings

- 2026-05-08 research focused on Adafruit MiniMQTT, CircuitPython networking,
  Pico/Pico W socket behavior, and cold-start recovery.
- Adafruit MiniMQTT documentation:
  - MiniMQTT exposes `recv_timeout`, `socket_timeout`, and `connect_retries`.
  - `recv_timeout` defaults to 10 seconds, matching the observed
    `No data received from broker for 10 seconds` failure.
  - MiniMQTT exceptions are described as protocol or network/system level
    errors; the documented robust recovery path is reconnect.
  - MiniMQTT `connect()` and `reconnect()` perform exponential backoff on
    connect failures.
- Adafruit MiniMQTT learning guide:
  - The `loop()` method keeps the MQTT connection alive and processes incoming
    and outgoing MQTT messages.
  - It explicitly does not handle Wi-Fi/network-hardware or broker
    disconnection; application code must handle those failures.
- CircuitPython `socketpool` documentation:
  - `SocketPool` is tied to the connected radio/network interface.
  - Sockets support blocking, non-blocking, and timeout modes.
  - `sendall()` can fail after partially sending bytes, and the API cannot
    report how much was sent.
- CircuitPython release research:
  - The current device serial reports CircuitPython `9.2.8`.
  - CircuitPython `9.2.9` release notes say it fixes network crashes on Pico W
    and fixes a regression since `9.2.5`.
  - CircuitPython `10.0.0` release notes include RP2 networking changes:
    updated Raspberry Pi `pico-sdk` to `2.2.0`, updated `cyw43-driver` to
    `v1.1.0`, and added `wifi.radio.power_management`.
  - Inference: our observed cold-start socket path problem may be below
    MiniMQTT, in CircuitPython/RP2/cyw43/lwIP startup state. This is not proof,
    but it matches the cold-vs-warm pattern better than a pure application
    queue-ordering bug.
- Raspberry Pi Pico SDK issue research:
  - There are upstream Pico W reports of station-mode Wi-Fi connect hangs in
    `cyw43_arch_wifi_connect_timeout_ms`, including behavior where progress can
    stop until external serial activity occurs.
  - This is not CircuitPython-specific proof, but it supports the broader
    hypothesis that Pico W/CYW43 cold network bring-up can get into states that
    look connected while lower network progress is unreliable.

## Web Research Conclusion

- I did not find a MiniMQTT-specific startup-subscribe bug that cleanly explains
  our logs.
- The upstream evidence points more strongly at the lower network stack:
  CircuitPython/RP2/CYW43/lwIP/socketpool readiness at cold start.
- The most actionable finding is that the device is on CircuitPython `9.2.8`,
  while `9.2.9` is a targeted Pico W network-crash regression fix. Testing
  `9.2.9` should happen before adding broad firmware workarounds.
- Because hostname broker support is a product requirement, direct-IP testing
  remains diagnostic only.

## 9.2.9 Retest Results

- 2026-05-08 retested on CircuitPython `9.2.9` despite prior concerns because
  upstream release notes mention Pico W network fixes.
- First `9.2.9` run starts around line 5365:
  - CircuitPython banner: `Adafruit CircuitPython 9.2.9`.
  - Firmware: `v0.26.128.2`.
  - Broker target: direct IP `10.0.0.248`.
  - User reported no broker-visible MQTT data from Nodus during startup.
  - Wi-Fi joined on attempt 1.
  - MQTT connected in `0.1s`.
  - Startup queue was `pub=8 sub=5 rx=0`.
  - Early publishes were fast:
    - meta: `elapsed_ms=1`
    - data: `elapsed_ms=2`
    - heartbeat: `elapsed_ms=1`
    - sensor availability printed `elapsed_ms=-1`; in 128.2 that may be a
      true 0 ms formatting artifact.
    - S1 availability: `elapsed_ms=1`
    - S2 availability: `elapsed_ms=1`
  - Then the MQTT socket path slowed:
    - S1 state publish: `elapsed_ms=9974`
    - S2 state publish: `elapsed_ms=19940`
    - first subscribe on `nodus/co2-ykdvea/config/set`: `elapsed_ms=29959`,
      failed with `No data received from broker for 10 seconds`.
  - Recovery then hit direct-IP `Repeated connect failures`.
- Second `9.2.9` run starts around line 5415:
  - Firmware: `v0.26.128.3`.
  - Broker target: direct IP `10.0.0.248`.
  - User again reported no broker-visible MQTT data from Nodus during startup.
  - Wi-Fi had an initial `Unknown failure 1`, then joined on attempt 2.
  - MQTT connected in `0.1s`.
  - Startup queue was `pub=8 sub=5 rx=0`.
  - Early publishes were fast:
    - meta: `elapsed_ms=2`
    - data: `elapsed_ms=1`
    - heartbeat: `elapsed_ms=1`
    - sensor availability: `elapsed_ms=1`
    - S1 availability: `elapsed_ms=2`
    - S2 availability: `elapsed_ms=1`
  - Then the same slowdown pattern repeated:
    - S1 state publish: `elapsed_ms=9970`
    - S2 state publish: `elapsed_ms=19931`
    - first subscribe on `nodus/co2-ykdvea/config/set`: `elapsed_ms=29951`,
      failed with `No data received from broker for 10 seconds`.
  - Recovery then hit direct-IP `Repeated connect failures`.
- 9.2.9 conclusion:
  - CircuitPython `9.2.9` does not resolve this cold-start MQTT/socket issue
    in this setup.
  - The repeated 10s/20s/30s elapsed pattern is strong evidence of stacked
    socket receive/send timeout behavior below the application queue.
  - Because broker-visible MQTT was absent even when early publish calls
    returned quickly, the successful MiniMQTT publish return is not a reliable
    delivery signal in this failure mode.

## Switch Shape Tests

- 2026-05-08 tested switchless and single-switch startup without code changes,
  using CircuitPython `9.2.9`, firmware `v0.26.128.3`, and direct-IP broker
  `10.0.0.248`.
- Switchless run starts around line 5530:
  - `switch enabled=False channels=0`.
  - Broker-visible MQTT messages were published: meta, heartbeat, availability,
    and data.
  - Meta showed `capabilities.switch=false` and `location_group.members` only
    contained `co2-ykdvea`.
  - Startup queue was reduced to `pub=3 sub=3 rx=0`.
  - Publish timings:
    - meta: `elapsed_ms=2`
    - heartbeat: `elapsed_ms=1`
    - availability: `elapsed_ms=1`
    - data: `elapsed_ms=0`
  - All three subscriptions completed successfully with `elapsed_ms=23`.
  - The run was manually interrupted later while polling; startup MQTT had
    already completed successfully.
- Single-switch run starts around line 5575:
  - `switch enabled=True channels=1`.
  - Channel list: `S1-ykdvea` / `Fan`.
  - User reported no broker-visible Nodus MQTT data from this run.
  - Startup queue was `pub=6 sub=4 rx=0`.
  - Publish timings were all fast:
    - meta: `elapsed_ms=1`
    - data: `elapsed_ms=2`
    - heartbeat: `elapsed_ms=1`
    - sensor availability: `elapsed_ms=1`
    - S1 availability: `elapsed_ms=1`
    - S1 state: `elapsed_ms=0`
  - First subscribe was still the sensor config topic
    `nodus/co2-ykdvea/config/set`, not the switch topic.
  - First subscribe failed after `elapsed_ms=10020` with
    `No data received from broker for 10 seconds`.
  - Recovery then hit direct-IP `Repeated connect failures`.
- Switch shape conclusion:
  - The failure is not caused by executing a switch subscription first; the
    failed subscription remains the sensor config topic.
  - However, the presence of even one switch channel changes the startup MQTT
    shape enough to trigger the cold-start socket/SUBACK failure.
  - The current discriminator is startup queue size/topic set:
    - no switch: `pub=3 sub=3`, success,
    - one switch: `pub=6 sub=4`, failure,
    - two switches: `pub=8 sub=5`, intermittent warm success but repeated cold
      failures.
  - This points toward startup MQTT burst size, retained switch topic traffic,
    retained switch state publication, broker retained-topic interaction, or
    socket receive/readiness timing after a larger startup exchange.

## Targeted Recovery Change

- 2026-05-08 agreed recovery direction:
  - Do not use an immediate warm reboot as the recovery response.
  - On early startup subscription failure, tear down MQTT first.
  - Then force a Wi-Fi station teardown/reconnect.
  - After Wi-Fi is ready and socket artifacts are rebuilt, allow MQTT to start
    again.
- Implemented in `v0.26.128.4`:
  - Added a no-publish MQTT close path for poisoned startup sockets.
  - Added `force_station_reset` to `reconnect_network_stack()`, which calls the
    radio station reset path before reconnecting.
  - App startup sync now detects an early `mqtt_subscribe_failed` during the
    bounded startup MQTT trace window.
  - The first matching startup subscribe failure triggers:
    - `close_mqtt_client()` with no shutdown/offline publish flush,
    - forced Wi-Fi station reset/reconnect with socket artifact rebuild,
    - MQTT adapter rebuild,
    - normal MQTT reconnect on the next loop.
  - This is intentionally a targeted startup path, not a general switch
    feature change and not a reboot.

## Startup NTP Ordering

- 2026-05-08 user noted NTP uses shared socket/network resources and should not
  run before startup MQTT completes.
- Implemented in `v0.26.128.5`:
  - For MQTT-enabled profiles, NTP is gated until MQTT is connected and the
    startup subscription queue has drained.
  - Profiles without MQTT can still run NTP normally.
  - This keeps NTP from consuming shared socket/network path during the fragile
    cold-start MQTT publish/subscribe sequence.

## Current Conclusion

- `v0.26.127.1` appears to resolve the startup recovery regression and is now
  supported by an overnight healthy soak on both test devices.
- The remaining issue is specifically cold-start startup: cold start can cause
  MQTT subscription failures which then lead into a network recovery spiral and
  reboot. Warm reboot/steady-state behavior appears healthy once startup has
  passed and subscriptions/queues drain successfully.
- New two-device result narrows the issue: `co2-ph244`/Home Assistant broker
  path works on 127.1, while `co2-ykdvea`/Sensorius broker path still shows the
  first-subscribe or post-first-data stall behavior.
- Historical 125.7 data further narrows this: `co2-ykdvea` has had both good
  and bad outcomes with the same broker/profile/switch shape. The failure is
  likely intermittent broker/socket/subscription timing rather than a clean
  version boundary.
- Overnight soak reduces urgency for immediate code changes. Next useful work
  should focus on preserving this baseline while hardening cold-start
  subscription/recovery behavior.
- Before behavioral changes, add narrowly scoped instrumentation around startup
  MQTT publish/subscribe flushing to distinguish:
  - publish call returned vs broker-visible delivery absent,
  - exact topic that was flushed last before first subscription,
  - whether the first subscription starts immediately after publish flush,
  - whether the failure is tied to the sensor config subscription or switch
    subscriptions.
- Instrumentation to answer those questions has been added in `v0.26.128.1`.
- First instrumented failure answer:
  - publish calls returned success for every startup topic,
  - first subscription started only after startup publishes drained,
  - first failed subscription was the sensor config topic, before switch
    subscriptions were attempted,
  - broker-visible delivery was absent despite successful publish returns.
- First direct-IP answer:
  - direct IP connected and subscribed cleanly in the complete `07:38:10` run,
  - all five subscriptions drained with `errors=none`,
  - that run does not reproduce the original hostname cold-start failure.
- Second direct-IP answer:
  - direct IP can still hit a startup publish stall after Wi-Fi startup retries,
  - the stall happened before subscriptions, so it is not the same first-SUBACK
    timeout, but it is relevant to the broader cold-start network fragility.
- Direct IP cannot be the final answer:
  - It may help isolate hostname/mDNS behavior from lower socket/radio behavior,
    but the firmware must support hostname brokers for normal users.
  - Any final recovery or startup fix must make hostname broker configuration
    reliable, not require users to discover and enter IP addresses.
- A likely next design is an escalation ladder:
  - SUBACK timeout: discard/close the current MQTT session.
  - First recovery: rebuild socket artifacts and MQTT client.
  - If hostname and direct-IP connect both fail afterward: force a real Wi-Fi
    radio disconnect/reconnect before rebooting.
  - Reboot only after Wi-Fi reconnect plus fresh socket artifacts plus direct-IP
    MQTT connect fail for a bounded window.

## Verification Log

- Read `/Users/twfarley/cu.usbmodem13401.log`.
- No pytest or hardware routine was run for this note-only update.
- 2026-05-08: User reported both serial logs healthy for 14+ hours and supplied
  MQTT log excerpts showing continued `/data`, heartbeat, and availability for
  both `co2-ph244` and `co2-ykdvea`.
- 2026-05-08: User supplied MQTT excerpts and directed analysis to 13401 line
  4931 cold-start failure. Serial and MQTT logs were compared. No code changes
  were made during this analysis.
- 2026-05-08: Added MQTT sync instrumentation and bumped runtime version to
  `v0.26.128.1`.
- 2026-05-08: User ran `co2-ykdvea` `v0.26.128.1` in ROFS mode. Serial
  instrumentation confirmed all startup publish calls returned success before
  first subscribe failure; user reported no broker-visible MQTT messages.
- 2026-05-08: User changed the ROFS MQTT broker to direct IP `10.0.0.248`.
  Serial analysis showed the `07:38:10` direct-IP run connected, published,
  subscribed all five topics successfully, and published another `/data`
  sample. The later run starting around line 5201 showed a pre-subscription
  publish stall/hang during shutdown flushing after Wi-Fi startup retries; its
  first absolute timestamp is post-NTP and should not be treated as the boot
  start time.
- 2026-05-08: Added `v0.26.128.2` operation elapsed-time instrumentation for
  MQTT publish/subscribe sync calls.
- 2026-05-08: User supplied `v0.26.128.2` cold/warm comparison starting around
  line 5248. Cold boot showed `/data` publish taking nearly 10 seconds and
  first subscribe failing after nearly 20 seconds; warm boot immediately after
  showed fast publishes and all five subscriptions completing in 26 ms.
- 2026-05-08: Added `v0.26.128.3` formatting fix so 0 ms diagnostics are not
  printed as `-1`.
- 2026-05-08: Web research added. Most relevant finding: CircuitPython `9.2.9`
  is an upstream Pico W network-crash regression fix, while the current device
  serial reports `9.2.8`.
- 2026-05-08: User retested with CircuitPython `9.2.9`. Both direct-IP cold
  starts reproduced the startup MQTT/socket failure with no broker-visible
  Nodus data and escalating elapsed timings around 10s, 20s, and 30s before
  first subscribe failure.
- 2026-05-08: User tested switch shape. Switchless startup succeeded with
  broker-visible messages and `pub=3 sub=3`; single-switch startup failed with
  no broker-visible messages and `pub=6 sub=4`, even though all publishes
  returned quickly and the first failed subscription was still the sensor config
  topic.
- 2026-05-08: Added `v0.26.128.4` targeted startup subscribe-failure recovery:
  close MQTT without shutdown publish, force Wi-Fi station reset/reconnect,
  rebuild socket artifacts and MQTT adapter, then let MQTT reconnect without
  rebooting.
- 2026-05-08: Added `v0.26.128.5` startup ordering change: NTP waits until
  MQTT startup has connected and drained subscriptions for MQTT-enabled
  profiles.
- 2026-05-08: User tested `v0.26.128.5` cold start at 13401 line 5620 with
  CircuitPython `9.2.9`, ROFS, direct-IP broker, and one switch channel. NTP
  did not run before MQTT, so the NTP deferral behaved as intended but did not
  resolve the cold-start MQTT failure.
- 2026-05-08: In that `v0.26.128.5` cold start, the first sensor config
  subscribe failed after about 10 seconds. The targeted recovery then closed
  MQTT, forced Wi-Fi station reset/reconnect, rebuilt MQTT, and successfully
  drained all four queued subscriptions in 26 ms. This confirms the forced
  Wi-Fi reset path can recover the first poisoned subscription state.
- 2026-05-08: Immediately after the successful recovery subscription drain,
  startup queued a second full MQTT startup generation (`gen=2`, `pub=6
  sub=4`). The duplicate publish set completed quickly, but the duplicate
  sensor config subscribe failed after about 10 seconds and led into repeated
  direct-IP MQTT connect failures until soft reboot.
- 2026-05-08: Current conclusion: the next target is not NTP and not the first
  forced Wi-Fi recovery itself. The next issue is duplicate startup MQTT
  sequencing after a successful startup-subscribe recovery. The firmware should
  avoid immediately requeuing the same startup subscriptions after recovery has
  already drained them.
- 2026-05-08: Added `v0.26.128.6` targeted guard for that duplicate sequence.
  When a startup subscription failure triggers forced MQTT/Wi-Fi recovery, and
  the following `post_connect` sync successfully drains subscriptions, the app
  advances steady-state to the recovered MQTT generation and logs
  `startup recovery phase=subscription_recovered
  action=suppress_duplicate_startup_queue`. Normal reconnect behavior is
  unchanged outside this one-shot startup recovery path.
- 2026-05-08: User tested `v0.26.128.6` at 13401 line 5775. The duplicate
  subscription sequence was suppressed and the device stayed online, but the
  broker did not receive `/meta` because the only startup `/meta` publish
  occurred on the pre-recovery poisoned MQTT/socket path. After recovery, the
  firmware subscribed successfully and later published periodic `/data`,
  heartbeat, and availability.
- 2026-05-08: Added `v0.26.128.7` refinement: after startup-subscribe recovery
  drains subscriptions, replay the existing startup publish cycle on the
  recovered MQTT connection while still suppressing duplicate subscriptions.
  The log marker changes to `startup recovery phase=subscription_recovered
  action=replay_startup_publish`.
- 2026-05-08: User tested `v0.26.128.7` at 13401 line 5837. The replay queued
  `/meta` on the recovered connection, but the run then showed DNS/NTP errors
  and a later `/data` publish taking nearly 10 seconds. Reverted the `128.7`
  replay-startup-publish refinement so the next repeated cold-start tests use
  the narrower `128.6` duplicate-subscription suppression behavior.
- 2026-05-08: User repeated `v0.26.128.6` cold-start tests starting at 13401
  line 5949. Runs at lines 5949, 6010, and 6068 all showed the same stable
  recovery pattern: startup publishes returned success, the first sensor config
  subscribe timed out after about 10 seconds, forced MQTT/Wi-Fi recovery
  reconnected, `post_connect` drained all four subscriptions in about 24 ms,
  duplicate startup subscriptions were suppressed, NTP synced, and
  broker-visible periodic `/data`, heartbeat, and availability resumed. No
  `/meta` was broker-visible after recovery in this reverted `128.6` behavior.
- 2026-05-08: Added `v0.26.128.8` test change: remove the startup-subscribe
  recovery Wi-Fi station reset/socket rebuild. On the same failure mode, the
  firmware now closes MQTT, rebuilds the MQTT adapter against the existing
  network socket artifacts, and lets the current MQTT reconnect path continue.
  This isolates whether the network teardown was required for the observed
  `128.6` recovery or whether MQTT-only rebuild is sufficient.
- 2026-05-08: User tested `v0.26.128.8` starting at 13401 line 6132. The
  failure and recovery pattern was similar to `128.6`: first sensor config
  subscribe timed out after about 10 seconds, MQTT-only rebuild reconnected
  without Wi-Fi station reset, `post_connect` drained all four subscriptions
  in 29 ms, duplicate startup subscriptions were suppressed, NTP synced, and
  broker-visible periodic `/data`, heartbeat, and availability resumed.
  Current conclusion: keep the MQTT-only startup recovery process for now; the
  network teardown/socket rebuild is not required for this observed cold-start
  recovery case.
- 2026-05-08: Longer `v0.26.128.8` review shows the cold-start recovery itself
  was stable, but the first run did not last continuously for the full soak.
  At 11:40:39 a steady-state `/data` publish failed after 28 seconds with
  `Errno 5`. MQTT rebuilt and reconnected, then steady-state queued a new
  connection generation (`gen=3`) with startup publishes and subscriptions.
  Publishes, including `/meta`, returned success, but the sensor config
  subscribe failed again after about 10 seconds. Because the startup-specific
  subscribe recovery had already been used, normal recovery spiraled through
  repeated direct-IP connect failures until soft reboot at 11:43:59. The
  broker-visible `/meta` at 11:44:11 came from the warm reboot that followed,
  after which the device ran normally for the supplied 1+ hour MQTT log.
- 2026-05-08: Added `v0.26.128.9` recovery change: any MQTT sync subscribe
  failure now gets the MQTT-only close/rebuild treatment, not only the first
  cold-start subscribe failure. After reconnect, if `post_connect` drains the
  subscription queue, the app advances steady-state to the recovered MQTT
  generation and suppresses duplicate startup subscriptions. This should cover
  the later `gen=3` subscribe failure seen after the 11:40 steady-state publish
  failure without requiring soft reboot.
- 2026-05-08: User tested `v0.26.128.9` at 13401 line 6335. Recovery still
  drained subscriptions and resumed periodic `/data`, heartbeat, and
  availability, but broker logs still lacked the normal retained startup
  `/meta`, heartbeat, and availability immediately after recovery because the
  first startup publish batch was on the pre-recovery poisoned connection and
  the duplicate startup generation was suppressed.
- 2026-05-08: Added `v0.26.128.10` retained startup refresh after successful
  subscription recovery. This queues only retained `/meta`, device heartbeat,
  sensor availability, and switch availability on the recovered connection.
  It intentionally does not queue `/data`, switch state, onboarding, or
  discovery payloads, avoiding the broader `128.7` startup replay behavior.
- 2026-05-08: User tested `v0.26.128.10` for 300+ seconds. No broker-visible
  startup MQTT messages were produced, and serial showed the retained refresh
  destabilized the socket path: after the recovered `/meta` publish, DNS/NTP
  failed with `socket_resolve_host() returned -2`, retained availability
  publishes stretched to roughly 20 seconds, health reported `dns_health=error`
  and `ntp_health=cooldown`, and shutdown flushing blocked in MiniMQTT publish.
  Current conclusion: retained post-recovery publish replay is unsafe in this
  cold-start failure mode. Reverted the `128.10` retained refresh in
  `v0.26.128.11`, keeping the broader `128.9` MQTT subscribe recovery.
- 2026-05-08: Added `v0.26.128.12` instrumentation and NTP gate tightening.
  `_ntp_allowed_for_startup()` now waits for both subscription and publish
  queues to drain before NTP can use the shared socket path. Serial logs now
  emit `ntp phase=deferred reason=mqtt_startup_pending ...` when NTP is held
  back by MQTT queues, and the subscription-recovery suppression log includes
  queue depths plus `retained_replay=disabled`. This should clarify whether a
  future retained replay is being interrupted by NTP or other queued MQTT work.
- 2026-05-08: User tested `v0.26.128.12`. Serial showed NTP deferral working:
  NTP was held while startup MQTT had pending publishes/subscriptions, recovery
  drained subscriptions, the suppression log showed `queues pub=0 sub=0 rx=0
  retained_replay=disabled`, and NTP then synced successfully. Broker logs
  still lacked `/meta` and initial retained startup messages after recovery;
  only later periodic `/data`, heartbeat, and availability were broker-visible.
  Current conclusion: NTP is not the reason `/meta` is missing in the current
  `.12` path. `/meta` is missing because the first startup publish batch occurs
  on the pre-recovery poisoned connection, and retained replay is deliberately
  disabled after recovery.
- 2026-05-08: Added `v0.26.128.13`: after subscription recovery drains all
  subscriptions, queue a narrow retained recovery refresh for `/meta`, device
  heartbeat, sensor availability, and switch availability. This reuses the
  retained refresh experiment from `128.10`, but now runs with the corrected
  `128.12` NTP gate so NTP is deferred until both publish and subscription
  queues are empty. It still avoids `/data`, switch state, discovery, and full
  startup replay.
- 2026-05-08: User tested `v0.26.128.13` starting at 13401 line 6590 and
  terminated the run after network errors; broker logs showed no MQTT messages.
  Serial showed the original startup publish batch returned success, then the
  first sensor config subscribe failed after about 10 seconds. MQTT-only
  recovery reconnected and `post_connect` drained all four subscriptions in
  27 ms. The retained recovery refresh queued `/meta`, heartbeat, sensor
  availability, and switch availability; each retained publish returned success
  and NTP was correctly deferred until the publish queue reached `pub=0
  sub=0`. Immediately after that, DNS/NTP failed with
  `socket_resolve_host() returned -2` and NTP timeout. Current conclusion:
  the corrected NTP gate did its job, so NTP is not racing the retained
  recovery refresh. The retained post-recovery publish replay itself is still
  unsafe on this cold-start socket path and should be removed again while
  keeping the MQTT-only subscribe-failure recovery and NTP deferral
  instrumentation.
- 2026-05-08: Added `v0.26.128.14` experiment:
  - NTP is disabled entirely for this build, not merely deferred, so NTP cannot
    use the shared socket path during startup or recovery.
  - After subscribe-failure recovery drains subscriptions, the app no longer
    queues retained `/meta` and availability immediately. It logs
    `action=wait_for_good_publish` and waits for a later successful
    non-retained `/data` publish sync with no subscription backlog.
  - Only after that good publish signal does the app queue the narrow retained
    startup refresh: `/meta`, `/status/heartbeat`, sensor availability, and
    switch availability.
  - This tests whether the recovered socket path can become safe after normal
    post-recovery publishing, instead of replaying retained startup topics
    immediately after SUBACK recovery.
- 2026-05-08: User tested `v0.26.128.14`. Broker logs showed only one
  post-recovery `/data` so far:
  `2026-05-08T13:55:09-0600 nodus/co2-ykdvea/data ... timestamp=946684875`.
  Serial showed the experiment gate worked as intended: NTP was disabled,
  the first subscribe failed, MQTT-only recovery reconnected, `post_connect`
  drained all four subscriptions, and the app logged
  `action=wait_for_good_publish`. At `67s`, a non-retained `/data` publish
  flushed successfully and the app queued the retained refresh. Serial then
  showed successful local publish returns for `/meta`, `/status/heartbeat`,
  sensor availability, and switch availability from `68s` through `72s`, and
  another `/data` publish at `128s`. Current conclusion: waiting for one good
  `/data` publish and disabling NTP is not sufficient to make the retained
  refresh broker-visible. The recovered path can deliver at least one
  non-retained data publish while still failing to deliver the subsequent
  retained startup refresh.
- 2026-05-08: Backed out the `v0.26.128.14` experiment in `v0.26.128.15`.
  NTP is back in the normal deferred-then-sync path. The delayed
  `wait_for_good_publish` retained refresh was removed, returning recovery to
  the stable MQTT-only subscribe-failure behavior: after recovery drains
  subscriptions, duplicate startup is suppressed and steady-state can resume
  `/data`, periodic `/status/heartbeat`, and periodic availability. Current
  framing: the required missing broker-visible startup piece for Sensorius is
  `/meta`; NTP instantiation is not the evidence-backed cause of socket
  corruption.
- 2026-05-08: Added `v0.26.128.16` instrumentation hook for the retained
  startup refresh helper. When that replay/probe helper is used, callers can
  provide an availability debug logger so the Nodus serial log can print the
  exact retained sensor/switch `/availability` payloads before they are queued.
  This does not re-enable retained replay in the current `.15` recovery path;
  it prepares the next replay/probe experiment to verify the small
  availability packets on-device before considering broker/tcpdump analysis.
- 2026-05-08: Added `v0.26.128.17` MQTT round-trip experiment without retained
  replay. After subscribe-failure recovery drains subscriptions, the app queues
  a non-retained subscribe-to-self probe on
  `nodus/<device_id>/debug/echo`, publishes a tiny payload such as `rt:2`, and
  waits up to 30 seconds for the broker to echo that same payload through the
  normal `on_message` path. Serial markers:
  `roundtrip phase=subscribe_queued`, `publish_queued`, `publish_sent`, and
  either `echo_received` or `timeout`. This tests broker-mediated publish and
  receive health after recovery without touching retained `/meta` or retained
  availability replay.
- 2026-05-08: Local Adafruit CircuitPython `.py` bundle source is available at:
  `/Users/twfarley/Projects/pico_libs/circuitPython_9.2.8/libs/adafruit-circuitpython-bundle-py-20250314/lib`.
  Use this for future MiniMQTT source/signature inspection instead of the
  project-local `lib/adafruit_minimqtt/*.mpy` files.
- 2026-05-08: MiniMQTT API check from the local `.py` bundle:
  - `MQTT.publish(topic, msg, retain=False, qos=0)` supports `qos=1` and waits
    for broker `PUBACK` packet type `0x40` before returning.
  - `MQTT.subscribe(topic, qos=0)` supports a string, `(topic, qos)` tuple, or
    list of `(topic, qos)` tuples and waits for `SUBACK`.
  - `MQTT.ping()` sends `PINGREQ` and waits for `PINGRESP`.
  - `loop()` handles keepalive by calling `ping()` and returns packet types
    received while polling.
  - For this debugging path, QoS 1 publish is the direct broker ACK probe;
    subscribe-to-self remains the stronger routing/receive proof.
- 2026-05-08: User tested `v0.26.128.17`. Serial shows the cold-start
  retained startup publishes still returned local success before the first
  subscribe timed out after about 10 seconds. MQTT-only recovery then rebuilt
  the client on the existing ready network/socket pool, reconnected, and
  `post_connect` drained all four normal subscriptions. The new
  subscribe-to-self probe then subscribed to `nodus/co2-ykdvea/debug/echo`,
  published non-retained payload `rt:2`, and received the same payload back
  through `on_message` after 3.5 seconds. NTP also synced immediately after
  the probe publish, and normal `/data`, `/status/heartbeat`, and
  `/availability` publishes continued. Current conclusion: after recovery the
  MQTT path can complete broker-mediated subscribe, publish, route, and receive
  without retained replay. The remaining startup problem is narrower than
  general socket corruption: broker-visible retained startup `/meta` remains
  the missing Sensorius-critical piece, while retained replay experiments have
  been unsafe or broker-invisible.
- 2026-05-08: Added `v0.26.128.18` fake-small-meta round-trip experiment.
  This replaces the debug echo topic with the real device `/meta` topic, but
  keeps the publish non-retained and tiny:
  `{"schema":"nodus-meta/v1","device_id":"...","type":"nodus","probe":"rt:N"}`.
  The goal is to isolate topic/payload class from payload size and retained
  behavior. If this echo succeeds, the recovered path can route a small
  `/meta`-topic publish and receive it back; the remaining suspect becomes the
  retained flag, the large full-meta payload, or their cold-start timing. The
  publish is deliberately non-retained so it does not overwrite the retained
  production metadata on the broker.
- 2026-05-08: User tested `v0.26.128.18`. External MQTT capture showed the
  fake small `/meta` publish was broker-visible:
  `2026-05-08T14:42:50-0600 nodus/co2-ykdvea/meta {"schema":"nodus-meta/v1","device_id":"co2-ykdvea","type":"nodus","probe":"rt:2"}`.
  A normal `/data` publish followed at `14:43:32`. Current conclusion: the
  recovered path can publish on the real `/meta` topic when the payload is
  small and non-retained. This strengthens payload size as a suspect, while
  retaining the retained flag as a secondary variable that still needs isolated
  testing.
- 2026-05-08: User clarified the current `switch.toml` test configuration is
  a modified S1-only switch setup, not the maximum S1+S2 metadata case. A prior
  no-switch cold-start experiment appeared successful. This means the signal is
  not simply "largest possible metadata fails"; switch presence itself remains
  important because it changes both metadata size and startup subscription/state
  behavior. The next size test should state the active switch shape explicitly
  (`no switch`, `S1 only`, or `S1+S2`) before comparing outcomes.
- 2026-05-08: User tested `v0.26.128.18` with sensor-only/no `switch.toml`.
  Serial tail shows `switch enabled=False channels=0`, startup queued only the
  three sensor/device subscriptions, and retained startup `/meta` was 1245
  bytes. The MQTT broker captured normal startup `/meta`, `/status/heartbeat`,
  `/availability`, and `/data` at `14:48:38` through `14:48:42`. Serial then
  showed all three subscriptions drained in one sync pass with `errors=none`,
  followed by successful NTP sync. This contrasts with the immediately prior
  S1-only run, where retained `/meta` was 1646 bytes, four subscriptions were
  queued, and the process blocked in the first subscribe until user interrupt.
  Current conclusion: switch-enabled cold startup is the discriminator. The
  failure may be caused by the additional switch subscription/state publishes,
  the approximately 400-byte larger metadata payload, or the combined
  cold-start sequence; sensor-only startup is healthy.
- 2026-05-08: Added `v0.26.128.19` switch-subscription deferral experiment.
  Startup publishes remain unchanged for switch-enabled devices, including full
  switch metadata, switch availability, and switch state. Initial startup
  subscriptions are limited to device-level topics (`config/set`,
  `calibration/set`, and `fwupdate`). Once those drain and the MQTT publish and
  subscription queues are empty, the app queues switch channel subscriptions
  separately and logs `mqtt switch_subscriptions queued ...`. This isolates the
  extra switch `config/set` subscription from the switch metadata and retained
  startup publish sequence.
- 2026-05-08: User tested `v0.26.128.19` with minimal S1-only switch config.
  Serial showed switch channel subscription was successfully deferred: startup
  queued only the three device-level subscriptions while still publishing the
  full switch-enabled startup batch (`/meta` 1645 bytes, heartbeat, sensor
  availability, `S1` availability, `S1` state, and `/data`). The first
  device-level subscribe still timed out after about 10 seconds on
  `nodus/co2-ykdvea/config/set`. Recovery then succeeded, and the fake small
  non-retained `/meta` probe was broker-visible at `14:59:47`, followed by
  normal `/data` at `15:00:29`. Current conclusion: the switch channel
  subscription itself is not required to trigger the cold-start failure. The
  remaining switch-enabled startup variables are the larger full `/meta`
  payload and the extra retained switch availability/state publishes before
  subscription begins.
- 2026-05-08: Added `v0.26.128.20` experiment. It keeps the `.19`
  switch-subscription deferral and keeps full switch-enabled `/meta` in the
  startup batch, but skips only the retained switch startup topics
  (`S1/availability` and `S1/state`) during the initial startup publish cycle.
  Sensor/device startup publishes remain unchanged. This isolates the extra
  retained switch publishes from the larger switch-enabled `/meta` payload.
- 2026-05-08: User tested `v0.26.128.20` with minimal S1-only switch config.
  Serial confirmed `switch enabled=True channels=1` and showed startup queued
  only three device subscriptions. Startup publish queue was reduced to the
  full switch-enabled `/meta` (`1645` bytes), device heartbeat, sensor
  availability, and `/data`; there were no startup publishes for
  `S1/availability` or `S1/state`. The first device-level subscribe still
  timed out after about 10 seconds on `nodus/co2-ykdvea/config/set`. Recovery
  succeeded and the fake small non-retained `/meta` probe was broker-visible at
  `15:04:19`, followed by normal `/data`, heartbeat, and availability. A later
  `S1/availability` publish at `15:06:04` came from the periodic availability
  refresh after recovery, not the startup batch. Current conclusion: extra
  retained switch startup availability/state publishes are not required to
  trigger the cold-start failure. The remaining suspect is the full
  switch-enabled `/meta` payload itself: size, content, retained publish, or
  its cold-start timing before the first subscribe.
- 2026-05-08: Broker capture interpretation note. A later broker capture at
  `15:09:51` showed `/meta` with version `v0.26.128.18` plus retained
  `S2-ykdvea` availability/state even though the current on-device run was
  `.20` with minimal S1-only config. These are retained broker messages
  delivered when `mosquitto_sub` subscribed, not evidence that the current
  `.20` startup published S2 or old-version metadata. Future broker captures
  should either include retain markers in the format or use `mosquitto_sub -R`
  when only live messages are wanted.
- 2026-05-08: Added `v0.26.128.21` reduced switch `/meta` experiment. It
  keeps `.20` behavior: switch subscription is deferred and retained switch
  startup availability/state publishes are skipped. The startup `/meta` still
  reports `capabilities.switch=true`, keeps switch members in
  `location_group.members`, and includes a `switch` object with
  `device_id`, `location`, and `channel_count`, but omits the detailed
  `switch.channels` array. This isolates the detailed channel metadata block
  from the rest of switch-enabled startup.
- 2026-05-08: User tested `v0.26.128.21` with minimal S1-only switch config.
  Broker captured live startup `/meta` at `15:16:15` with version
  `v0.26.128.21`, `capabilities.switch=true`, `location_group.members`
  including `S1-ykdvea`, and reduced `switch` object
  `{device_id, channel_count, location}`. Serial showed reduced `/meta` size
  `1337` bytes, then heartbeat, sensor availability, and `/data`; the three
  device subscriptions drained successfully in one sync pass, deferred
  `S1-ykdvea/config/set` was then queued and drained successfully, and NTP
  synced without recovery. Current conclusion: the detailed
  `switch.channels` metadata block in retained startup `/meta` is the decisive
  trigger seen so far. Switch capability, S1 membership, switch availability,
  switch state, and switch subscription are all safe when the detailed channel
  block is omitted or deferred.
- 2026-05-08: User performed a second `v0.26.128.21` verification. Broker
  again captured the reduced live startup `/meta` at `15:20:31`, but serial
  showed the first device subscription still timed out after about 10 seconds
  on `nodus/co2-ykdvea/config/set`; recovery then ran, the fake small `/meta`
  probe was broker-visible at `15:20:46`, and `/data` resumed. Updated
  conclusion: reduced switch `/meta` makes startup `/meta` broker-visible and
  can allow a fully clean startup, but it does not deterministically eliminate
  the cold-start subscribe timeout. The detailed `switch.channels` block is a
  major contributor to broker invisibility and startup fragility, but there is
  still a cold-start timing/socket readiness component.
- 2026-05-08: User performed a third `v0.26.128.21` verification. Broker
  captured live reduced startup `/meta` at `15:23:27`, followed by startup
  `/status/heartbeat` at `15:23:28`, startup `/availability` at `15:23:29`,
  and startup `/data` at `15:23:30`. No fake `/meta` recovery probe appeared
  in the broker log for this run. Updated conclusion: the reduced switch
  `/meta` shape is repeatedly broker-visible and can support clean cold
  startup, but the second-run timeout means the first subscribe path is still
  intermittently sensitive to cold-start timing/socket readiness.
- 2026-05-08: Added `v0.26.128.22` experiment. Removed the fake `/meta`
  round-trip publication from the MQTT recovery path. Kept reduced switch
  `/meta` from `.21`. Added a narrow startup subscription settle gate: after
  the startup publish queue drains and device subscriptions are waiting, the
  main loop waits `3.0s` before flushing the first subscriptions. Serial logs
  `startup_subscribe_settle phase=start` and `phase=complete` around this
  delay. Current hypothesis: reduced `/meta` fixes broker-visible startup
  identity, while the remaining intermittent cold-start failure may be caused
  by subscribing immediately after the retained startup publish burst.
- 2026-05-08: Added `v0.26.128.23` cleanup. User observed
  `ntp phase=deferred reason=mqtt_startup_pending` after NTP had already
  synced, caused by periodic MQTT publish queues. Tightened the NTP startup
  guard so the MQTT-priority deferral applies only before the first successful
  NTP sync. After `ntp_state.phase == "synced"`, periodic heartbeat,
  availability, switch availability, and sensor publishes should not emit
  startup-related NTP deferral logs.
- 2026-05-08: User tested `v0.26.128.23` with minimal S1-only switch config.
  Broker captured the expected live startup sequence: reduced `/meta` at
  `15:30:25`, `/status/heartbeat` at `15:30:26`, `/availability` at
  `15:30:27`, and `/data` at `15:30:28`. No fake `/meta` probe appeared.
  Current conclusion: `.23` has the desired broker-visible startup shape for
  this run. Next evidence needed is repeated cold-start verification that the
  `3.0s` startup subscription settle prevents the intermittent first
  `config/set` subscribe timeout.
- 2026-05-08: User performed five quick `v0.26.128.23` startup tests, two in
  RWFS and the others in ROFS. Broker logs showed live reduced `/meta` for each
  run at approximately `15:30:25`, `15:32:29`, `15:33:22`, `15:35:11`, and
  `15:35:58`, followed by the normal startup heartbeat, availability, and/or
  data sequence. User-initiated restarts produced expected offline heartbeat
  messages between runs. Current conclusion from broker evidence: `.23`
  repeatedly restores broker-visible startup identity under quick cold-start
  cycling. Serial confirmation is still needed for whether any run hit the
  first-subscribe recovery path or whether the `3.0s` settle made the
  subscription phase clean.
- 2026-05-08: Serial review of the five quick `.23` startup tests. Runs at
  roughly `15:30`, `15:32`, `15:33`, and `15:35:11` showed
  `startup_subscribe_settle phase=start`, `phase=complete`, successful device
  subscriptions, deferred `S1-ykdvea/config/set` subscription, and NTP sync
  without MQTT recovery. The fifth run at roughly `15:35:58` showed settle
  start/complete, then the first device subscription still timed out on
  `nodus/co2-ykdvea/config/set` after about 10 seconds; MQTT-only rebuild then
  recovered, drained the three device subscriptions, suppressed duplicate
  startup replay, and normal publishes resumed. Updated conclusion: the `3.0s`
  settle improves the cold-start path but does not fully eliminate the
  intermittent first-subscribe timeout. Reduced `/meta` remains the key fix
  for broker-visible startup identity; subscribe readiness still needs either
  a stronger readiness gate or a more tolerant first-subscribe recovery path.
- 2026-05-08: Added `v0.26.128.24` timing experiment. No MQTT payload,
  subscription ordering, recovery, NTP, or switch metadata behavior changed.
  The only runtime behavior change is increasing
  `MQTT_STARTUP_SUBSCRIBE_SETTLE_S` from `3.0s` to `10.0s`. Test goal: decide
  whether the remaining intermittent first-subscribe timeout is simply a
  cold-start readiness/timing problem. Pass criteria are repeated cold starts
  with live reduced `/meta`, normal startup heartbeat/availability/data, settle
  start/complete logs, no `mqtt_subscribe_failed`, and no
  `recovery action=mqtt_rebuild`.
- 2026-05-08: User tested `.24` with three ROFS cold starts. Serial review:
  first run was clean (`delay_s=10.0`, device subscriptions drained, deferred
  `S1-ykdvea/config/set` drained, NTP synced). Second run failed after the
  `10.0s` settle on the first device subscription
  `nodus/co2-ykdvea/config/set`, rebuilt MQTT, failed the first post-connect
  subscription attempt once more, rebuilt again, then drained all device
  subscriptions and recovered with duplicate startup replay suppressed. Third
  run was clean. Current conclusion: a longer fixed settle does not eliminate
  the cold-start subscribe failure. The issue is not simply "wait longer after
  startup publishes"; the first subscribe path sometimes needs MQTT-client
  rebuild/reconnect before SUBACK reads work reliably.
- 2026-05-08: Added `v0.26.128.25` experiment. Changed only
  `MQTT_STARTUP_SUBSCRIBE_SETTLE_S` from `10.0s` to `0.0s`. Reduced `/meta`,
  deferred switch subscription, no retained replay, and MQTT-only subscribe
  recovery remain unchanged. Test goal: stop treating fixed delay as the
  primary mitigation and observe whether immediate subscription plus bounded
  MQTT-only recovery behaves no worse than the delayed path.
- 2026-05-08: User tested `.25` with five ROFS cold starts. Broker logs showed
  live reduced `/meta` for each run at approximately `15:51:59`, `15:54:28`,
  `15:55:52`, `15:57:22`, and `15:58:54`, followed by normal startup
  heartbeat, availability, and data. Serial alignment showed all five runs
  logged `startup_subscribe_settle phase=start delay_s=0.0` and immediate
  `phase=complete`; each then drained the three device subscriptions, queued
  and drained deferred `S1-ykdvea/config/set`, and synced NTP. No
  `mqtt_subscribe_failed`, no `recovery action=mqtt_rebuild`, and no retained
  replay occurred in these five `.25` starts. Current conclusion: fixed settle
  delay is not required for clean startup in this sample. The dominant fixes
  remain reduced switch `/meta`, deferred switch subscription, and safe
  MQTT-only recovery for the remaining intermittent subscribe failures.
- 2026-05-08: User restored the two-channel switch configuration and kept the
  shorter test cadence, waiting only for NTP and `/data`. Broker logs showed
  live reduced two-channel `/meta` at approximately `16:07:56`, `16:08:50`,
  and `16:09:21`. These payloads included
  `location_group.members=["co2-ykdvea","S1-ykdvea","S2-ykdvea"]` and
  `switch.channel_count=2`, but still intentionally omitted the detailed
  `switch.channels` array. Serial review showed the `16:07:56` run hit the
  original first device subscription timeout on
  `nodus/co2-ykdvea/config/set` after startup publishes drained; MQTT-only
  recovery rebuilt the client, drained device subscriptions, suppressed
  duplicate startup replay, and NTP synced. The following two-channel runs
  visible in the tail drained device subscriptions, queued both deferred
  switch subscriptions (`S1` and `S2`), drained them, and then synced NTP.
  Current conclusion: increasing reduced `/meta` from one switch channel to
  two switch channels does not reintroduce the broker-invisible startup
  publish problem. The remaining intermittent fault is still the first
  subscription/SUBACK receive path, not the reduced `/meta` payload size.
- 2026-05-08: User captured the `.25` two-channel corner case starting at
  serial line 8590 and left it running for post-recovery observation. Sequence:
  Wi-Fi joined on attempt 1, reduced two-channel `/meta` published
  broker-visible at startup (`bytes=1359`), heartbeat, availability, and first
  `/data` all published successfully, then the immediate first device
  subscription to `nodus/co2-ykdvea/config/set` timed out after about 10
  seconds with `No data received from broker for 10 seconds`. MQTT-only
  recovery rebuilt the client while preserving the ready network phase and
  IP address, reconnected to `10.0.0.248` in about `1.1s`, drained the three
  device subscriptions successfully in `post_connect`, suppressed duplicate
  startup replay, and then NTP synced. The first observed post-recovery
  periodic publish was `/data` at `16:17:35` with queues drained. Current
  conclusion: this is the narrow remaining failure mode. Startup publish
  visibility is healthy; subscription ACK receive can still fail once after
  cold start; MQTT-only rebuild can recover without rebooting or replaying
  startup retained topics.
- 2026-05-08: Broker-side continuation of the line-8590 corner case confirmed
  post-recovery MQTT health. After the startup `/meta`, heartbeat,
  availability, and first `/data`, the first post-recovery `/data` appeared at
  `16:17:36`. Periodic heartbeat and sensor availability resumed at
  `16:18:34` and `16:18:35`; both switch availabilities appeared at
  `16:18:37` (`S1`) and `16:18:38` (`S2`); another `/data` followed at
  `16:18:39`. Updated conclusion: in this observed failure case, MQTT-only
  rebuild restored normal periodic sensor, heartbeat, sensor availability, and
  switch availability publishing without a reboot.
- 2026-05-08: Contract direction captured for Sensorius. Keep retained startup
  `nodus/<device_id>/meta` compact and broker-visible, including switch
  presence via `switch.device_id`, `switch.location`, `switch.channel_count`,
  and location-group members. Move detailed switch channel control topics out
  of startup `/meta` into retained `nodus/<device_id>/meta/switch`, published
  after MQTT startup subscriptions are healthy. Updated public docs:
  `docs/sensorius_contract.md`, `docs/mqtt.md`, `docs/architecture.md`,
  `docs/onboarding.md`, `docs/onboarding_v2_sensorius_requirements.md`, and
  `docs/README.md`.
- 2026-05-08: Added contract guidance for Sensorius backward compatibility:
  compact startup `/meta` should identify the split with `switch.meta_topic`.
  Sensorius should parse older embedded `meta.switch.channels` first when
  present, then use `switch.meta_topic`, then fall back to the default
  `nodus/<device_id>/meta/switch` topic when `switch.channel_count > 0`.
- 2026-05-09: Overnight `.25` soak review from serial logs. `co2-ph244`
  (`cu.usbmodem13301.log` from line 7479) had one autonomous soft reboot.
  It recorded ten MQTT recovery starts including boot/post-reboot recovery,
  nine successful MQTT connects, thirty-six connect errors clustered in one
  long failure window around `2026-05-08 20:42:43` through `20:45:39`, and no
  subscription ACK failures. The reboot cause was MQTT recovery timeout after
  repeated broker connect failures, not a traceback or heap exhaustion. Periodic
  GC recovered free memory to roughly `148132..154000` bytes, with no pre-GC
  sample below `10000` bytes.
- 2026-05-09: `co2-ykdvea` (`cu.usbmodem13401.log` from line 8663) was much
  chattier: five boot records in the requested range, four autonomous soft
  reboots, forty MQTT recovery starts, thirty-eight successful MQTT connects,
  one hundred fifty-seven connect errors, and two subscription ACK failures
  that recovered via MQTT rebuild. Soft reboots occurred after repeated connect
  failures at about `2026-05-08 20:09:35`, `23:30:39`, `2026-05-09 02:56:25`,
  and `04:58:20`. Subscription ACK failures occurred at `02:34:31` on the
  `fwupdate` subscription and `05:14:59` on the `config/set` subscription; the
  latter recovered and resumed data, heartbeat, sensor availability, and switch
  availability publishes. Periodic GC recovered free memory to roughly
  `146544..152784` bytes, but pre-GC free memory dipped below `10000` bytes
  nine times, with a minimum observed `4992` bytes. Current conclusion: `.25`
  fixed startup `/meta` visibility, and the remaining restart signature is
  MQTT/network/socket instability: repeated MQTT connect failures and occasional
  SUBACK receive timeouts. Heap remains telemetry to watch, but after-GC free
  memory is healthy and the logs do not point to heap exhaustion as the restart
  cause.
- 2026-05-09: Implemented the split metadata contract in `v0.26.129.1`.
  Compact retained `/meta` now includes `switch.meta_topic` when switch
  channels are present and continues to omit `switch.channels` in the startup
  path. Added retained `nodus/<device_id>/meta/switch` payload generation with
  `schema=nodus-meta-switch/v1`, channel state, command/result/availability
  topics, and no hardware pin fields. Runtime queues `/meta/switch` only after
  the current MQTT generation drains its startup device subscriptions; the
  delay gate is `MQTT_SWITCH_META_DELAY_S = 0.0` initially. Also fixed the
  subscribe-failure recovery path so, after recovered device subscriptions
  drain, switch command subscriptions are requeued for the recovered MQTT
  generation instead of being silently skipped. Verification:
  `pytest tests` passed with 321 tests.
- 2026-05-11: Compared the two Sensorius broker soak logs for correlation
  between devices using a `+/-60s` recovery-event window. `aht-rvwi73`
  (`cu.usbmodem13301.log` from line 144, broker `samhain.local` with persisted
  `10.0.0.248`) had a dated event span from `2026-05-10 15:37:33` through
  `2026-05-11 05:59:42`, with 43 MQTT recovery starts, 2 subscribe-failure
  MQTT rebuilds, 48 MQTT connect errors, 51 network reconnect attempts during
  recovery, 0 explicit Wi-Fi recovery phase entries, and 4 recovery-triggered
  soft reboots. The soft reboots occurred at `18:09:38`, `21:43:34`,
  `00:38:17`, and `03:15:46`. `co2-ykdvea` (`cu.usbmodem13401.log` from line
  207, broker `10.0.0.248`) had a dated event span from `2026-05-10 15:19:06`
  through `2026-05-11 06:05:59`, with 57 MQTT recovery starts, 4
  subscribe-failure MQTT rebuilds, 90 MQTT connect errors, 72 network reconnect
  attempts during recovery, 0 explicit Wi-Fi recovery phase entries, and 7
  recovery-triggered soft reboots. The soft reboots occurred at `19:25:57`,
  `21:20:56`, `21:27:02`, `23:10:08`, `02:07:12`, `04:06:33`, and `06:05:40`.
  Only five cross-device recovery-start matches fell within `+/-60s`:
  `16:01:26` vs `16:01:10`, `21:16:36` vs `21:16:00`, `00:47:41` vs
  `00:47:18`, `05:23:52` vs `05:23:48`, and `05:51:46` vs `05:51:44`. These
  were all MQTT phase starts, not the longer soft-reboot spirals. There were
  zero soft-reboot matches within `+/-60s`. Current conclusion: this does not
  show strong broker-wide timing correlation. The longer recovery spirals and
  warm restart paths do not line up between devices, which supports the
  per-device Pico/CYW43/socket/MiniMQTT-state hypothesis over a shared
  `samhain.local` or `10.0.0.248` broker outage hypothesis.
- 2026-05-11: Compared current working tree behavior against the stable
  `v0.26.125.7` baseline commit
  `64453fee358ce803b132c9a088515c01a295b261`. The stable
  `co2-ykdvea` slice in `cu.usbmodem13401may08-10.log` lines 230-1202 spans
  about `39h45m` from `2026-05-05 15:27:33` through
  `2026-05-07 07:12:25` with one expected startup MQTT rebuild/connect, `478`
  `mqtt_connected=True` health lines, and no MQTT connect errors, MQTT sync
  errors, recovery soft reboots, runtime reloads, or tracebacks. The current
  `v0.26.130.1` `co2-ykdvea` log from line 207 spans about `14h47m` and shows
  repeated MQTT recovery starts, connect errors, subscribe-failure rebuilds,
  and recovery soft reboots. This supports classifying the current behavior as
  a stability regression, even though recovery is more active.
- 2026-05-11: Diff review identified the likely stability regression as
  increased MQTT churn rather than MQTT timeout length. `v0.26.125.7` drained
  queued publishes before attempting subscriptions and treated slow successful
  MiniMQTT operations as success. Current code prioritized retained identity
  publishes, then attempted subscriptions while low-priority publishes could
  remain queued; logs show subscribe failures with `before=queues pub=1 sub=4`.
  Current code also treated slow successful publish/subscribe operations over
  `10000ms` as transport failures. The clearest example is
  `co2-ykdvea` line 901: retained switch availability published after about
  `29906ms`, but firmware marked it `mqtt_publish_slow`, disconnected, and
  entered repeated connect failures. Current conclusion: the unstable behavior
  is a feedback loop: slow publish/subscribe or SUBACK timeout -> mark MQTT
  disconnected -> rebuild/reconnect -> replay startup retained work and
  subscriptions -> more MiniMQTT/socket pressure -> possible MQTT recovery
  timeout and warm reboot. This is not primarily a timeout-length issue.
- 2026-05-11: Implemented `v0.26.131.1` to reduce MQTT churn without extending
  the recovery timeout. Runtime now drains queued publishes in queue order
  before attempting subscriptions, matching the stable `v0.26.125.7` ordering.
  Slow successful publish/subscribe operations are logged as
  `mqtt_publish_slow` or `mqtt_subscribe_slow` warnings with `phase=synced`;
  they no longer mark the transport disconnected. Real publish/subscribe
  exceptions still mark MQTT disconnected. Immediate subscribe-failure recovery
  now rebuilds socket artifacts via `reconnect_network_stack(...,
  rebuild_socket_artifacts=True)` before creating the replacement MQTT adapter,
  so the immediate path is closer to the normal timed MQTT recovery rebuild.
  The MQTT recovery timeout was intentionally not changed in this fix.
- 2026-05-11: Added `v0.26.131.4` stability experiment controls for the next
  125.7 comparison soak. MQTT `logs/get` subscription is disabled and retained
  `/meta` now advertises `capabilities.log_transfer=false` so the extra
  diagnostic subscription introduced after 125.7 is not part of the MQTT
  startup shape. Learned `.local` broker IP persistence is disabled for this
  test; manually configured `BROKER_IP` remains available as the explicit
  fallback, but successful hostname resolution no longer writes back to
  `settings.toml`. The immediate MQTT adapter rebuild after every connect
  error was removed from the runtime path; connect errors now log
  `action=retry_without_rebuild` and leave rebuilds to subscribe-failure
  recovery or the normal timed recovery policy. Added light serial diagnostics:
  the boot marker reports the experiment flags, connect logs include broker
  target state and persistence status, MQTT sync logs include connection
  generation and connection age, failure logs include last successful MQTT
  operation detail, and recovery phase/soft-reboot logs include per-boot MQTT
  counters plus pre-reboot memory.
- 2026-05-12: Current `v0.26.132.3` `co2-ykdvea` run confirmed a remaining
  MiniMQTT/socket compatibility failure rather than a broker, DNS, publish, or
  recovery-timeout failure. The boot begins at `cu.usbmodem13401.log` line 5779
  and connects at line 5791. At line 5819, MQTT poll fails after about
  `235812ms` connected with `client_conn=true`, `sock=present`, `sock=wrapped`,
  and `backcompat=1`: `function takes 3 positional arguments but 2 were given`.
  The last successful publish was `nodus/co2-ykdvea/data` about `54s` earlier,
  and the last sync had no queued MQTT operation about `320ms` earlier. Recovery
  started immediately at line 5820, reconnected at line 5824, and publications
  resumed at line 5829. `v0.26.132.4` changes `_MiniMQTTSocketCompat.recv()` to
  use the normalized `recv_into(buffer, nbytes)` path first, avoiding raw socket
  `recv()` implementations that still throw the MiniMQTT arity error. Added a
  regression test for a socket whose `recv()` throws the exact arity error while
  `recv_into(buffer, nbytes)` succeeds. Verification: `ruff check
  cpynodus_ii/core/mqtt_client.py tests/test_mqtt_client_adapter.py
  cpynodus_ii/__init__.py`, `pytest tests/test_mqtt_client_adapter.py`, and
  `pytest tests` all passed.
- 2026-05-12: `v0.26.132.4` still reproduced the same poll arity error at
  `cu.usbmodem13401.log` line 5925 after about `224005ms` connected:
  `sock=wrapped backcompat=1 error=function takes 3 positional arguments but 2
  were given`. This proves the 132.4 raw-`recv()` preference fix was incomplete.
  The broker log shows publications resumed immediately after recovery: retained
  `/meta` at `08:06:44`, `/data` at `08:06:45`, heartbeat at `08:06:47`,
  availability at `08:06:48`, and `/meta/switch` at `08:06:50`. `v0.26.132.5`
  now patches MiniMQTT's instance `_sock_exact_recv` after connect so the poll
  read path goes through `_MiniMQTTSocketCompat.recv_exact()` and normalized
  `recv_into(buffer, nbytes)`, bypassing MiniMQTT's `recv()` exact-read
  branch. The MiniMQTT socket error string now includes `exact`, `sock_op`,
  `sock_nbytes`, and `sock_error` so any remaining arity failure identifies the
  last wrapped-socket operation. Verification: `ruff check
  cpynodus_ii/core/mqtt_client.py tests/test_mqtt_client_adapter.py
  cpynodus_ii/__init__.py`, `pytest tests/test_mqtt_client_adapter.py`, and
  `pytest tests` all passed.
- 2026-05-12: `v0.26.132.5` still reproduced the poll arity error at
  `cu.usbmodem13401.log` line 6122 after about `235969ms` connected, but the
  added diagnostics narrowed it further: `exact=1 sock_op=recv_into
  sock_nbytes=1 sock_error=none`. This means the patched exact-read path is in
  use and the failing operation is the 1-byte poll read, which is the path that
  should normally become "no packet available". `v0.26.132.6` now treats an
  exhausted recv-compat arity failure during exact receive as `ETIMEDOUT`, and
  poll-level `ETIMEDOUT`/`EAGAIN` is handled as `phase=polled` instead of
  disconnecting MQTT. This keeps real publish/subscribe/connect exceptions as
  failures while stopping the no-data poll path from forcing an MQTT rebuild.
  Verification: `ruff check cpynodus_ii/core/mqtt_client.py
  tests/test_mqtt_client_adapter.py cpynodus_ii/__init__.py`, `pytest
  tests/test_mqtt_client_adapter.py`, and `pytest tests` all passed.
- 2026-05-12: `v0.26.132.6` still showed poll arity recoveries on both
  `aht-rvwi73` and `co2-ykdvea`: the error reached the outer
  `poll_mqtt_client()` TypeError handler before it was converted to
  `ETIMEDOUT`, with diagnostics still showing `exact=1 sock_op=recv_into
  sock_nbytes=1`. `v0.26.132.7` adds an outer poll guard for this exact
  condition: patched MiniMQTT exact receive is active, the socket is wrapped,
  no message was received during the poll, and the last socket operation was a
  1-byte `recv_into`. That condition is now treated as `phase=polled` with no
  errors instead of `mqtt_poll_socket_type`, so it should not produce the
  serial disconnect/rebuild line for this no-data poll path. Verification:
  `ruff check cpynodus_ii/core/mqtt_client.py tests/test_mqtt_client_adapter.py
  cpynodus_ii/__init__.py`, `pytest tests/test_mqtt_client_adapter.py`, and
  `pytest tests` all passed.
- 2026-05-12: `v0.26.132.7` proved that recovery instrumentation and poll
  arity suppression were not the same as restoring 125.7 stability. The
  `co2-ykdvea` `v0.26.132.7` run starting at `cu.usbmodem13401.log` line 6370
  ran cleanly at startup but then produced repeated publish failures with
  `[Errno 9] EBADF`, subscribe failures, and eventually an MQTT recovery timeout
  soft reboot. This is still a stability regression relative to the 125.7 run,
  which did not need recurring MQTT recovery after startup. `v0.26.132.8`
  rolls back the extra MiniMQTT intervention added during the poll-arity
  investigation: Nodus no longer monkey-patches MiniMQTT `_sock_exact_recv`,
  no longer forces `_backwards_compatible_sock=True`, no longer suppresses the
  1-byte poll arity TypeError as a successful no-data poll, and
  `_MiniMQTTSocketCompat.recv()` once again delegates to the wrapped socket's
  `recv()` method. The retained behavior is only the small socket method
  signature bridge plus diagnostic `sock_op`, `sock_nbytes`, and `sock_error`
  logging. The purpose of the next run is to test whether the current firmware
  can return to the 125.7 no-recovery steady state, not whether recovery can
  compensate for MQTT/socket instability.
