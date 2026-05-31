# AVPD BME280 MQTT Startup Investigation - 2026-05-29

## Context

Device under test:

- Nodus: `avpd-zbcalz`
- Sensor path: single I2C BME280, `avpd`
- Switch path: one `S1-zbcalz` channel
- Primary failing broker during the broader investigation:
  `sensoria-hub-0.local` / `10.0.0.246`
- Current platform-test baseline broker:
  `samhain.local` / `10.0.0.248`

The original symptom was a startup MQTT failure on Raspberry Pi brokers, while
the same device could run with the Mac broker. A second Nodus configured as
BME280 plus S1 reproduced the startup problem, while changing that same
hardware to AHT or BME680 did not reproduce it. This keeps the BME280 startup
path in scope, but the current narrowing is focused on the MQTT/platform
appliance behavior before changing the app again.

## Known Good Version Boundary

Broker-visible tests showed that `avpd-zbcalz` can work against the Raspberry Pi
broker path:

- `v0.26.134.1`: app startup produced broker-visible retained startup topics.
- `v0.26.137.2`: app startup produced broker-visible retained startup topics.
- `v0.26.139.3`: app startup produced broker-visible retained startup topics.
- `v0.26.140.26`: app startup produced broker-visible retained startup topics.
- `v0.26.142.9`: app startup produced broker-visible retained startup topics.

The break is after `v0.26.142.9`. `v0.26.143.1` was bad in the same test
description. Earlier review identified the first bad area as the `143.1` merge
path that introduced startup/MQTT queue changes.

## Non-BME280 Broker Comparisons

Other Nodus devices running later app code have not shown the same Raspberry Pi
broker startup failure. One concrete comparison from `cu.usbmodem133101.log`:

- Device: `aht-yuk0nv`
- App version: `v0.26.148.1`
- Sensor path: AHT on I2C address `0x38`
- Switch path: one `S1-yuk0nv` channel
- Broker: `sensoria-hub-0.local` / `10.0.0.246`

Serial evidence:

```text
22s cPyNodus_II boot version=v0.26.148.1 profile=sensorius
22s cPyNodus_II sensor ... target_addr=0x38 adapter=bound service=ready
22s mqtt preflight phase=tcp_ok broker=10.0.0.246 target=10.0.0.246
22s mqtt connect_probe phase=skipped ... reason=raw_connect errors=none
22s mqtt connect_decision ... will_minimqtt=0 connect_mode=raw
23s mqtt connect phase=connected broker=10.0.0.246 elapsed_s=0.1
23s mqtt subscriptions queued ... queues pub=5 sub=4 rx=0
30s mqtt startup_subscribe_settle phase=complete ... queues pub=0 sub=1 rx=0
2026-05-29 15:31:39 ntp phase=synced
```

This comparison is important because it weakens a broad "`v0.26.148.1` app code
cannot use the Raspberry Pi broker" explanation. The observed failure remains
more specific to the BME280/AVPD startup combination, or to code paths that the
AHT startup does not exercise.

A second comparison from `cu.usbmodem1334101.log` shows a CO2 plus two-switch
Nodus also stable on `v0.26.148.1`:

- Device: `co2-ykdvea`
- App version: `v0.26.148.1`
- Sensor path: CO2 on I2C address `0x61`
- Switch path: `S1-ykdvea`, `S2-ykdvea`
- Broker host in operator notes: `samhain.local`
- Active broker IP in serial log: `10.0.0.220`
- Runtime duration: greater than 24 hours, from `2026-05-28 07:20:55` through
  at least `2026-05-29 16:43:38`

Startup evidence:

```text
12s cPyNodus_II boot version=v0.26.148.1 profile=sensorius
12s cPyNodus_II sensor ... target_addr=0x61 adapter=bound service=ready
13s mqtt preflight phase=tcp_ok broker=10.0.0.220 target=10.0.0.220
13s mqtt connect_probe phase=skipped ... reason=raw_connect errors=none
13s mqtt connect_decision ... will_minimqtt=0 connect_mode=raw
13s mqtt connect phase=connected broker=10.0.0.220 elapsed_s=0.1
13s mqtt subscriptions queued ... queues pub=5 sub=4 rx=0
20s mqtt startup_subscribe_settle phase=complete ... queues pub=0 sub=2 rx=0
2026-05-28 07:20:55 ntp phase=synced
```

Steady-state evidence:

```text
2026-05-29 16:40:34 co2-ykdvea health ... mqtt_connected=True active_broker=10.0.0.220
2026-05-29T16:40:37-0600 nodus/co2-ykdvea/data ...
2026-05-29T16:41:17-0600 nodus/co2-ykdvea/status/heartbeat ...
2026-05-29T16:41:19-0600 nodus/S1-ykdvea/availability ...
2026-05-29T16:41:20-0600 nodus/S2-ykdvea/availability ...
```

The same run also handled a broker-visible switch config command and emitted
`config/ack`, `config/result`, switch `event`, switch `state`, and
`meta/patch`. This further weakens any broad rPi broker, `samhain.local`,
`v0.26.148.1`, raw connect, or switch-command-path explanation.

A third comparison from `cu.usbmodem1334301.log` removes sensors entirely:

- Device: `switch-w9umh8`
- App version: `v0.26.148.1`
- Sensor path: disabled/inactive
- Switch path: one `S1-w9umh8` channel
- Broker: `sensoria-hub-0.local` / `10.0.0.246`
- Runtime duration: greater than 24 hours, from `2026-05-28 07:50:12` through
  at least `2026-05-29 16:46:48`

Startup evidence:

```text
19s cPyNodus_II boot version=v0.26.148.1 profile=sensorius
19s cPyNodus_II sensor enabled=False ... service=inactive metrics=0
19s cPyNodus_II switch enabled=True channels=1 phase=ready
19s mqtt preflight phase=tcp_ok broker=10.0.0.246 target=10.0.0.246
19s mqtt connect_probe phase=skipped ... reason=raw_connect errors=none
19s mqtt connect_decision ... will_minimqtt=0 connect_mode=raw
19s mqtt connect phase=connected broker=10.0.0.246 elapsed_s=0.1
20s mqtt subscriptions queued ... queues pub=3 sub=4 rx=0
23s mqtt startup_subscribe_settle phase=complete ... queues pub=0 sub=4 rx=0
2026-05-28 07:50:12 ntp phase=synced
```

Steady-state and broker-visible command evidence:

```text
2026-05-29 16:44:45 switch-w9umh8 health ... mqtt_connected=True active_broker=10.0.0.246
2026-05-29T16:44:20-0600 nodus/S1-w9umh8/config/set ...
2026-05-29T16:44:21-0600 nodus/S1-w9umh8/config/ack ...
2026-05-29T16:44:22-0600 nodus/S1-w9umh8/config/result ...
2026-05-29T16:44:23-0600 nodus/S1-w9umh8/event ...
2026-05-29T16:44:24-0600 nodus/S1-w9umh8/state OFF
2026-05-29T16:44:25-0600 nodus/switch-w9umh8/meta/patch ...
```

This switch-only control further narrows the failing surface away from generic
MQTT startup, raw-connect mode, switch subscriptions, or command handling on
`sensoria-hub-0.local`.

## Current App Baseline

With current experiment firmware `v0.26.149.5`, using `samhain.local` /
`10.0.0.248`, the real app startup succeeded and produced broker-visible MQTT
startup messages:

- `nodus/avpd-zbcalz/meta`
- `nodus/avpd-zbcalz/status/heartbeat`
- `nodus/avpd-zbcalz/availability`
- `nodus/avpd-zbcalz/meta/switch`

The serial startup path for that successful app run was:

```text
mqtt preflight phase=tcp_ok broker=10.0.0.248
mqtt connect_probe phase=connack_ok broker=10.0.0.248 client_id=avpd-zbcalz
mqtt connect_delay phase=pre_minimqtt delay_s=5.0
mqtt connect phase=connected broker=10.0.0.248
mqtt subscriptions queued ... queues pub=4 sub=4
```

This is the baseline that the platform appliance needs to reproduce before the
app is changed again.

## Platform Appliance Results

`platform_test.py` was extended to add elapsed seconds and app-startup probe
modes. The operator deploys this file manually to the CIRCUITPY root.

### `v0.26.149.8`

`app_startup` skipped the raw CONNACK probe:

```text
preflight phase=tcp_ok
connect_probe phase=skipped reason=app_startup_skip_connack_probe
connect_delay phase=pre_minimqtt delay_s=5.0
connect phase=error ... ('Connect failure', None)
```

This did not match the successful app baseline because the real app performed
and passed the raw CONNACK probe before MiniMQTT.

### `v0.26.149.9`

`app_startup` was changed to perform the raw CONNACK probe again. Against
`samhain.local` / `10.0.0.248`, it failed before MiniMQTT:

```text
preflight phase=tcp_ok broker=10.0.0.248
connect_probe phase=connack_error ... OSError:[Errno 116] ETIMEDOUT
```

This occurred with both already-connected and reset/reconnect station state.

### `v0.26.149.10`

`raw_device_id` was added to test a standalone raw MQTT CONNECT using the
production client id `avpd-zbcalz`.

The observed sequence was:

- `direct`: pass
- `raw_device_id`: pass
- `app_startup`: fail

Evidence:

```text
mqtt mode=direct connect phase=connected
mqtt mode=direct subscribe phase=ok
mqtt mode=direct publish phase=ok
mqtt mode=direct phase=roundtrip_ok

mqtt mode=raw_device_id connect_packet phase=sent bytes=25 sent=25
mqtt mode=raw_device_id connack phase=received bytes=4 hex=20020000
mqtt mode=raw_device_id connack phase=ok

mqtt mode=app_startup preflight phase=tcp_ok broker=10.0.0.248
mqtt mode=app_startup connect_probe phase=connack_error ...
    OSError:[Errno 116] ETIMEDOUT
```

## Ruled Out By Current Appliance Runs

These are not sufficient explanations for the current `samhain.local` appliance
failure:

- Broker reachability: TCP preflight succeeds.
- MiniMQTT generally: `direct` connects, subscribes, publishes, and receives.
- Raw MQTT packet shape generally: standalone raw probe receives CONNACK.
- Production client id: `raw_device_id` with `avpd-zbcalz` receives CONNACK.
- Broker authentication: both `direct` and raw `avpd-zbcalz` CONNECT succeed
  without credentials.

## Current Narrowing

The remaining failing path is specific to the adapter-based app-startup raw
CONNACK probe:

```text
build_mqtt_client_adapter(...)
preflight_mqtt_broker_tcp(adapter) -> tcp_ok
preflight_mqtt_broker_connect(adapter) -> Errno 116 waiting for CONNACK
```

The standalone raw probe uses the same broker and production client id but does
not build the firmware MQTT adapter first, and it receives CONNACK immediately.

The next useful appliance instrumentation should log the adapter preflight
CONNECT packet/send/read path:

- chosen socket pool object/type
- CONNECT packet length
- send return value
- whether the timeout happens before the first CONNACK byte or during payload
  read
- socket API path used by the adapter preflight (`recv` vs `recv_into`)

Do not change runtime app startup again until the appliance reproduces the
successful `samhain.local` app baseline or identifies the adapter preflight
delta precisely.

## 2026-05-30 Morning Follow-up: `avpd-0kl7sx+s1`

The May 30 investigation moved from `avpd-zbcalz` to a second BME280 plus S1
Nodus, `avpd-0kl7sx`, and added startup publish/subscribe memory diagnostics.
The important result is that the observable failure is now narrower than
"BME280 plus switch cannot use MQTT":

- `aht-yuk0nv+s1` works against `10.0.0.246`.
- `avpd-0kl7sx` without the S1 switch enable jumper works.
- `avpd-0kl7sx+s1` works against `10.0.0.248`.
- `avpd-0kl7sx+s1` repeatedly fails against `10.0.0.246`.

The switch-enable gate was also corrected during this series: for runtime
switch detection, `switch.toml` is necessary but not sufficient. At least one
configured `SWITCH_N_ENABLE_PIN` must be populated and grounded at boot. With
the S1 enable jumper removed, `avpd-0kl7sx` is now treated as sensor-only and
does not publish `meta/switch`.

### Heap and Publish Sequence

The post-device-`/meta` garbage collection experiment improved heap headroom but
did not resolve the failure.

Representative successful `aht-yuk0nv+s1`, `v0.26.150.7`, broker
`10.0.0.246`:

```text
post_mqtt_connect free_mem=101920
publish nodus/aht-yuk0nv/meta bytes=1405
    after=free_mem=59616 post_gc=free_mem=98096
publish heartbeat after=free_mem=83648
publish availability after=free_mem=70640
publish meta/switch after=free_mem=56096
subscriptions drain; NTP syncs
```

Representative failing `avpd-0kl7sx+s1`, `v0.26.150.7`, broker
`10.0.0.246`:

```text
post_mqtt_connect free_mem=98112
publish nodus/avpd-0kl7sx/meta bytes=1434
    after=free_mem=48548 post_gc=free_mem=92884
publish heartbeat after=free_mem=78436
publish availability after=free_mem=65396
publish meta/switch after=free_mem=50852
subscribe nodus/avpd-0kl7sx/config/set
    before=free_mem=38740
    raw_subscribe_diag pkt_bytes=35 topic_len=28 send_ms=0
    error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT
```

The later reconnect/preflight path then reported `ENOMEM` while Python heap was
back near 90 KB free:

```text
mqtt connect_context ... free_mem=91636
mqtt preflight phase=tcp_error ... OSError:[Errno 12] ENOMEM
```

That pattern argues against ordinary CircuitPython heap exhaustion as the
primary fault. The Nodus sends the raw SUBSCRIBE packet immediately, then fails
waiting for the first SUBACK byte. After that timeout, the socket/radio/lwIP
state appears fragile or poisoned enough that immediate TCP preflight can fail
with `ENOMEM` despite healthy Python heap.

Extending the SUBACK timeout to 2 seconds did not resolve the failure and was
not retained. The failure remained `mqtt_suback_stage=header:ETIMEDOUT`.

### BME280 Driver Profile Experiment

`v0.26.150.8` changed the BME280 import from `adafruit_bme280.basic` to
`adafruit_bme280.advanced` as a heap/timing experiment. It did not resolve the
failure and reduced heap headroom by roughly 3 KB in the failing path. The
experiment was reverted in `v0.26.150.9`, returning `avpd`/`apvpd` to
`adafruit_bme280.basic`.

Representative `v0.26.150.8`, `avpd-0kl7sx+s1`, broker `10.0.0.246`:

```text
post_mqtt_connect free_mem=94912
publish /meta post_gc=free_mem=89556
publish meta/switch after=free_mem=47524
subscribe before=free_mem=35412
error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT
recovery preflight OSError:[Errno 12] ENOMEM with free_mem=88308
```

The same `v0.26.150.8` sensor-only control, with the switch disabled, reached
`sub=0`, synced NTP, and continued running. That reinforces the current
trigger shape as `avpd+s1`, not BME280 alone.

### Broker A/B on `v0.26.150.9`

The most useful May 30 comparison used the same `avpd-0kl7sx+s1` firmware and
hardware against two brokers.

Failing broker `10.0.0.246`:

```text
runtime mqtt_connect=deferred:10.0.0.246
post_mqtt_connect free_mem=98064
publish /meta bytes=1434 post_gc=free_mem=92712
publish meta/switch after=free_mem=50680
subscribe nodus/avpd-0kl7sx/config/set
    before=free_mem=38568
    send_ms=0
    error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT
recovery preflight OSError:[Errno 12] ENOMEM with free_mem=91464
```

Successful broker `10.0.0.248`:

```text
runtime mqtt_connect=deferred:10.0.0.248
post_mqtt_connect free_mem=98128
publish /meta bytes=1427 post_gc=free_mem=95984
publish meta/switch after=free_mem=52448
device subscriptions drain from sub=4 to sub=0
switch_subscriptions queued topics=nodus/S1-0kl7sx/config/set
startup_subscribe_settle complete
NTP syncs
```

The final `v0.26.150.9` run was configured with
`MQTT.STARTUP_SUBSCRIBE_BEFORE_PUBLISH = true`, but the serial sequence still
showed retained startup publishes before the first subscription attempt:

```text
subscriptions queued ... queues pub=6 sub=4
publish nodus/avpd-0kl7sx/meta
publish nodus/avpd-0kl7sx/status/heartbeat
publish nodus/avpd-0kl7sx/availability
publish nodus/avpd-0kl7sx/meta/switch
subscribe nodus/avpd-0kl7sx/config/set
```

That means the final `.246` failure should not be treated as a valid
subscribe-before-publish experiment. The runtime code path appears not to be
honoring the diagnostic flag in `sync_transport_to_client`: when subscriptions
are pending, the adapter still selects the next retained startup-priority
publish before syncing subscriptions. The docs and focused test expectation say
the flag should subscribe first, so the next runtime fix should wire this flag
through the MQTT adapter and skip the startup-priority publish override when it
is true.

Implementation follow-up: `v0.26.150.10` wires
`STARTUP_SUBSCRIBE_BEFORE_PUBLISH` through `MQTTConfig` and
`MQTTClientAdapter`. With the flag set, pending command subscriptions now sync
before retained startup-priority publishes.

`v0.26.150.10` hardware evidence showed the flag was honored for device command
topics: the queue moved from `sub=4` to `sub=0` before the retained `/meta`
publish. The remaining failure moved to the deferred switch command
subscription after `meta/switch`:

```text
switch_subscriptions queued topics=nodus/S1-0kl7sx/config/set
subscribe nodus/S1-0kl7sx/config/set
    error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT
```

Implementation follow-up: `v0.26.150.11` makes the diagnostic flag subscribe
device and switch command topics in the initial startup subscription batch. The
normal default path still defers switch command subscriptions until after the
retained startup publish batch.

The two broker runs had nearly identical Python heap at MQTT connect. The
successful `.248` run had slightly more headroom after retained startup
publishes, but the failing `.246` run still had enough heap for the small
35-byte SUBSCRIBE packet. In later `.246` retry cycles, SUBACK timeout was also
observed with approximately 90 KB free before subscribe, further weakening a
simple low-heap explanation.

### External Mosquitto SUBACK Probes

External `mosquitto_sub -d` probes confirmed that broker `10.0.0.246` can
normally SUBACK both the individual device command topic and the full startup
topic set:

```text
mosquitto_sub -h 10.0.0.246 -p 1883 -i suback-probe-246 \
  -V mqttv311 -q 0 -d -W 3 \
  -t 'nodus/avpd-0kl7sx/config/set'

Client suback-probe-246 received CONNACK (0)
Client suback-probe-246 sending SUBSCRIBE ...
Client suback-probe-246 received SUBACK
Subscribed (mid: 1): 0
```

The full topic-set probe also received one SUBACK granting all five QoS 0
subscriptions:

```text
nodus/avpd-0kl7sx/config/set
nodus/avpd-0kl7sx/calibration/set
nodus/avpd-0kl7sx/fwupdate
nodus/avpd-0kl7sx/logs/get
nodus/S1-0kl7sx/config/set

Client suback-probe-avpd-246 received SUBACK
Subscribed (mid: 1): 0, 0, 0, 0, 0
```

The final `Timed out` line from these probes is expected with `-W 3`; it means
no message payload arrived before the client exited. It is not a SUBACK
failure.

These probes rule out "broker refuses or cannot SUBACK these topics" as a
normal-client explanation. The remaining Nodus-specific difference is the
sequence:

1. connect raw MQTT successfully,
2. publish retained startup identity/status payloads,
3. send raw SUBSCRIBE successfully,
4. time out waiting for the first SUBACK byte,
5. enter recovery where socket/preflight can return `ENOMEM` despite high
   Python heap.

### Current Working Theory

The strongest current theory is not a broker policy failure and not ordinary
Python heap exhaustion. It is a Nodus/Pico2 W socket receive-path failure after
the `avpd+s1` retained startup publish sequence. Broker `10.0.0.246` triggers
the failure with this Nodus/config, while `10.0.0.248` tolerates the same Nodus
startup shape.

Useful next tests or changes should focus on SUBACK resolution rather than more
sensor-driver heap tuning:

- Fix and retest `MQTT.STARTUP_SUBSCRIBE_BEFORE_PUBLISH = true` for
  `avpd-0kl7sx+s1` against `10.0.0.246`. The `v0.26.150.9` run was configured
  with the flag, but serial evidence shows the retained publish-first path was
  still used. After the flag is actually honored, a pass would implicate the
  retained publish sequence as the receive-path poison; a failure would show
  raw subscribe receive is broken independent of publish ordering.
- Treat `mqtt_suback_stage=header:[Errno 116] ETIMEDOUT` as a poisoned
  MQTT/socket condition. Plain MQTT client rebuild may be insufficient because
  the next immediate TCP preflight can return `ENOMEM` with high Python heap.
- Consider recovery escalation that refreshes station/socket artifacts or hard
  resets after this exact SUBACK timeout pattern, rather than repeatedly
  rebuilding the MQTT adapter on the same fragile socket state.
- Broker-visible MQTT capture remains required when validating any startup
  ordering or recovery behavior change. Serial `published=1` alone is not
  proof of broker-visible success.

## 2026-05-30 Afternoon Follow-up: Retained `/meta` Delivery

Later May 30 tcpdump captures moved the primary suspect earlier than SUBACK.
The SUBACK timeout remains a useful symptom, but it is no longer the first
known broker-visible failure. The strongest evidence now points to incomplete
delivery of the long retained `nodus/avpd-0kl7sx/meta` publish when S1 is
enabled.

### Broker Capture Reliability

Mosquitto retained snapshots and live captures were useful but not sufficient
on their own during the mixed `v0.26.150.11` runs:

- Retained snapshots sometimes showed only older retained startup messages.
- Live `mosquitto_sub -R` captures could time out without receiving new
  messages even while serial reported local publish success.
- The investigation therefore switched to broker-side `tcpdump` on MQTT port
  `1883` to verify whether bytes for the Nodus connection actually reached the
  broker.

This reinforced the validation rule for this issue: serial-side
`published=1`, queue advancement, or a retained snapshot alone is not proof of
current-run MQTT success.

### `v0.26.150.12`: Raw Publish Chunking

`v0.26.150.12` added generic raw MQTT packet send chunking so long MQTT
packets are fed to the CircuitPython socket in bounded writes instead of one
large `send()`.

The `avpd-0kl7sx+s1` startup still failed against `10.0.0.246`. Serial still
reported local publish success for retained startup topics and then reached
the familiar device-command SUBACK timeout:

```text
boot version=v0.26.150.12
switch enabled=True channels=1
publish nodus/avpd-0kl7sx/meta bytes=1435 errors=none
publish nodus/avpd-0kl7sx/meta/switch bytes=490 errors=none
subscribe nodus/avpd-0kl7sx/config/set
    error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT
```

However, broker-side tcpdump showed the retained `/meta` publish itself was
not completely delivered. Only the first 512 bytes of the MQTT PUBLISH reached
the broker, and that same first 512-byte range was retransmitted. The remainder
of the MQTT packet was not observed, so the broker could not publish the
message to subscribers:

```text
10.0.0.221 > 10.0.0.246.1883: seq 26:538 length 512
... nodus/avpd-0kl7sx/meta ...
10.0.0.221 > 10.0.0.246.1883: seq 26:538 length 512
10.0.0.221 > 10.0.0.246.1883: seq 26:538 length 512
```

Because the raw publish was still QoS 0 in this firmware, Nodus could count the
local socket write as success even though the broker never received the
complete MQTT PUBLISH.

### `v0.26.150.13`: PUBACK Verification For Long Raw Publishes

`v0.26.150.13` changed long raw publishes on recv-capable sockets to MQTT QoS
1 and waited for PUBACK before counting the publish as successful. This did not
fix the transport failure, but it made the failure observable at the correct
operation.

Switch-enabled `avpd-0kl7sx+s1` against `10.0.0.246`:

```text
boot version=v0.26.150.13
switch enabled=True channels=1
op=publish topic=nodus/avpd-0kl7sx/meta retain=1 bytes=1435
    raw_publish_diag qos=1 pkt_id=1 pkt_bytes=1464 topic_len=22
    puback_ms=1001
    error=mqtt_puback_stage=header:[Errno 116] ETIMEDOUT
```

The matching tcpdump again showed only the first 512 bytes of the retained
`/meta` PUBLISH arriving and being retransmitted. No complete MQTT PUBLISH
arrived at the broker, and therefore no PUBACK was sent.

This changed the interpretation of the previous SUBACK timeout: in the failing
`avpd+s1` case, the socket path can already be broken during the retained
`/meta` publish. SUBACK is often the next visible timeout only because earlier
QoS 0 publishes did not require broker acknowledgement.

### `v0.26.150.13`: No-Switch Control

The same firmware, same AVPD sensor, same broker, and same IP path succeeded
when the S1 switch enable jumper was removed:

```text
boot version=v0.26.150.13
switch enabled=False channels=0
switch_channels ids=none labels=none
publish nodus/avpd-0kl7sx/meta bytes=1301
    qos=1 pkt_id=1 pkt_bytes=1330 puback_ms=27 errors=none
publish heartbeat errors=none
publish availability errors=none
subscribed=4 errors=none
ntp phase=synced
```

The no-switch tcpdump aligned with serial:

```text
CONNECT / CONNACK complete
10.0.0.221 > 10.0.0.246.1883: seq 26:1356 length 1330
    nodus/avpd-0kl7sx/meta
10.0.0.246.1883 > 10.0.0.221: seq 5:9 length 4
    PUBACK
heartbeat publish observed
availability publish observed
four device SUBSCRIBE/SUBACK exchanges observed
data publish observed
32 packets captured, 0 dropped
```

The retained no-switch `/meta` included:

```json
"capabilities":{"switch":false,"fwupdate":true,"log_transfer":true,"sensor":true}
"location_group":{"location":"Unknown","members":["avpd-0kl7sx"]}
```

There was no `S1-0kl7sx` member, no switch object, no `meta/switch`, and no
switch command subscription. This is strong evidence that AVPD/BME280 alone is
not sufficient to trigger the failure. Enabling the switch adds enough retained
startup metadata to move the main `/meta` MQTT packet from about 1330 bytes to
about 1464 bytes, and that larger retained publish is the first operation now
known to fail broker-visible validation.

### `v0.26.150.14`: Smaller Raw Send Chunks

`v0.26.150.14` is the next hardware experiment. It reduces raw MQTT socket send
chunks from 512 bytes to 256 bytes while keeping the long-publish verification
threshold at 512 bytes:

```text
MQTT_RAW_SEND_CHUNK_BYTES = 256
MQTT_RAW_VERIFY_PUBLISH_BYTES = 512
```

Keeping the verification threshold at 512 means ordinary mid-sized publishes
between 256 and 512 packet bytes remain QoS 0, while the long retained
`avpd+s1` `/meta` publish still uses QoS 1 and must receive PUBACK before the
firmware counts it as successful.

Expected hardware-test interpretation:

- Pass: `avpd-0kl7sx+s1` publishes retained `/meta` with
  `qos=1 pkt_bytes=1464`, receives a small `puback_ms`, and broker-side capture
  shows complete `/meta`.
- Fail with PUBACK timeout: smaller application-level chunks are still not
  enough; the Pico2 W/CircuitPython socket path is fragile for this long raw
  retained PUBLISH.

If `v0.26.150.14` still fails, the next likely fix should avoid requiring this
startup path to deliver a long retained `/meta` as one raw MQTT PUBLISH. The
main options are to keep the compact retained `/meta` below the known-good
envelope for this device class, move more switch material into retained
`meta/switch`, or use a different publish path for retained startup metadata.

### `v0.26.150.15`: Compact Switch Block In Main `/meta`

The `v0.26.150.14` tcpdump showed that the broker received exactly one
MSS-sized 1460-byte segment for the switch-enabled retained `/meta` packet and
never received the final 4 bytes of the 1464-byte MQTT packet. That made the
next fix a payload-size fix rather than another socket pacing change.

`v0.26.150.15` removes the duplicated `switch.location` field from the main
retained `/meta` switch block. The main `meta.switch` block still carries
`device_id`, `channel_count`, and `meta_topic`; retained `meta/switch` remains
the authoritative switch detail payload and still includes switch location.

The new host-side guard builds the observed `avpd-0kl7sx+s1` metadata shape
and asserts that the retained QoS 1 `/meta` MQTT packet stays at or below 1460
bytes. Hardware validation should confirm that the broker now sees the full
`/meta` packet and returns PUBACK before startup moves on.

The `v0.26.150.15` hardware run did confirm this. Serial showed the main
retained `/meta` publish at `bytes=1414`, `pkt_bytes=1443`, `qos=1`, and
`puback_ms=6`, followed by successful retained `meta/switch`, device
subscriptions, switch subscription, sensor data, and NTP sync. Tcpdump aligned:
the broker received the complete 1443-byte MQTT PUBLISH and returned PUBACK.

### `v0.26.150.16`: Cleanup After Root Cause Confirmation

After the compact `/meta` fix validated broker-visible startup success, the
temporary `STARTUP_SUBSCRIBE_BEFORE_PUBLISH` diagnostic flag and its alternate
startup path were removed. Publish-first startup is now the only runtime path:
retained identity/status publishes drain before command subscriptions, with the
existing clean-poll check before the first subscribe.

The noisy MQTT sync instrumentation was also reduced. Routine successful
publishes no longer log queue snapshots, adapter indexes, heap snapshots,
socket capabilities, or QoS/PUBACK summaries. Raw QoS 1 retained publish
failures and raw SUBACK failures still include detailed diagnostics because
those remain actionable failure signals.

### `v0.26.150.17`: EBADF Follow-Up Diagnostics

The first `v0.26.150.17` `co2-ykdvea` run confirmed that recovery could
survive an `mqtt_poll_failed:[Errno 9] EBADF` event, but the existing serial
and broker evidence did not pinpoint when the socket became invalid. In one
aligned broker capture, the last broker-visible message before recovery was a
normal `/data` publish at `06:07:04`, while serial did not enter MQTT recovery
until about `06:09:59`. With the steady-state heartbeat and availability cycle
still at roughly 120 seconds, the evidence window was too wide to determine
whether the poisoned socket followed `/data`, heartbeat, sensor availability,
or switch availability.

The follow-up diagnostics added compact "last ACK" context to the MQTT
transport:

- raw PUBACK/SUBACK reads record ACK kind, topic, packet id, elapsed time,
  timeout, socket state, and socket capabilities;
- EBADF poll failures include poll socket state/capabilities/timeout, the last
  publish diagnostic, the last loop diagnostic, and the last raw ACK diagnostic;
- successful routine publish logs remain quiet, so the extra context is only
  emitted when a failure makes it useful.

This did not change recovery policy. It was added to answer a narrower
question: when a later poll reports EBADF, what was the most recent broker-ACKed
operation and what socket shape was being polled?

### `v0.26.151.1` / `v0.26.151.2`: Narrow Rollback Toward `148.1`

The overnight `150.16`/`150.17` runs showed a regression in socket stability:
more MQTT recoveries and restarts than the `148.1` baseline, especially after
the long-publish QoS 1/PUBACK verification and generic 256-byte chunking work.
That made the next experiment a rollback toward the known-good `148.1` raw
publish behavior.

The operator clarification was important: the 256-byte chunking and PUBACK
work had been introduced for the retained startup `/meta` and `/meta/switch`
problem, not as a broad steady-state transport policy. The final `151.2` shape
therefore split the behavior:

- normal raw publishes use QoS 0 and send the MQTT packet as one socket write,
  matching the `148.1`-style steady-state path more closely;
- retained startup `/meta` and `/meta/switch` publishes remain identified
  explicitly and keep the constrained startup send path;
- long-publish QoS 1/PUBACK verification is disabled by default
  (`MQTT_RAW_VERIFY_PUBLISH_BYTES = 0`) and remains only as an optional
  diagnostic path for tests or temporary hardware experiments;
- SUBACK diagnostics remain, because startup subscribe failures are still a
  separate observable symptom;
- the steady-state availability refresh interval was lowered to 30 seconds for
  this debug run, so the next EBADF/socket-poisoning window should be much
  tighter than the earlier 120-second cadence.

`v0.26.151.1` was treated as an intermediate rollback candidate after startup
tails showed failures. `v0.26.151.2` is the narrower compromise: closer to
`148.1` for normal steady-state publishing while preserving the startup-specific
handling that was added for the AVPD + switch retained metadata envelope.

### `v0.26.151.2`: Initial Soak Read

The first multi-device `151.2` soak used these serial-log mappings:

- `cu.usbmodem133101.log`: `switch-w9umh8`
- `cu.usbmodem133301.log`: `aht-yuk0nv`
- `cu.usbmodem1334301.log`: `avpd-zbcalz`
- `cu.usbmodem1334101.log`: `co2-ykdvea`

Serial review from each final `151.2` boot showed materially quieter behavior
than `150.x`:

- `switch-w9umh8` hit one early publish failure on
  `nodus/S1-w9umh8/availability` with `[Errno 5] Input/output error`, then
  escalated through repeated direct-IP MQTT connect `ETIMEDOUT` to a hard
  reboot. After the reboot, no further MQTT/recovery failures were seen in the
  reviewed window. This device was on the USB hub port already suspected of
  extra Wi-Fi instability.
- `aht-yuk0nv` was clean from the final `151.2` boot: no publish, subscribe,
  poll, or reset markers in the reviewed window.
- `avpd-zbcalz` had two startup raw SUBACK timeouts on
  `nodus/avpd-zbcalz/config/set`, each followed by `ENOMEM` during reconnect.
  It recovered and then ran cleanly in the reviewed window.
- `co2-ykdvea` was clean from the final `151.2` boot: no EBADF poll failures,
  no MQTT recovery, and no reset markers in the reviewed window.

Most importantly for the regression investigation, the reviewed `151.2`
windows did not show the `150.x` retained `/meta` PUBACK-timeout signature and
did not show EBADF poll failures on the stable devices. Broker-visible
validation is still required before treating the behavior as fully accepted,
but this run supports keeping the broad QoS 1/PUBACK verification disabled and
continuing the soak with the 30-second debug cadence.
