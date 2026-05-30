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
