# Sensorius Contract

This document is the canonical forward-only contract between current
`cPyNodus_II` firmware and current Sensorius.

The two projects move together. Old topic names and compatibility shims do not
define the contract. When other docs drift, this document wins.

## Principles

- AP bootstrap uses only `/itaot-meta` and `/itaot-init`.
- Normal runtime sync uses only MQTT.
- Nodus publishes retained full `meta` on connect/reconnect.
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
- `nodus/<sensor_id>/availability`
- `nodus/<sensor_id>/data`
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
- retained `nodus/<channel_id>/state`
- `nodus/<device_id>/meta/patch`

### Calibration

- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/meta/patch`

## Onboarding MQTT Flow

1. Nodus joins Wi-Fi and MQTT.
2. Nodus publishes `nodus/<device_id>/onboard/hello`.
3. Sensorius validates the token and sends one full onboarding
   `nodus/<device_id>/config/set` envelope.
4. Nodus publishes `config/ack`.
5. Nodus publishes `config/result`.
6. Nodus publishes retained `nodus/<device_id>/meta`.

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

Retained `nodus/<device_id>/meta` is the authoritative startup snapshot.
Sensorius uses it to rebuild the local shadow copy of Nodus state.

The payload must include:

- top-level `schema`, `device_id`, `hostname`, `serial`, `version`, `type`
- `capabilities`
- `status.heartbeat_topic`
- `mqtt.broker`, `mqtt.broker_ip`, `mqtt.active_broker`, `mqtt.port`
- `location_group.location`, `location_group.members`
- `sensor.sensor_id`, `sensor.location`, `sensor.data_topic`,
  `sensor.event_topic`, `sensor.availability_topic`
- `switch.device_id`, `switch.location`
- per-channel `index`, `label`, `channel_id`, `enable_pin`, `pin`,
  `state_topic`, `set_topic`, `result_topic`, `availability_topic`

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

