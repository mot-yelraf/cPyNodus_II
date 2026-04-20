# MQTT

The device can publish sensor metrics and receive switch commands over MQTT to a single MQTT Broker. Sensorius (Automatio Instrumentorum) is the primary broker Nodus was developed for and supports device discovery. Home Assistant is an option for the MQTT Broker.

## Behavior

- By default MQTT uses port 1883 on both Sensorius AI and Home Assistant. 
- Sensorius AI uses anonymous access; Home Assistant requires a username and password.
- TLS is enabled when configured or when broker port is 8883.
- Home Assistant discovery can be enabled with configurable prefixes.
- Switch control is handled via `/set` topics.
- Events and state are published to `/event` and `/state` topics.
- Runtime identity metadata is published as a retained payload on `nodus/<device_id>/meta` (`schema = "nodus-meta/v1"`).
- Runtime config deltas are published on `nodus/<device_id>/meta/patch` (`schema = "nodus-meta-patch/v1"`).
- When `ACTIVE_PROFILE = "sensorius"`, `ACTIVE_PROFILE = "homeassistant"`, or `ACTIVE_PROFILE = "weewx"`, Nodus does not start the normal-mode webserver.
- Provisioning for these MQTT-only profiles is expected to happen in `nodusweb`/AP mode before rebooting into the target profile.

## Core Topic Contract (Sensorius)

- Device heartbeat:
  - `nodus/<device_id>/status/heartbeat` (retained online/offline status envelope)
- Sensor data:
  - `nodus/<sensor_id>/data`
- Sensor availability:
  - `nodus/<sensor_id>/availability` (retained online/offline)
- Switch channels:
  - `nodus/<channel_id>/state` (retained `ON`/`OFF`)
  - `nodus/<channel_id>/config/set` (command topic consumed by Nodus)
  - `nodus/<channel_id>/config/result` (compact apply result envelope from Nodus)
  - `nodus/<channel_id>/availability` (retained online/offline)
- Runtime metadata:
  - `nodus/<device_id>/meta` (retained; includes location, channel IDs, labels, and per-channel topics)
  - `nodus/<device_id>/meta/patch` (non-retained; accepted runtime config deltas for Sensorius)

## Source Of Truth And Sync Model

- Nodus TOML files are the source of truth for accepted device configuration.
- Sensorius should treat retained `nodus/<device_id>/meta` as the authoritative full snapshot used to initialize or rebuild its local copy of Nodus state.
- Nodus publishes that full retained `meta` payload after successful MQTT connect/reconnect.
- After startup, Sensorius sends runtime config writes to `nodus/<device_id>/config/set`, typically one accepted key update at a time.
- Nodus applies those updates to its TOMLs, publishes `config/ack` and `config/result`, and then emits only `nodus/<device_id>/meta/patch` for the accepted delta.
- Nodus applies calibration offset writes from `nodus/<device_id>/calibration/set` to its TOMLs, publishes `calibration/ack` and a compact `calibration/result`, and then emits `nodus/<device_id>/meta/patch` with `source = "calibration_set"` for the accepted delta.
- Nodus does not automatically republish the full retained `meta` payload after ordinary runtime config changes.
- If Sensorius or Nodus restarts later, the next startup/reconnect full `meta` publish re-establishes the authoritative snapshot.

## `nodus-meta/v1` Payload

Published retained at `nodus/<device_id>/meta` after successful MQTT connect/reconnect.

```json
{
  "schema": "nodus-meta/v1",
  "device_id": "aqi-x943fm",
  "hostname": "aqi-x943fm",
  "serial": "x943fm",
  "type": "nodus",
  "version": "vX.Y.Z",
  "capabilities": {
    "sensor": true,
    "switch": true
  },
  "sensor": {
    "sensor_id": "aqi-x943fm",
    "location": "TestLab",
    "display_metrics": ["Air Quality", "Temperature", "Rel-Humidity"],
    "display_styles": ["graph24hr", "graph24hr", "gauge"],
    "data_topic": "nodus/aqi-x943fm/data",
    "event_topic": "nodus/aqi-x943fm/event",
    "availability_topic": "nodus/aqi-x943fm/availability"
  },
  "status": {
    "heartbeat_topic": "nodus/aqi-x943fm/status/heartbeat"
  },
  "switch": {
    "device_id": "switch-x943fm",
    "location": "TestLab",
    "channels": [
      {
        "index": 1,
        "label": "Fan",
        "channel_id": "S1-x943fm",
        "enable_pin": "GP5",
        "pin": "GP28",
        "state": false,
        "event_topic": "nodus/S1-x943fm/event",
        "state_topic": "nodus/S1-x943fm/state",
        "set_topic": "nodus/S1-x943fm/config/set",
        "result_topic": "nodus/S1-x943fm/config/result",
        "availability_topic": "nodus/S1-x943fm/availability"
      },
      {
        "index": 2,
        "label": "Light",
        "channel_id": "S2-x943fm",
        "enable_pin": "GP10",
        "pin": "GP21",
        "state": false,
        "event_topic": "nodus/S2-x943fm/event",
        "state_topic": "nodus/S2-x943fm/state",
        "set_topic": "nodus/S2-x943fm/config/set",
        "result_topic": "nodus/S2-x943fm/config/result",
        "availability_topic": "nodus/S2-x943fm/availability"
      }
    ]
  },
  "location_group": {
    "location": "TestLab",
    "members": ["aqi-x943fm", "S1-x943fm", "S2-x943fm"]
  },
  "timestamp": 1763859546
}
```

### Sensorius Consumption Notes

- Prefer MQTT metadata (`nodus/<device_id>/meta`) over `/itaot-meta` for steady-state discovery/materialization.
- Treat retained `meta` as the full snapshot and `meta/patch` as the steady-state incremental sync stream.
- Use `switch.channels[*].channel_id` + topic fields as source of truth for switch tile creation.
- For command confirmation, consume `result_topic` when present and keep retained `state_topic` as the live applied state signal.
- Group sensor and switch tiles by `location_group.location` (fallback: `switch.location`, then `sensor.location`).
- `/itaot-meta` remains optional diagnostic enrichment and should not be required for online device rendering.

## Runtime Command Modules

- `cPyMiniMQTT.py` is the local transport shim over Adafruit MiniMQTT. It is intentionally limited to send/recv compatibility fixes and transport diagnostics for Pico2 W socketpool behavior.
- `cPyMQTTCommandHandler.py` is the lightweight callback wrapper that intercepts device runtime command topics without moving heavy logic onto the MQTT startup path.
- `cPyMQTTConfigHandler.py` owns runtime `config/set` apply, ack/result publishing, and `meta/patch` emission.
- `cPyMQTTCalibrationHandler.py` owns calibration command parse/ack/result routing and hands device/system/soil apply work to the narrower calibration worker modules.
- Full retained `meta` publishing stays out of the runtime config path and belongs to startup/reconnect handling.

## Notes

- Keep publish intervals conservative to reduce power usage.
- If MQTT is disabled, the device still runs locally.
- Calibration MQTT contract for Sensorius integration: see `docs/calibration_mqtt_contract.md`.

## Troubleshooting

### MQTT Publish Stall With False Local Success

Field testing on Pico 2 W devices has shown a failure mode where:

- Nodus logs local MQTT publish success (`ok=True`, normal `Published data ...` lines).
- Broker-observed traffic stops after startup or only retained startup topics arrive.
- Serial logs may also show repeated `~10s` switch-state or sensor publish timings.

When this specific failure mode appears, a normal CircuitPython reflash by itself may not fix it.

Observed remediation:

1. Save the device TOML files.
2. Flash `flash_nuke.uf2`.
3. Flash a fresh CircuitPython `9.2.8` UF2.
4. Deploy a clean Nodus build.
5. Restore the TOML files.

In recent validation, two separate Nodus devices that exhibited this MQTT publish-stall / broker-mismatch behavior were restored to normal operation only after the full `flash_nuke.uf2` + fresh CircuitPython reflash sequence. A plain CircuitPython reflash alone did not clear the issue.
