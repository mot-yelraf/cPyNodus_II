# Sensorius Contract

This document is the canonical forward-only contract between current
`cPyNodus_II` firmware and current Sensorius.

The two projects move together. Old topic names and compatibility shims do not
define the contract. When other docs drift, this document wins.

## Principles

- AP bootstrap uses only `/itaot-meta` and `/itaot-init`.
- Normal runtime sync uses only MQTT.
- OTA uses MQTT only for the prepare/result control path; package bytes move
  over HTTP while Nodus is in temporary OTA mode.
- Nodus publishes retained compact `meta` on connect/reconnect.
- Nodus publishes retained `meta/switch` with detailed switch channel topics in
  the startup identity publish batch when switch channels are present.
- After accepted runtime changes, Nodus publishes only `meta/patch`.
- Sensorius paces ordinary runtime config writes one key at a time per
  physical Nodus host and waits for `ack` plus successful `result`.

## AP Bootstrap

Routes exposed in AP mode:

- `GET /itaot-meta`
- `POST /itaot-init`

Canonical `/itaot-init` request:

```json
{
  "onboard_token": "token-123",
  "ssid": "MyWiFi",
  "password": "my-password",
  "hostname": "co2-ykdvea",
  "mqtt": {
    "broker_host": "samhain.local",
    "broker_port": 1883
  }
}
```

Canonical success response:

```json
{
  "accepted": true,
  "rebooting": true
}
```

Bootstrap rules:

- Validate required fields and types.
- Persist only supported Network, MQTT, and Profile settings.
- Keep onboarding protocol state out of normal TOML config schema.
- If `mqtt.active_profile` is omitted and `mqtt.broker_host` is present,
  infer `ACTIVE_PROFILE = "sensorius"`.

## MQTT Topics

### Startup and steady state

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<device_id>/meta/switch`
- `nodus/<sensor_id>/availability`
- `nodus/<sensor_id>/data`
- `nodus/<channel_id>/event`
- `nodus/<channel_id>/state`
- `nodus/<channel_id>/availability`

### Onboarding

- `nodus/<device_id>/onboard/hello`
- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`

### Ordinary runtime config

- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`
- `nodus/<device_id>/meta/patch`

### Switch runtime config

- `nodus/<channel_id>/config/set`
- `nodus/<channel_id>/config/ack`
- `nodus/<channel_id>/config/result`
- `nodus/<channel_id>/event`
- retained `nodus/<channel_id>/state`
- `nodus/<device_id>/meta/patch`
- optional Sensorius-owned retained empty `nodus/<channel_id>/config/set`
  cleanup when Sensorius used a retained command

### Calibration

- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/meta/patch`

### Firmware Update

- `nodus/<device_id>/fwupdate`
- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`

## Onboarding MQTT Flow

1. Nodus joins Wi-Fi and MQTT.
2. Nodus publishes `nodus/<device_id>/onboard/hello`.
3. Sensorius validates the token and sends one full onboarding
   `nodus/<device_id>/config/set` envelope.
4. Nodus publishes `config/ack`.
5. Nodus publishes `config/result`.
6. Nodus publishes retained `nodus/<device_id>/meta`.
7. If switch channels are present, Nodus publishes retained
   `nodus/<device_id>/meta/switch` in the startup identity publish batch.

Canonical `onboard/hello` payload:

```json
{
  "onboard_token": "token-123",
  "device_id": "co2-ykdvea",
  "hostname": "co2-ykdvea",
  "serial": "ykdvea",
  "type": "pico2w",
  "version": "v0.26.111.15",
  "capabilities": {
    "sensor": true,
    "switch": true
  }
}
```

## Runtime Payloads

Canonical `/data` payload:

```json
{
  "schema": "nodus-sensor-data/v1",
  "sensor_id": "co2-ykdvea",
  "device": "co2",
  "location": "OfficeDesk",
  "values": {
    "CO2": 792,
    "Temperature": 22.8
  },
  "timestamp": 946709424
}
```

Canonical heartbeat payload:

```json
{
  "schema": "nodus-heartbeat/v1",
  "device_id": "co2-ykdvea",
  "status": "online",
  "timestamp": 946709424
}
```

Canonical switch state payload:

```json
{
  "schema": "nodus-switch-state/v1",
  "device_id": "switch-ykdvea",
  "channel_id": "S1-ykdvea",
  "label": "Fan",
  "state": "ON",
  "timestamp": 946709424
}
```

## Retained `meta`

Retained `nodus/<device_id>/meta` is the compact authoritative startup
snapshot. Sensorius uses it to materialize the device, sensor, core MQTT
topics, and switch presence quickly after connect/reconnect.

The payload must include:

- top-level `schema`, `device_id`, `hostname`, `serial`, `version`, `type`
- `capabilities`
- `status.heartbeat_topic`
- `network.ssid`, `network.password`, `network.hostname`
- `profile.active_profile`
- `mqtt.broker`, `mqtt.broker_ip`, `mqtt.active_broker`, `mqtt.port`,
  `mqtt.use_tls`, `mqtt.username`, `mqtt.password`, `mqtt.base_topic`
- `fwupdate.schema`, `fwupdate.transport`, `fwupdate.prepare_topic`,
  `fwupdate.ack_topic`, `fwupdate.result_topic`
- `location_group.location`, `location_group.members`
- `sensor.sensor_id`, `sensor.location`, `sensor.data_topic`,
  `sensor.event_topic`, `sensor.availability_topic`,
  `sensor.display_metrics`, `sensor.display_styles`
- `switch.device_id`, `switch.location`, `switch.channel_count`, and
  `switch.meta_topic` when switch capability is present

The startup `meta` payload intentionally does not include
`switch.channels[*]`. The detailed per-channel switch topic map is published
separately on retained `nodus/<device_id>/meta/switch`.

Sensorius compatibility rule:

1. If retained `meta.switch.channels` exists, parse it as the legacy embedded
   switch topic map.
2. Else if retained `meta.switch.meta_topic` exists, read retained
   `meta.switch.meta_topic` and parse `nodus-meta-switch/v1`.
3. Else if `meta.switch.channel_count > 0`, derive the default topic
   `nodus/<device_id>/meta/switch` and read retained `nodus-meta-switch/v1`
   as a fallback.
4. Else treat the device as having no switch channel topic map yet and wait
   for a later retained `meta` or `meta/switch`.

Password fields in retained `meta` use the same `obf1:` obfuscation format as
persisted TOML password fields. They are not plaintext.

## Retained `meta/switch`

Retained `nodus/<device_id>/meta/switch` is the authoritative switch channel
topic map for Sensorius control. Nodus queues it with the retained startup
identity publish batch, before runtime command subscriptions are allowed to
block progress. Sensorius should merge it with the latest retained `meta` for
switch control materialization.

Canonical topic:

- `nodus/<device_id>/meta/switch`

Canonical payload:

```json
{
  "schema": "nodus-meta-switch/v1",
  "device_id": "co2-ykdvea",
  "switch_device_id": "switch-ykdvea",
  "location": "OfficeDesk",
  "channel_count": 2,
  "channels": [
    {
      "index": 1,
      "label": "Fan",
      "channel_id": "S1-ykdvea",
      "state": false,
      "event_topic": "nodus/S1-ykdvea/event",
      "state_topic": "nodus/S1-ykdvea/state",
      "set_topic": "nodus/S1-ykdvea/config/set",
      "ack_topic": "nodus/S1-ykdvea/config/ack",
      "result_topic": "nodus/S1-ykdvea/config/result",
      "availability_topic": "nodus/S1-ykdvea/availability"
    },
    {
      "index": 2,
      "label": "Humidifier",
      "channel_id": "S2-ykdvea",
      "state": false,
      "event_topic": "nodus/S2-ykdvea/event",
      "state_topic": "nodus/S2-ykdvea/state",
      "set_topic": "nodus/S2-ykdvea/config/set",
      "ack_topic": "nodus/S2-ykdvea/config/ack",
      "result_topic": "nodus/S2-ykdvea/config/result",
      "availability_topic": "nodus/S2-ykdvea/availability"
    }
  ],
  "timestamp": 946709424
}
```

`meta/switch` must include:

- top-level `schema`, `device_id`, `switch_device_id`, `location`,
  `channel_count`, `channels`, and `timestamp`
- per-channel `index`, `label`, `channel_id`, `state`, `event_topic`,
  `state_topic`, `set_topic`, `ack_topic`, `result_topic`, and
  `availability_topic`

Hardware pin fields are not part of the MQTT control contract. If Sensorius
needs pin diagnostics, use `/itaot-meta` or a later diagnostic contract rather
than startup MQTT metadata.

## Retained Command Cleanup

`/set` topics are command topics, not state topics. Sensorius should publish
commands non-retained unless a specific command flow requires retained delivery.

When Sensorius intentionally publishes any `/set` command retained, Sensorius
owns removing that retained command after successful handling by publishing an
empty retained payload to the same topic. Nodus ignores empty `/set` payloads.

Sensorius retained-command sequence:

1. Publish the command to the relevant `/set` topic with `retain = true`.
2. Wait for the correlated `ack`.
3. Wait for the correlated successful `result`.
4. Consume the correlated retained state, event, or `meta/patch` update as
   applicable.
5. Publish an empty retained payload to the same `/set` topic.

Current implementation:

- Nodus ignores empty payloads on switch `nodus/<channel_id>/config/set`.
- Nodus ignores empty payloads on ordinary device
  `nodus/<device_id>/config/set`.
- Nodus ignores empty payloads on `nodus/<device_id>/calibration/set`.
- Nodus does not publish empty retained cleanup payloads to `/set` topics.

## Ordinary `config/set`

Canonical topic:

- `nodus/<device_id>/config/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cfg-123",
  "payload": {
    "updates": [
      {
        "section": "Sensor",
        "key": "LOCATION",
        "value": "OfficeDesk",
        "name": "sensor_i2c.toml"
      }
    ]
  },
  "restart": false
}
```

Canonical replies:

```json
{"message_id":"cfg-123","accepted":true,"duplicate":false}
{"message_id":"cfg-123","applied":true,"updated":1,"duplicate":false,"error":""}
```

Implemented behavior:

- Nodus publishes `config/ack` after a valid envelope is accepted for
  handling.
- Duplicate `message_id` values produce `config/ack` with
  `duplicate = true` and `config/result` with `applied = true`,
  `updated = 0`, and `duplicate = true`.
- Accepted non-duplicate writes publish `config/result` and a non-retained
  `meta/patch` with `source = "config_set"`.
- Failed validation or rejected writes publish `config/result` with
  `applied = false` and an error string.
- Empty payloads on `nodus/<device_id>/config/set` are ignored. This allows
  Sensorius retained command cleanup publishes to be received safely after
  reconnect.
- Nodus does not clear `nodus/<device_id>/config/set`; Sensorius owns retained
  command cleanup for commands it publishes retained.

## Switch `config/set`

Canonical topic:

- `nodus/<channel_id>/config/set`

Canonical switch-control envelope:

```json
{
  "message_id": "cfg-123",
  "payload": {
    "updates": [
      {
        "section": "Switch",
        "key": "SWITCH_1_LAST_STATE",
        "value": true,
        "name": "switch.toml"
      }
    ]
  },
  "restart": false
}
```

Forward-only rule:

- JSON `config/set` is the canonical switch-control contract.
- Plain `ON` and `OFF` payloads may still be tolerated by firmware, but they
  are not the documented forward contract.

Implemented behavior:

- Empty payloads on `nodus/<channel_id>/config/set` are ignored. This allows
  Sensorius retained command cleanup publishes to be received safely after
  reconnect.
- Nodus publishes channel-scoped `config/ack` after a valid switch command is
  accepted for handling.
- Nodus applies the switch state, publishes channel-scoped `config/result`,
  publishes `event`, publishes retained `state`, and publishes a non-retained
  device `meta/patch` with `source = "switch_set"`.
- If the filesystem is writable, Nodus persists the channel
  `SWITCH_<n>_LAST_STATE` update into `switch.toml`. If persistence fails, the
  command may still be applied locally and the command result carries
  `persistence_mode = "volatile"` in serial logging.
- Nodus does not clear `nodus/<channel_id>/config/set`; Sensorius owns retained
  command cleanup for commands it publishes retained.

## `calibration/set`

Canonical topic:

- `nodus/<device_id>/calibration/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cal-123",
  "action": "apply",
  "payload": {
    "offsets": [
      {
        "key": "Calibration.Device.TEMP_OFFSET",
        "value": 1.5
      }
    ]
  }
}
```

Canonical replies:

- `calibration/ack`
- `calibration/result`
- `meta/patch` with `source = "calibration_set"` for accepted writes

Implemented behavior:

- Nodus publishes `calibration/ack` after a valid calibration envelope is
  accepted for handling.
- Duplicate `message_id` values produce `calibration/ack` and
  `calibration/result` with `applied = true`, `updated = 0`, and no
  `meta/patch`.
- `action = "apply"`, `"set"`, or `"update"` writes accepted calibration
  values, publishes `calibration/result`, and publishes non-retained
  `meta/patch` with `source = "calibration_set"`.
- `action = "status"` republishes retained
  `nodus/<sensor_id>/event/calibration_status` and publishes a correlated
  `calibration/result`.
- Soil pH session actions publish command-scoped `calibration/result` plus the
  soil calibration event topics described in
  `docs/calibration_mqtt_contract.md`.
- Empty payloads on `nodus/<device_id>/calibration/set` are ignored. This
  allows Sensorius retained command cleanup publishes to be received safely
  after reconnect.
- Nodus does not clear `nodus/<device_id>/calibration/set`; Sensorius owns
  retained command cleanup for commands it publishes retained.

## `fwupdate`

Canonical prepare topic:

- `nodus/<device_id>/fwupdate`

Canonical prepare payload:

```json
{
  "schema": "nodus-fwupdate/v1",
  "message_id": "fw-20260504T193222Z",
  "command": "prepare",
  "package_id": "ota-tagA-to-tagB"
}
```

Canonical replies:

- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`

Implemented behavior:

- Nodus subscribes to `fwupdate` in normal MQTT runtime.
- For accepted `prepare`, Nodus persists private OTA state, publishes `ack`
  and a prepared `result`, publishes offline availability/heartbeat, then
  soft-reboots into temporary OTA mode.
- OTA mode does not run MQTT. Sensorius or the CLI transfers package files
  over HTTP using the endpoints in `docs/ota.md`.
- After successful apply and reboot back into the prior profile, Nodus
  publishes a non-retained `fwupdate/result` with `phase = "applied"`,
  `applied = true`, `package_id`, and `prior_profile`.

## `meta/patch`

Canonical non-retained patch payload:

```json
{
  "schema": "nodus-meta-patch/v1",
  "device_id": "co2-ykdvea",
  "timestamp": 946709500,
  "source": "config_set",
  "message_id": "cfg-123",
  "sections": ["Sensor"],
  "updates": [
    {
      "section": "Sensor",
      "key": "LOCATION",
      "value": "OfficeDesk"
    }
  ]
}
```

Patch rules:

- `meta/patch` is the incremental sync stream after startup.
- It does not replace retained startup `meta`.
- Accepted config, switch, and calibration writes should emit `meta/patch`.

## Deprecated Doc Shapes

These shapes are deprecated and should not be treated as canonical:

- `nodus/<channel_id>/set`
- switch-control docs centered on plain `ON` and `OFF`
- docs that imply ordinary runtime config writes trigger a full retained `meta`
  refresh
