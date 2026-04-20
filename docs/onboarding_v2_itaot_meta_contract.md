# cPyNodus `/itaot-meta` Contract (Onboarding V2 UI Metadata)

## Purpose
Provide a compact metadata payload for Sensorius UI enrichment during/after onboarding, with MQTT remaining the primary integration channel.

`/itaot-meta` is read-only and does not change device settings.

## Endpoint
- `GET /itaot-meta`

## Relationship to Other V2 APIs
1. Config apply flow remains:
   - `POST /itaot-init`
   - MQTT `nodus/<device_id>/onboard/hello`
   - MQTT `nodus/<device_id>/config/set`
   - MQTT `.../config/ack`
   - MQTT `.../config/result`
2. `/itaot-meta` is optional enrichment data for UI and diagnostics.
3. `/itaot-meta` is a fallback/diagnostic endpoint only; MQTT remains authoritative for device state and onboarding progression.

## Response Shape (`itaot-meta/v1`)
Example:

```json
{
  "schema": "itaot-meta/v1",
  "version": "v0.26.053.0",
  "origin": "nodus",
  "device_id": "aqi-x943fm",
  "network": {
    "hostname": "aqi-x943fm",
    "ssid": "PeaceHill",
    "ipv4addr": "192.168.1.40"
  },
  "device": {
    "type": "nodus",
    "capabilities": {
      "sensor": true,
      "switch": true
    }
  },
  "endpoints": {},
  "sensor": {
    "present": true,
    "device": "aqi",
    "sensor_id": "aqi-x943fm",
    "serial": "x943fm",
    "location": "Greenhouse",
    "active_sensor_file": "sensor_i2c.toml",
    "display_metrics": ["CO2", "Temperature"],
    "calibration": {
      "calibrated": true,
      "status": "Calibrated"
    }
  },
  "switch": {
    "present": true,
    "device_id": "switch-x943fm",
    "serial": "x943fm",
    "location": "Greenhouse",
    "channels": [
      {
        "index": 1,
        "label": "Fan",
        "channel_id": "S1-x943fm",
        "state": false,
        "enabled": true
      }
    ]
  },
  "location_group": {
    "id": "x943fm",
    "members": ["aqi-x943fm", "S1-x943fm"],
    "label": "SENSOR - aqi-x943fm SWITCH - S1-x943fm"
  }
}
```

## Field Notes
1. `schema`:
   - Fixed discriminator for contract versioning.
2. `device_id`:
   - Derived in priority order: `sensor_id`, `switch_device_id`, `hostname`.
3. `network`:
   - Mirrors runtime identity data useful for UI cards.
4. `device.capabilities`:
   - Presence booleans for sensor/switch features.
5. `endpoints`:
   - Reserved for future capability hints.
   - Current contract returns `{}` and does not advertise TOML fetch endpoints.
6. `sensor`:
   - Includes display metric labels and calibration status for UI.
7. `switch.channels`:
   - Includes channel label/id/current state per enabled channel.
8. `location_group`:
   - Grouping metadata for combined sensor/switch display.

## Size Goals and Exclusions
`/itaot-meta` intentionally excludes high-cardinality per-pin MQTT topic maps:
- `mqtt_switch_topics`
- `mqtt_switch_state_topics`
- `mqtt_switch_command_topics`
- `mqtt_switch_availability_topics`

This keeps payload smaller for constrained transports.

## Error Behavior
1. On payload-build degradation, Nodus will return best-effort metadata from minimal identity fields.
2. HTTP status:
   - `200` on success
   - `400` on unrecoverable handler failure

## Sensorius Integration Guidance
1. Treat missing fields as optional; default defensively.
2. Use `schema == "itaot-meta/v1"` to select parser.
3. Do not block onboarding success on `/itaot-meta`; MQTT config flow is authoritative for onboarding.
