# MQTT Merge Redo Plan - 2026-05-20

## Goal

Redo the post-`v0.26.139.3` merge into `trunk` without losing broker-visible
MQTT discovery and identity publishing.

The protected behavior is:

- Sensor devices publish retained `/meta`.
- Switch-capable devices publish retained `/meta` and `/meta/switch`.
- `/data` continues at the configured sensor cadence.
- `/status/heartbeat` and `/availability` continue at the configured status
  cadence.
- Recovery may occur, but recovery must restore broker-visible identity
  messages, not only data publishing.

## Current Branch State

Primary working tree:

- Path: `/Users/twfarley/Projects/cPyNodus_II`
- Branch: `mqtt-merge-redo-from-139.3`
- Base commit: `9db8189`
- Base version: `v0.26.139.3`

Archived failed merge/debug work:

- Branch: `archive/post-merge-mqtt-debug-2026-05-20`
- Commit: `748c459`

Disposable reference checkout:

- Path: `/Users/twfarley/tmp/cPyNodus_II-139.3`
- Detached at `9db8189`
- Remove after the redo branch no longer needs it for comparison.

## Known Evidence

`v0.26.139.3` is pre-merge firmware. It can have MQTT recoveries, but the
broker-visible MQTT contract remains intact. In the overnight run, `139.3`
devices continued producing `/meta`, `/meta/switch` where applicable, data,
heartbeat, and availability after recovery.

`v0.26.139.4` is post-merge firmware. The most significant regression is that
`co2-frank` produced data, heartbeat, and availability, but the broker capture
did not show `/meta`. Serial logs showed `/meta` as published, so serial-side
`published=1` is not a sufficient pass condition.

## 2026-05-20 Warm-Boot Debug Log

Primary test device:

- Device: `co2-frank`
- Location: `hub-1`
- IP: `10.0.0.219`
- Broker: `10.0.0.248` / `samhain.local`
- Serial log: `/Users/twfarley/nodus.log`
- MQTT capture topic root: `nodus/co2-frank`

Important framing:

- Cold starts working is a key signal. When cold start succeeds, broker,
  credentials, retained topics, startup publish order, and basic MQTT contract
  are not the primary fault.
- The failure under investigation is warm-start / soft-reboot MQTT reconnect
  and MiniMQTT stack behavior, not a general broker outage.
- The `soft reboot` banner alone is not enough to identify the path. We need
  to track whether the reload was caused by CircuitPython auto-reload, REPL
  `Ctrl-D`, firmware `supervisor.reload()`, or a prior `KeyboardInterrupt`.
  The latest evidence shows these paths do not all leave the network/MQTT stack
  in the same condition.
- In ROFS mode the Nodus runtime cannot write to CIRCUITPY. If CircuitPython
  prints `Code stopped by auto-reload. Reloading soon.`, the filesystem event is
  host-side or deploy-side, not a normal Nodus app write. The firmware bug is
  allowing host-side auto-reload to interrupt runtime while the device is
  operating in host-visible ROFS/edit mode.

### Baseline and Test Harness

- Copied `testApparatus/platform_test.py` from
  `archive/post-merge-mqtt-debug-2026-05-20`.
- Platform-test MQTT topics observed:
  - `nodus/co2-frank/platform_test/direct_plain`
  - `nodus/co2-frank/platform_test/direct`
  - `nodus/co2-frank/platform_test/firmware`
  - `nodus/co2-frank/platform_test/direct_wrapped`
  - later matrix entries include `raw_mqtt`, `firmware_direct_loop`, and
    `firmware_wrapped`.
- Broker capture command was extended to include platform-test topics plus
  `config/*`, `calibration/*`, and `fwupdate/*`.

### Findings and Fix Attempts

1. `v0.26.139.3` / early redo testing exposed a warm-start failure where repeated
   `mqtt_connect_failed:10.0.0.248:('Connect failure', None)` could persist
   across soft reboots.

2. Soft-reboot cleanup was strengthened:
   - `close_mqtt_client()` and `disconnect_mqtt_client()` now force-close the
     MiniMQTT socket even when MiniMQTT refuses a normal disconnect.
   - Recovery soft reboot prep now tears down MQTT and cycles Wi-Fi radio.
   - Host tests cover forced socket close and radio-cycle teardown.

3. Platform tests showed direct MiniMQTT paths could publish while firmware
   adapter paths exhausted pystack:
   - `direct_plain` and `direct` passed.
   - `direct_wrapped` repeatedly failed during poll with `RuntimeError: pystack
     exhausted`.
   - Firmware adapter initially failed in connect/publish/poll depending on the
     version under test.

4. `v0.26.140.1` through `v0.26.140.7` reduced adapter call depth in stages:
   - opt-in socket wrapper instead of always wrapping;
   - fixed-arity MQTT callback by default;
   - leaner single-target connect path;
   - lean poll path;
   - removed last-publish diagnostic recording from the hot path;
   - split publish from sync bookkeeping.
   Result: failures moved but persisted. Broker-visible direct publish was
   possible, but firmware adapter still hit pystack exhaustion.

5. `v0.26.140.8` added a raw QoS0 MQTT publish path for connected MiniMQTT
   sockets:
   - `sync_transport_to_client()` bypasses `client.publish()` when `_sock` has
     `send`.
   - It manually builds and sends a minimal QoS0 PUBLISH packet.
   - Fallback remains `client.publish()` for host fakes and non-socket clients.
   Result: this targets publish stack depth, but the next platform run showed
   the current failure had moved earlier to subscribe, so publish was not yet
   reached in firmware mode.

6. `v0.26.140.9` fixed the normal shutdown path exposed by serial lines
   `4021+` and `4113+`:
   - normal `finally` shutdown now calls network teardown with
     `cycle_radio=True`, matching recovery reboot prep.
   - Evidence: line `4154` and later line `4177` show
     `runtime network action=teardown result=1 cycle_radio=1`.
   - After line `4154`, the following warm/auto-reload boot connected MQTT at
     line `4172` and broker showed `/meta`, `online`, `/availability`, and
     several `/data` publishes.
   Result: warm restart after strong teardown is substantially improved.

7. `v0.26.140.10` added a raw QoS0 MQTT subscribe path:
   - `sync_transport_to_client()` bypasses `client.subscribe()` when `_sock`
     supports `send` and `recv` / `recv_into`.
   - It sends a minimal SUBSCRIBE packet, reads the SUBACK immediately, and
     validates packet id and return code.
   - This directly targets the latest platform-test failure at line `4265`:
     `mqtt_subscribe_failed:...:pystack exhausted`.

8. `v0.26.140.11` disables CircuitPython auto-reload during runtime:
   - `boot.py` disables auto-reload before `code.py`.
   - `code.py` repeats the guard when firmware starts.
   - This is not because ROFS Nodus writes files. It prevents host-side
     CIRCUITPY writes or delayed deploy metadata writes from asynchronously
     stopping firmware while the device is running.
   - Explicit firmware reload/reset paths remain available and logged.

9. `v0.26.140.11` run starting at serial log line `4303` clarified the remaining
   failure:
   - Auto-reload is effectively resolved. The first banner still prints
     `Auto-reload is on`, but after firmware starts, later reload prompts show
     `Auto-reload is off` and no `Code stopped by auto-reload` event occurs.
   - Cold/manual start still succeeds. The run at line `4334` connects MQTT at
     line `4343`, and the broker captures `/meta`, online heartbeat,
     `/availability`, and `/data`.
   - Manual soft reboot still reproduces `mqtt_connect_failed` at line `4379`:
     `mqtt_connect_failed:10.0.0.248:('Connect failure', None)`.
   - Platform test still passes `raw_mqtt`, `direct_plain`, and `direct`, but
     firmware adapter subscribe still fails with
     `mqtt_subscribe_failed:...:pystack exhausted`.
   - This suggests the raw subscribe path is not being used on-device, likely
     because the socket capability gate is falling back to MiniMQTT
     `client.subscribe()`.
   - Auto-reload-triggered soft restart has been observed to publish `/meta`,
     heartbeat, availability, and `/data`, while REPL/manual soft reboot after
     an interrupted run can still fail MQTT connect. Treat those as separate
     test cases.

10. Serial log line `4500+` shows the firmware recovery soft-reboot path can
    enter a loop:
    - Boot at line `4502` reaches Wi-Fi/NTP but MQTT connect fails repeatedly.
    - At line `4528`, recovery triggers
      `action=soft_reboot reason=mqtt_repeated_connect_failures count=12`.
    - The recovery path tears down the network at line `4529`, calls
      `supervisor.reload()` at line `4530`, then the `finally` shutdown path
      tears down networking again at line `4531`.
    - The next boot at line `4540` repeats the same MQTT connect failure and
      triggers another recovery soft reboot at line `4566`.
    - This differs from the successful auto-reload case, which had one observed
      shutdown teardown before CircuitPython's delayed auto-reload and did not
      immediately re-enter the repeated-connect reboot loop.

11. `v0.26.140.12` changes the firmware recovery soft-reboot path to more
    closely match the successful auto-reload behavior:
    - Recovery soft reboot now performs one controlled MQTT/network teardown and
      marks shutdown as already prepared, so `main()` `finally` skips duplicate
      cleanup.
    - Recovery soft reboot waits briefly before calling `supervisor.reload()`,
      mimicking CircuitPython's delayed `Reloading soon` behavior.
    - Plain startup MQTT connect failures no longer trigger repeated recovery
      soft reboots when MQTT has not connected successfully in the current
      runtime. Those failures should remain in MQTT recovery/backoff instead of
      looping through `supervisor.reload()`.

12. `v0.26.140.12` device run showed the reboot loop was stopped, but the device
    stayed in passive MQTT recovery indefinitely:
    - At `12:11:09`, recovery elapsed was already `202s` with no reboot loop.
    - At `12:15:41`, recovery elapsed was `473s` and the device was still only
      logging `recovery mqtt action=hold_rebuild reason=broker_connect`.
    - Wi-Fi, DNS, and NTP stayed healthy, but MQTT never recovered.

13. `v0.26.140.13` keeps the no-loop behavior but makes plain startup MQTT
    connect failure recovery active:
    - After `180s` of plain MQTT connect failures with no MQTT success, recovery
      now closes MQTT, resets/cycles station networking, rebuilds socket
      artifacts, rebuilds the MQTT adapter, and immediately retries connect.
    - Further station resets are rate-limited to once every `180s`.
    - Expected log marker:
      `recovery mqtt action=station_reset reason=plain_connect_failure`.

14. Device run starting at serial log line `4606` clarified the `v0.26.140.13`
    result:
    - `v0.26.140.12` could still recover after a manual REPL reload in one case:
      line `4641` shows MQTT connected and broker logs show `/meta`, heartbeat,
      availability, and `/data`.
    - After another manual soft reboot, `v0.26.140.12` stayed in passive MQTT
      recovery from line `4679` through line `4734`.
    - `v0.26.140.13` performed the new active recovery step at line `4783`:
      `recovery mqtt action=station_reset reason=plain_connect_failure`.
    - It reconnected Wi-Fi and rebuilt socket/MQTT artifacts at lines
      `4784`-`4786`, but MiniMQTT connect still failed at lines `4787` and
      `4788`.
    - Correction from device operator: the following successful recovery at
      line `4819` was a power-cycle cold start, not a manual REPL soft reload.
      Broker logs show normal startup/data publishes for `v0.26.140.13` after
      that cold start.
    - Current inference: station/socket rebuild improves the recovery attempt,
      but it is not equivalent to a full cold start and is not sufficient for
      this warm-start MiniMQTT failure. If cold starts remain reliable, recovery
      probably needs a hard MCU reset escalation rather than repeated soft
      reloads.

15. Platform-test harness was extended to focus on the MiniMQTT connect failure:
    - `platform_test.py` version `v0.26.140.13.1` adds
      `mqtt_mode="connect_matrix"`.
    - The connect matrix avoids subscribe/poll and compares:
      `raw_mqtt`, `direct_plain_connect`, `direct_connect`,
      `direct_wrapped_connect`, `firmware_connect`, and
      `firmware_wrapped_connect`.
    - It logs GC memory and MiniMQTT `_sock` capability summaries around each
      connect attempt.
    - `reset_station=False` now preserves an existing Wi-Fi link when possible,
      making it better suited for testing immediately after a warm-start failure
      without masking state by resetting station first.

16. Planned cold/no-app baseline:
    - Rename device `code.py` to `nocode.py` from the host side, then power-cycle
      the Nodus.
    - With no `code.py`, CircuitPython should not auto-start the normal
      firmware app after cold boot. That allows `platform_test.py` to run from a
      cold device state without prior app MQTT/network setup.
    - This should distinguish "cold app start works" from "cold platform-test
      MiniMQTT works" and give a clean baseline for the connect matrix.

17. Cold/no-app baseline run starting at serial log line `4845`:
    - `code.py` was not present, so the normal app did not auto-start.
    - `platform_test.run(mqtt_mode="connect_matrix", scans=0,
      reset_station=False, mqtt_rounds=1)` was run twice.
    - No MQTT topic messages were expected on the broker capture because
      `connect_matrix` only opens MQTT connections and disconnects; it does not
      publish or subscribe to `nodus/...` topics.
    - First run result: `raw_mqtt`, `direct_plain_connect`, `direct_connect`,
      `direct_wrapped_connect`, and `firmware_connect` passed; only
      `firmware_wrapped_connect` failed with `pystack exhausted`.
    - Second run repeated the same result while preserving the existing Wi-Fi
      connection.
    - Current inference: cold/no-app MiniMQTT connect works, and the firmware
      adapter connect path works when socket wrapping is disabled. Wrapped
      firmware connect is still too stack-heavy. The normal app's warm-start
      `mqtt_connect_failed:('Connect failure', None)` is not explained by a
      general app initialization requirement.

18. Cold/no-app publish baseline run starting at serial log line `5016`:
    - `platform_test.run(mqtt_mode="direct_plain", mqtt_rounds=1)` completed a
      full subscribe/publish/poll roundtrip. The broker received
      `nodus/co2-frank/platform_test/direct_plain` at `12:54:15`.
    - `platform_test.run(mqtt_mode="firmware", mqtt_rounds=1)` reached farther
      than the earlier firmware adapter tests: adapter setup, MQTT connect,
      subscribe, and publish all succeeded. The broker received
      `nodus/co2-frank/platform_test/firmware` at `12:54:55`.
    - The firmware run failed only after publish, when the transport poll path
      raised `RuntimeError: pystack exhausted` with `connected=True` and
      `pending=0`.
    - Current inference: cold/no-app firmware MQTT can connect, subscribe, and
      publish through the unwrapped adapter. The remaining platform-test stack
      failure in this mode is now isolated to the firmware receive/poll path,
      likely still traversing MiniMQTT's loop stack.

19. `v0.26.140.14` removes MiniMQTT loop use from the unwrapped firmware poll
    path when the connected client exposes a readable socket:
    - `poll_mqtt_client()` now parses one inbound raw MQTT packet directly from
      `_sock` before falling back to `client.loop()`.
    - QoS0 `PUBLISH` packets are decoded and delivered to `MQTTTransport`
      without entering MiniMQTT's receive loop.
    - Socket timeout / no-data during the first byte read is treated as a
      successful empty poll, preserving normal idle behavior.
    - This targets the line `5016+` failure where firmware subscribe and
      publish succeeded, then poll raised `RuntimeError: pystack exhausted`.

20. `v0.26.140.14` cold app boot run starting at serial log line `5090` exposed
    a no-data classification regression:
    - Cold boot connected to MQTT and broker received startup `/meta`,
      heartbeat, availability, and `/data`, so basic cold-start broker reach was
      present.
    - Immediately after each connect, `mqtt poll phase=error` reported
      `mqtt_poll_failed:[Errno 116] ETIMEDOUT`.
    - The raw poll path treated idle socket timeout as fatal, marked MQTT
      disconnected, retried connect, and requeued startup publishes. Broker logs
      therefore showed repeated retained `/meta` publishes and serial queue
      depth grew (`queues pub=4`, then `6`, `8`, ...).
    - `v0.26.140.15` treats CircuitPython `ETIMEDOUT` / errno `116` as an empty
      poll, matching the intended idle behavior from `v0.26.140.14`.

21. `v0.26.140.15` cold app boot run starting at serial log line `5280` confirms
    the no-data timeout regression is resolved for the normal app:
    - The device connected MQTT once at `13:21:29`, published startup identity,
      and moved to `recovery phase=idle` at `13:21:30`.
    - Broker logs show one startup `/meta`, online heartbeat, availability, and
      steady `/data` publishes through `13:33:41`.
    - Periodic health at `13:26:23` and `13:31:23` reported
      `mqtt_connected=True` and queues drained to `pub=0 sub=0 rx=0`.
    - No `mqtt_poll_failed:[Errno 116] ETIMEDOUT` entries occurred in this
      `v0.26.140.15` app run.
    - After manual interruption, `platform_test.run(mqtt_mode="firmware")`
      still connected, subscribed, and published, but failed in poll with
      `RuntimeError: pystack exhausted`. Treat this as the remaining standalone
      firmware poll-path issue, separate from normal app cold boot stability.

22. `v0.26.140.16` adds source-specific poll diagnostics for the remaining
    platform-test `pystack exhausted` failure:
    - Raw socket poll `pystack exhausted` now returns an MQTT sync error instead
      of bubbling a bare `RuntimeError`.
    - The error includes `source=raw_socket`, `source=minimqtt_loop`, or
      `source=compat_loop`, socket state, socket capabilities, client-connected
      state, and the raw receive stage when available.
    - `platform_test.py` version `v0.26.140.16.1` logs the firmware client socket
      state immediately before each transport poll.
    - Next expected platform-test output should identify whether the failure is
      inside raw socket `recv` / `recv_into`, a fallback into MiniMQTT `loop()`,
      or a wrapped socket compatibility path.

23. `v0.26.140.16` platform-test run starting at serial log line `5375`
    identified the remaining poll failure source:
    - Normal app cold boot stayed healthy: MQTT connected once, recovery moved
      to idle, broker data cadence was stable, and periodic GC showed
      `queues pub=0 sub=0 rx=0`.
    - `platform_test.py` version `v0.26.140.16.1` showed the firmware adapter
      socket before poll as `Socket:send1 recv0 recv_into1`.
    - The poll failure is now explicit:
      `mqtt_poll_failed:pystack source=raw_socket ... raw_stage=publish_decode`.
    - This means the platform-test failure is not falling back into MiniMQTT
      `loop()`. It is exhausting stack inside our raw MQTT `PUBLISH` decode /
      receive-delivery path.

24. `v0.26.140.17` reduces stack depth in the raw socket `PUBLISH` decode path:
    - Inlines topic/payload decode and `transport.receive()` into
      `_poll_mqtt_socket_once()` instead of calling a second publish-decode
      helper.
    - Keeps stage-specific `pystack` reporting, now able to identify
      `topic_decode`, `payload_decode`, `puback`, or `transport_receive`.
    - `platform_test.py` version `v0.26.140.17.1` keeps the pre-poll socket
      state diagnostic.

25. `v0.26.140.17` platform-test run starting at serial log line `5450`
    narrows the remaining raw poll stack failure:
    - Normal app runtime was healthy after the second start: MQTT connected at
      `14:01:07`, recovery moved to `idle`, broker-visible `/meta`,
      heartbeat/availability, and `/data` were stable until manual shutdown,
      and periodic GC showed `queues pub=0 sub=0 rx=0`.
    - `platform_test.py` version `v0.26.140.17.1` connected, subscribed, and
      published through the firmware adapter; broker received
      `nodus/co2-frank/platform_test/firmware` at `14:06:37`.
    - The firmware adapter poll still failed on the raw socket path with
      `sock=Socket:send1 recv0 recv_into1`.
    - The failure moved from `raw_stage=publish_decode` to
      `raw_stage=remaining_length:pystack exhausted`.
    - This makes the next target the raw MQTT packet header reader itself,
      especially the helper chain used to read remaining length bytes from
      `recv_into()`.

26. `v0.26.140.18` flattens the raw socket poll receive path further:
    - `_poll_mqtt_socket_once()` now reads the fixed header, remaining length,
      and payload directly from `recv()` / `recv_into()` instead of using the
      generic `_recv_mqtt_remaining_length()` / `_recv_mqtt_byte()` /
      `_recv_socket_exact()` helper chain.
    - Added host coverage for a raw poll socket with only `recv_into()`, matching
      the on-device `Socket:send1 recv0 recv_into1` capability reported by
      `platform_test.py`.
    - Added host coverage that a `pystack exhausted` failure after the fixed
      header is still reported as `raw_stage=remaining_length`.
    - `platform_test.py` version `v0.26.140.18.1` marks the next deployed test
      probe.

27. `v0.26.140.18` run starting at serial log line `5616` was initially
    suspected as a firmware regression, but the failure was hardware-side:
    - MQTT connected successfully at `14:20:47` and recovery moved to `idle`.
    - The fatal error at `14:21:50` was an SCD30/I2C read timeout:
      `OSError: [Errno 116] ETIMEDOUT` from `adafruit_scd30.py`.
    - The following soft-reboot loop failed while binding I2C hardware with
      `RuntimeError: No pull up found on SDA or SCL; check your wiring`.
    - Hardware was corrected by inspection outside the firmware; do not treat
      this run as evidence that `v0.26.140.18` regressed MQTT connect or raw
      MQTT socket polling.

28. `v0.26.140.18` run starting at serial log line `5951` confirms the hardware
    correction and narrows the remaining platform-test failure further:
    - Firmware connected MQTT at `14:27:10`, moved recovery to `idle`, and
      broker-visible `/meta`, heartbeat/availability, and `/data` resumed.
    - Manual stop at `14:29:26` ran runtime network teardown with
      `cycle_radio=1`.
    - `platform_test.py` version `v0.26.140.18.1` connected, subscribed, and
      published through the firmware adapter; broker received
      `nodus/co2-frank/platform_test/firmware` at `14:29:39`.
    - The raw poll failure moved again, from `raw_stage=remaining_length` to
      `raw_stage=transport_receive:pystack exhausted`.
    - This means fixed header, remaining length, payload read, topic decode,
      and payload decode completed on-device. The remaining standalone test
      failure is now in the final delivery into `MQTTTransport.receive()`.

29. `v0.26.140.19` reduces inbound delivery stack pressure:
    - Replaces the frozen dataclass `ReceivedMessage` with a tiny slotted class.
    - Keeps the same `.topic` and `.payload_text` interface used by command
      intake and host tests.
    - Avoids the frozen dataclass initializer/object-setattr path inside
      `MQTTTransport.receive()`, which is the stage that failed on-device as
      `raw_stage=transport_receive:pystack exhausted`.
    - `platform_test.py` version `v0.26.140.19.1` marks the next deployed test
      probe.

30. `v0.26.140.19` run starting at serial log line `6025` confirms the
    standalone firmware MQTT path now passes, but app warm-start connect still
    fails:
    - App boot at `14:34:36` had Wi-Fi ready and NTP synced, then MQTT connect
      failed at `14:34:49`.
    - A following app soft start at `14:35:15` connected and published
      broker-visible `/meta`, heartbeat/availability, and `/data`.
    - After manual stop, `platform_test.py` version `v0.26.140.19.1` completed
      firmware-mode MQTT round trip at `14:35:55`.
    - A later app soft start at `14:36:23` again had Wi-Fi ready and NTP synced,
      but repeated `mqtt_connect_failed` through `14:38:02`.
    - Immediately after stopping that failed app run, the same
      `platform_test.py` firmware-mode MQTT round trip passed again at
      `14:38:20`.
    - This makes `platform_test.py` a positive control: broker reachability,
      Wi-Fi, TCP, firmware adapter connect, subscribe, publish, poll, and receive
      all work in the same warm-failed window where the app cannot connect.
    - The strongest remaining difference is app network/socket construction:
      the app uses `adafruit_connection_manager.get_radio_socketpool(radio)`,
      while `platform_test.py` resets station state and creates a fresh
      `socketpool.SocketPool(radio)` directly.

31. `v0.26.140.20` makes app station startup closer to the passing
    `platform_test.py` path:
    - Station startup now performs an explicit station reset before the first
      connect attempt.
    - Socket artifacts now prefer a fresh direct `socketpool.SocketPool(radio)`
      when available, falling back to `adafruit_connection_manager` if direct
      socketpool construction is unavailable.
    - Recovery reconnects that already requested `reset_station=True` do not
      perform a second startup reset inside `_connect_station()`.
    - `platform_test.py` version `v0.26.140.20.1` marks the next deployed test
      probe.

32. `v0.26.140.20` run starting at serial log line `6190` shows the app change
    is active but does not fully solve warm-start MQTT connect:
    - App startup now logs `network station reset phase=startup`, confirming the
      new station reset path ran.
    - First app run at `14:47:39` still hit `mqtt_connect_failed` at
      `14:47:56` and `14:48:13`.
    - A following app run at `14:48:38` connected and published broker-visible
      `/meta`, heartbeat/availability, and `/data`.
    - A later app run at `14:49:52` again failed MQTT connect at `14:50:09`,
      while `platform_test.py` version `v0.26.140.20.1` immediately passed
      firmware-mode MQTT round trip at `14:51:37`.
    - Station reset alone is not sufficient. The next missing diagnostic is
      whether the app's exact socket pool can complete a TCP probe immediately
      before MiniMQTT connect, and whether direct socketpool construction is
      actually used on device.

33. Test classification note:
    - A firmware deploy followed by immediately starting `code.py` is a warm
      deploy-start test, not a cold-start test, unless the Nodus was power
      cycled.
    - These immediate post-deploy runs are intentionally included in the debug
      timeline because they reproduce the same `mqtt_connect_failed` behavior
      seen after other warm starts.
    - Treat only explicit power-cycle starts as cold-start evidence.

34. `v0.26.140.21` adds the next app-side discriminator:
    - Startup logs now report the app socket artifact source as `direct`,
      `connection_manager`, or `unknown`.
    - Immediately before each app MiniMQTT connect attempt, the app opens and
      closes a TCP socket to the active broker using the same adapter socket
      pool and logs `mqtt preflight phase=tcp_ok` or `phase=tcp_error`.
    - If preflight fails, the issue is below MiniMQTT in the app socket
      pool/radio state. If preflight succeeds but MiniMQTT connect fails, the
      remaining issue is in the MiniMQTT CONNECT/CONNACK path or state.

35. `v0.26.140.21` run starting at serial log line `6323` proves the app can
    open raw TCP from the same direct socket pool before MiniMQTT fails:
    - Startup reports `network socket_artifacts source=direct socket_pool=ready
      ssl_context=ready`.
    - Failed app run at `15:02:09` shows repeated `mqtt preflight phase=tcp_ok`
      immediately before `mqtt_connect_failed`.
    - The following app run at `15:03:43` also reports `tcp_ok`, then connects
      immediately and publishes broker-visible `/meta`, heartbeat,
      availability, and `/data`.
    - Later soft reboot at `15:08:58` again reports `tcp_ok` before repeated
      `mqtt_connect_failed`; the interrupt traceback is inside MiniMQTT
      `_sock_exact_recv` waiting for CONNACK.
    - `platform_test.py` firmware mode immediately passes after the failed app
      run. The remaining split is no longer Wi-Fi, DNS, broker reachability, or
      app socket pool construction; it is MiniMQTT CONNECT/CONNACK behavior in
      the app startup context.

36. `v0.26.140.22` adds a raw MQTT CONNECT/CONNACK probe in the app startup
    path:
    - The probe uses the same MQTT adapter socket pool as MiniMQTT.
    - When MiniMQTT accepts `client_id`, both MiniMQTT and the probe use the
      same deterministic runtime client ID: sensor ID, switch device ID, then
      hostname.
    - It sends a minimal MQTT 3.1.1 CONNECT packet, reads CONNACK, sends
      DISCONNECT on CONNACK success, closes the socket, then allows the normal
      MiniMQTT connect attempt to run.
    - The log line is `mqtt connect_probe phase=connack_ok` or
      `phase=connack_error`, including target, CONNACK code, and probe client
      ID.
    - If this probe succeeds while MiniMQTT connect fails, the next fix should
      avoid MiniMQTT's connect/CONNACK path for app startup.

37. `v0.26.140.22` run starting at serial log line `6477` shows the raw MQTT
    CONNECT probe also times out in failed app-start states:
    - Startup reports direct socket artifacts and Wi-Fi/NTP ready.
    - Each failed app attempt shows `mqtt preflight phase=tcp_ok`, followed by
      `mqtt connect_probe phase=connack_error ... OSError:[Errno 116]
      ETIMEDOUT`, then MiniMQTT `mqtt_connect_failed`.
    - This means the TCP connection to broker `10.0.0.248:1883` opens, but the
      app-start socket does not receive CONNACK after sending CONNECT.
    - A following app run at `15:20:39` shows the same direct socket/TCP path but
      `connect_probe phase=connack_ok ... connack=0`, immediately followed by
      successful MiniMQTT connect and broker-visible `/meta`, heartbeat,
      availability, and `/data`.
    - A later soft reboot at `15:27:20` returns to `tcp_ok` plus
      `connack_error` with `Errno 116` before MiniMQTT connect failure.
    - `platform_test.py` firmware mode immediately passes after the failed app
      run, including TCP, MiniMQTT connect, subscribe, publish, poll, receive,
      and disconnect.
    - Updated interpretation: the failing app-start state is not a plain TCP
      reachability problem and not exclusively MiniMQTT's CONNACK handling. The
      broker CONNACK is not being received by a raw app-start MQTT CONNECT probe
      either, while a platform-test reset/connect sequence can receive CONNACK.
      The next split should compare app-start network sequencing against the
      platform-test station reset/connect sequence, especially scan/reset/order
      and any socket opened before MQTT CONNECT.

38. `v0.26.140.23` changes app-start sequencing to test that next split:
    - Station startup now performs a preconnect scan for the target SSID before
      the startup station reset/connect sequence. This is closer to the
      platform-test flow that successfully recovers after failed app starts.
    - Startup station reset now has a short settle window before connect.
    - NTP is deferred until MQTT has reached a successful/connected state so
      MQTT is the first priority application protocol after Wi-Fi/socket setup.
    - Expected new log lines include `network scan phase=preconnect ...` and
      `ntp phase=deferred reason=mqtt_startup_connect until=mqtt_connected`.
    - If this fixes warm deploy-start/soft-reboot MQTT, the culprit is likely
      startup ordering/radio socket state rather than MQTT packet formatting.
      If it still fails, compare the `connect_probe` result with the new scan
      and deferred-NTP ordering.

39. `v0.26.140.23` run starting at serial log line `6640` shows the sequencing
    changes are active but not sufficient:
    - First warm deploy-start at `15:38:20` logs preconnect scan success,
      startup station reset, direct socket artifacts, and deferred NTP.
    - Despite that, every MQTT attempt remains `tcp_ok` followed by raw
      `connect_probe phase=connack_error ... OSError:[Errno 116] ETIMEDOUT`,
      then MiniMQTT `mqtt_connect_failed`.
    - The plain-connect recovery station reset triggered at elapsed `188s`, but
      the following rebuilt app MQTT attempts still timed out waiting for
      CONNACK.
    - A later app run at `15:42` with the same `.23` sequencing immediately
      produced `connect_probe phase=connack_ok`, MiniMQTT connected, and broker
      received `/meta`, heartbeat, availability, and `/data`.
    - Because NTP is now deferred until MQTT works, startup `/meta`,
      heartbeat, and availability used the unsynced RTC timestamp
      `946684816`; NTP synced immediately after MQTT connected, so subsequent
      `/data` carried the correct epoch timestamp.
    - A later soft reboot at `15:45:09` again logged preconnect scan success
      and deferred NTP, but returned to `tcp_ok` plus CONNACK timeout.
    - `platform_test.py` firmware mode passed once after the successful app run.
      The next platform-test run failed at Wi-Fi scan (`target_ssid_not_found`),
      and an immediate third run passed end-to-end. Treat that middle failure as
      transient Wi-Fi scan/connectivity noise, not as an MQTT probe failure.
    - Updated interpretation: preconnect scan, station reset settle, and
      deferring NTP do not by themselves clear the warm-start CONNACK timeout.
      The failure can survive an in-app station reset/rebuild and still clear on
      a later code-run/platform-test sequence.

40. Related field observation: wrapped MiniMQTT socket arity failures were seen
    on multiple Nodus devices, including `co2-ykdvea` and `co2-frank`.
    Example from `/Users/twfarley/cu.usbmodem1334101.log` around
    `2026-05-20 00:30`:
    - The device was healthy at `00:29:53` with Wi-Fi ready, DNS OK, NTP synced,
      MQTT connected, and free heap recovered to about `126788` after GC.
    - At `00:30:15`, MQTT recovery entered because poll failed on a wrapped
      MiniMQTT socket:
      `mqtt_poll_failed:minimqtt_socket:sock=wrapped ... error=function takes 3
      positional arguments but 2 were given`.
    - Recovery rebuilt MQTT with `station_reset=1` and immediately reconnected,
      then queued startup/config subscriptions again.
    - At `00:30:28`, subscription sync failed on the second subscription with
      `No data received from broker for 1 seconds`, triggered another MQTT
      rebuild, and the first rebuild reconnect attempt hit
      `mqtt_connect_failed`.
    - A retry at `00:30:33` connected and completed subscription recovery, with
      queues drained and the device returning to idle.
    Example from `/Users/twfarley/nodus.log` on `co2-frank` around
    `2026-05-19 10:12`:
    - The device was healthy at `10:07:20` with Wi-Fi ready, DNS OK, NTP synced,
      MQTT connected, and free heap around `135152` after GC.
    - At `10:12:15`, MQTT recovery entered for the same wrapped MiniMQTT socket
      arity failure:
      `mqtt_poll_failed:minimqtt_socket:sock=wrapped ... error=function takes 3
      positional arguments but 2 were given`.
    - Recovery rebuilt MQTT with `station_reset=1`, reconnected immediately at
      `10:12:18`, queued subscriptions, and returned to idle by `10:12:20`.
    - This older fleet-wide pattern is not identical to the current `co2-frank`
      warm-start connect failure, but it may be related: MiniMQTT socket
      handling, subscription sync timeouts, and reconnect/rebuild churn can
      cascade into the same `mqtt_connect_failed` surface symptom even when
      Wi-Fi health is otherwise good.

41. `v0.26.140.24` changes recovery policy for the persistent warm-start MQTT
    CONNECT/CONNACK timeout:
    - Plain `mqtt_connect_failed:...('Connect failure', None)` errors now count
      toward the repeated MQTT connect failure window from startup, not only
      after a prior MQTT success.
    - After `>180s` and at least three consecutive MQTT connect failures,
      recovery escalates `mqtt_repeated_connect_failures` to
      `microcontroller.reset()`, including ROFS devices.
    - `microcontroller.nvm[1]` is used as a one-shot guard. The app sets a
      distinct MQTT recovery marker before hard reset, `boot.py` preserves it
      across warm resets, and the app clears it after MQTT connects. `boot.py`
      still clears this byte on true power/brownout reset.
    - If the hard reset does not clear the condition, the next `>180s` window
      logs `action=hard_reboot_suppressed reason=mqtt_repeated_connect_failures
      marker=nvm` rather than disconnecting the screen session repeatedly.
    - This deliberately does not revive the older "cold boot then warm reload"
      workaround from `f6eef83`. That workaround targeted the opposite failure
      shape. The current evidence shows soft/warm reload can preserve the bad
      CONNACK-timeout state, so the NVM marker is only a loop guard.

42. `v0.26.140.24` hard-reset run starting at serial log line `6888` confirmed
    the hard-reset escalation but exposed a separate Wi-Fi startup hang:
    - The app held repeated `tcp_ok` plus `connect_probe phase=connack_error`
      failures from `16:06:31` until `16:10:00`.
    - At `16:10:00`, recovery logged
      `action=hard_reboot reason=mqtt_repeated_connect_failures count=10
      elapsed_s=192 marker=set`, so the NVM-guarded hard reset fired as
      intended.
    - After reconnecting the serial screen, the rebooted app logged
      `network scan phase=preconnect ... result=found`, then
      `network station reset phase=startup`, then
      `network connect attempt=1`.
    - `wifi.radio.connect()` raised `ConnectionError: Unknown failure 1`. The
      exception handler then started another `_scan_for_station_ssid()` before
      retrying and the scan blocked until manual `KeyboardInterrupt`.
    - The manual interrupt was roughly `30+s` after
      `network connect attempt=1`, so the silent period may include time inside
      `wifi.radio.connect()` before it raised as well as time in the follow-up
      scan. The traceback only proves the interrupt landed in the scan.
    - `v0.26.140.25` changes this path: when a preconnect scan already produced
      a station hint, a join failure reuses that hint and logs
      `network scan attempt=... result=skipped_hint` instead of starting a
      second scan. This preserves retry with the known channel/BSSID while
      avoiding the observed post-failure scan freeze.
    - If `.25` still goes silent after `network connect attempt=1`, the silence
      is now more likely inside `wifi.radio.connect()` itself; if connect raises,
      the next expected log is `result=skipped_hint`.

43. `v0.26.140.25` run starting at serial log line `7043` is the first clean PoC
    that the hard-reset recovery works as intended:
    - The first `.25` boot after deploy/hard-reset hit Wi-Fi join noise:
      `Unknown failure 205`, then logged
      `network scan attempt=1 ... result=skipped_hint`, retried with
      `strategy=channel`, and connected on attempt 2. This confirms the `.25`
      scan-hang fix is active.
    - MQTT then reproduced the warm-start failure: every attempt had TCP OK,
      raw CONNECT probe timed out waiting for CONNACK with `Errno 116`, and
      MiniMQTT connect returned `mqtt_connect_failed`.
    - At `16:23:56`, after `192s` and `count=10`, recovery logged
      `action=hard_reboot reason=mqtt_repeated_connect_failures ... marker=set`
      followed by `runtime action=reset`.
    - The next boot again saw a first Wi-Fi join failure (`Unknown failure 1`),
      but `.25` reused the preconnect hint instead of scanning again, retried
      with `strategy=channel`, and connected by `16s`.
    - Immediately after that reset, MQTT preflight was TCP OK, raw
      `connect_probe` returned `connack_ok`, the NVM marker was cleared with
      `recovery action=clear_hard_reset_marker reason=mqtt_connected`, and
      MiniMQTT connected.
    - Broker capture confirms retained `/meta`, online heartbeat,
      `/availability`, and `/data` for version `v0.26.140.25` at `16:24:23+`.
      The startup identity/status timestamps are unsynced RTC (`946684826`)
      because NTP is deferred until MQTT works; `/data` after NTP sync carries
      the real epoch timestamp.

44. `v0.26.140.26` tightens the hard-reset gate after the PoC:
    - The hard reset now applies only to the specific pattern
      `tcp_ok -> connect_probe connack_error Errno 116/ETIMEDOUT ->
      mqtt_connect_failed`.
    - Generic `mqtt_connect_failed` no longer triggers this hard-reset path by
      itself.
    - The timeout window is reduced from `180s` to `60s` while retaining the
      minimum consecutive-failure count. In observed logs, attempts are roughly
      `21s` apart, so this should usually fire around the fourth failed connect
      cycle: not immediate, but no longer a three-minute wait.
    - This threshold intentionally sits above the observed transient MQTT/network
      hiccup range, which is typically under `30s`.

45. `platform_test.py` version `v0.26.141.1` adds a warm-state diagnostic mode:
    - `mqtt_mode="warm_diagnostics"` runs repeated connect-only probes while
      preserving the current station/radio state.
    - The intended warm-failure command disables scan, station reset, and NTP so
      the test does not accidentally clean up the failure before measuring it.
    - Each cycle logs memory, radio state, plain TCP reachability, raw MQTT
      CONNECT/CONNACK, firmware-adapter TCP preflight, firmware-adapter raw
      CONNECT/CONNACK, firmware MiniMQTT connect, direct-plain MiniMQTT connect,
      and direct MiniMQTT connect.
    - This should distinguish three cases after the app hits the warm-start
      `mqtt_connect_failed` loop:
      raw MQTT still fails, meaning the radio/socket stack is in the same bad
      CONNACK-timeout state; raw MQTT passes but firmware connect fails, meaning
      adapter/client setup is the split; all probes pass, meaning the app startup
      sequence or prior runtime state is still the split.
    - `mqtt_mode="warm_start_matrix"` bypasses the normal platform-test
      pre-connect path and compares parameter sets from the current state:
      `app_like`, `app_like_ntp_first`, `reset_no_scan`, and `platform_plain`.
      This is the next test for whether preconnect scan, station reset, NTP
      timing, or plain platform-style Wi-Fi setup changes the CONNACK result.
    - New hardware observation on `aqi-wfcp7p`: after `platform_test.py` runs
      successfully, the normal app can be warm-started repeatedly and still
      connect MQTT cleanly. That points to platform-test setup leaving a durable
      good radio/socket state, not merely proving a one-time broker path.

46. `v0.26.141.2` adds focused app-side warm-start diagnostics around the known
    CONNACK-timeout failure path:
    - `mqtt connect_context ...` records the connect attempt number, MQTT
      recovery phase/elapsed time, Wi-Fi readiness, network phase/IP, socket
      artifact source, heap, transport queues, and previous disconnect reason.
    - `mqtt connect_decision ...` records whether the raw MQTT probe already
      matches the `tcp_ok -> connack timeout` pattern before MiniMQTT connect is
      attempted.
    - `mqtt connect_cleanup ...` records failed-connect elapsed time, whether
      the failure was classified as the bounded hard-reset pattern, whether a
      MiniMQTT socket remains attached after cleanup, Wi-Fi readiness, IP, and
      transport queue depth.
    - This is intentionally diagnostic-only. It does not change MQTT recovery
      thresholds, broker topics, or whether MiniMQTT is still attempted after
      the raw CONNACK probe fails.

47. `v0.26.141.3` targets warm-start recovery without using the hard reset:
    - Raw CONNACK timeout by itself is not treated as fatal, because one
      `v0.26.141.2` run showed `connect_probe` timing out while MiniMQTT still
      connected successfully.
    - When raw CONNACK timeout and MiniMQTT connect failure both occur, the app
      now marks a pending CONNACK-timeout recovery.
    - On the next MQTT recovery pass, that pending state bypasses the
      `hold_rebuild` path and performs station reset, radio cycle, and socket
      artifact rebuild before the next connect attempt.
    - This is intended to make the app perform the same practical cleanup that
      platform_test currently gets after app teardown plus a plain Wi-Fi/socket
      reconnect, while keeping the hard reset as a later fallback.

### Current Accepted Status

- Current firmware version for this redo branch is `v0.26.142.9`.
- Broker-visible hardware validation for the protected MQTT contract has been
  accepted on 2026-05-22.
- Sensor-device validation on `aqi-wfcp7p` showed `/data` continuing at roughly
  the configured cadence and `/status/heartbeat` plus `/availability` at the
  status cadence from `20:29:56-0600` through `20:38:59-0600`.
- Switch-capable validation has been accepted. The broker capture showed
  `S1-wfcp7p` config `set` / `ack` / `result`, switch `event`, switch `state`,
  and the parent `aqi-wfcp7p/meta/patch` for `SWITCH_1_LAST_STATE`.
- The warm-start policy is accepted: runtime recovery and app-owned restarts for
  MQTT profiles should use `microcontroller.reset()` instead of treating
  `supervisor.reload()` as a recovery path. Manual REPL `Ctrl-D` remains a
  manual CircuitPython path, not an app recovery mechanism.
- Automatic `primer.py` / `startup_test.py` production startup hooks are not part
  of the accepted merge path. Those tools remain investigation apparatus only.

### Final Merge Checklist

1. Resolve the merge conflicts against current `origin/trunk`.
2. Run `git diff --check origin/trunk...HEAD`; it must report no whitespace
   errors.
3. Run `pytest tests`; all host-side tests must pass.
4. After conflict resolution, deploy the merged result and perform a final
   broker-visible smoke check:
   - sensor retained `/meta`;
   - switch-capable retained `/meta` and `/meta/switch`;
   - `/data` at the configured sensor cadence;
   - `/status/heartbeat` and `/availability` at the configured status cadence;
   - broker-visible identity/status/data after any recovery reset.
5. Confirm app-owned MQTT-profile restarts still log and perform hard reset
   rather than a soft reload.
6. Preserve the versioning rule: bump `cpynodus_ii/__init__.py` only once for
   runtime changes in the final merged branch, not for this docs-only cleanup.

## Merge Rules

1. Treat MQTT identity publishing as a protected contract.
2. Do not accept serial logs alone as proof of MQTT success.
3. Verify broker-visible retained `/meta` after startup and after recovery.
4. Verify broker-visible retained `/meta/switch` for switch-capable devices.
5. Merge in small milestones and stop immediately when the MQTT contract breaks.
6. Avoid importing warm-reboot or recovery rewrites blindly.
7. Keep generated `__pycache__` and build artifacts out of the branch unless
   they are deliberately part of a test fixture.

## Commit Risk Map

Commits from `mqtt-merge-redo-from-139.3` to `origin/trunk`:

- `f6eef83` - High risk. Cold boot MQTT workaround using warm reboot.
- `607465d` - Low risk. Docs-only architecture wording.
- `46ecd17` - Medium risk. Nodusweb UI work plus network/mDNS changes.
- `b9a3f1e` - Medium risk. mDNS only when web is enabled; reconnect behavior.
- `1f2fea9` - High risk. Commit message says MQTT is busted.
- `c201a6c` - Medium risk. MQTT contract cleanup and reboot log sizing.
- `8969d98` through `4cf87b8` - Medium/high risk. OTA implementation touches
  app startup, MQTT, command intake, payloads, and memory-sensitive paths.
- `6814c97` - Medium risk. Cleanup touches `app.py` and MQTT client.
- `7116106` - High risk. MQTT recovery process changes and recovery log.
- `bb8c133` - High risk. `/meta` split, startup sequence changes.
- `0873296` - High risk. MQTT sync priority and slow-operation disconnects.
- `3c2bc68` and `d42bd26` - Medium risk. MQTT log retrieval tooling.

## Milestones

### Milestone 0: Baseline

Status: complete.

Actions:

- Confirm branch is `mqtt-merge-redo-from-139.3`.
- Confirm version is `v0.26.139.3`.
- Confirm working tree is clean before migration work.
- Run host-side baseline checks.

Checks run:

- `pytest tests/test_mqtt_client_adapter.py`
- `pytest tests/test_publish_cycle.py`
- `pytest tests/test_app_startup.py`

Result:

- All passed.

### Milestone 1: Add MQTT Contract Validation

Before importing risky runtime changes, add or document a repeatable validation
step that confirms broker-visible messages.

Required broker checks:

- On boot, observe retained `/meta`.
- On recovery, observe retained `/meta` again.
- On switch-capable devices, observe retained `/meta/switch`.
- Observe `/data` at roughly 60 seconds, unless configuration says otherwise.
- Observe heartbeat and availability at roughly 120 seconds.

Recommended devices:

- Sensor-only: `aht-rvwi73` or `co2-frank`
- Switch-capable: `co2-ykdvea`

### Milestone 2: Low-Risk Non-MQTT Changes

Bring in changes that do not alter MQTT startup, recovery, socket handling, or
publish queue semantics.

Candidate:

- `607465d` if not already effectively present.

Validation:

- Host tests.
- No required hardware validation if no runtime files changed.

### Milestone 3: Web/Nodusweb Changes Without MQTT Recovery Changes

Consider selected parts of `46ecd17`, but inspect and avoid network/mDNS changes
that affect socket allocation or station reconnect behavior.

Validation:

- `pytest tests/test_network_stack.py`
- `pytest tests/test_web_handlers.py`
- `pytest tests/test_web_runtime.py`
- Hardware boot test on one sensor-only device.
- Broker-visible `/meta` check.

### Milestone 4: Network/mDNS Changes

Evaluate `b9a3f1e` separately. The intent may be useful, but it touches reconnect
behavior and socket artifact preservation.

Validation:

- Hardware boot and warm reload.
- Induced MQTT recovery.
- Broker-visible `/meta` after recovery.
- Confirm no extra mDNS allocation in `sensorius`.

### Milestone 5: MQTT Contract and Recovery Changes

Handle `c201a6c`, `7116106`, `bb8c133`, and `0873296` one at a time. These are
the most likely places to lose retained identity replay.

Validation after each commit or selected patch:

- Sensor-only broker contract.
- Switch-capable broker contract.
- Recovery after publish failure.
- Recovery after subscribe failure.
- Heap before and after recovery.

Do not proceed if `/meta` or `/meta/switch` disappears from broker captures.

### Milestone 6: OTA and Log Retrieval

Bring in OTA and MQTT log retrieval after MQTT identity is stable.

Relevant commits:

- `8969d98`
- `cb2ed9e`
- `6679a94`
- `e7c8eb4`
- `4cf87b8`
- `3c2bc68`
- `d42bd26`

Validation:

- Host OTA tests.
- Memory check on-device.
- Broker contract after OTA/log tooling is present.

## Stop Conditions

Stop the merge immediately if any of these occur:

- Broker does not receive `/meta` after boot.
- Broker does not receive `/meta` after MQTT recovery.
- Switch-capable device does not publish `/meta/switch`.
- Serial says `published=1` but broker does not receive the message.
- A recovery leaves only `/data` publishing while identity/status replay is lost.
- Free heap drops sharply without a clear reason.

## Notes for Final Merge to Trunk

When all milestones pass:

1. Run host tests.
2. Run cold boot and warm reload hardware checks.
3. Run broker-visible MQTT checks on sensor-only and switch-capable devices.
4. Bump `cpynodus_ii/__init__.py` once for runtime changes.
5. Merge `mqtt-merge-redo-from-139.3` into `trunk`.
