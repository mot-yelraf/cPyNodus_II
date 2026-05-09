# Platform Baseline - 2026-05-09

Debug notes for the current Sensorius/Nodus test platform.

## Software Versions

- Sensorius: `v0.26.129.2`
- Nodus devices: `v0.26.129.2`
- Date verified as baseline: `2026-05-09`

## Sensorius Hosts

### `sensorius-hub-3`

- Direct sensors:
  - `avpd`
  - `co2`
- Weather station:
  - WeeWX Acurite weather station

### `sensoria-hub-0`

- Direct switch hardware:
  - 3-relay switch HAT
- Nodus devices:
  - `aqi-x943rm` + `S1` + `S2`
  - `co2-v5p04u`
  - `soil-bd1234`
  - `switch-w9umh8` switch-only device

## Additional Nodes

### `samhain`

- `co2-ykdvea` + `S1` + `S2`
- `aqi-wfcp7p` + `S1`
- `aht-rvwi73`
- `apvpd-tqug2v`
- `lux-wf6ama`

### `homeassistant`

- `co2-ph244`

## Verification Status

- Switch toggle: verified working
- OTA functionality: not yet verified

## Follow-Up Checks

- Verify OTA discovery and update flow.
- Confirm OTA behavior across sensor-only, switch-only, and sensor+switch Nodus devices.
- Record any MQTT topic, Home Assistant discovery, or reboot behavior observed during OTA testing.
