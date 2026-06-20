# cPyNodus_II

`cPyNodus_II` is CircuitPython firmware for Nodus sensor/switch devices on
verified `pico2w` and `xesp32s3` targets.

Verified target builds:

- `pico2w`: Raspberry Pi Pico 2 W running CircuitPython `9.2.8`
- `xesp32s3`: Seeed Studio XIAO ESP32-S3 Sense running CircuitPython `10.2.1`

`cPyNodus_II` is based on the earlier `cPyNodus` project. It was produced with
AI-agent assistance from work previously done in `cPyNodus`, then substantially
tested and modified by the human operator to reach the desired device behavior.

## Principles

- Keep MQTT transport minimal and testable.
- Add behavior in phases, not by bulk porting.
- Separate transport, runtime orchestration, and feature modules.
- Prefer explicit state transitions over implicit cross-module coupling.
- Require host tests and on-device soak validation for each slice.

## Planned slices

1. Boot, settings, profile selection, and startup plan
2. Wi-Fi and MQTT connect/reconnect lifecycle
3. Sensor-only steady-state publish
4. Switch-only command handling
5. Combined sensor + switch runtime
6. Config apply and metadata patching
7. Calibration flows
8. AP/onboarding and local web flows

## Current status

The repository is no longer just a scaffold. Current implemented slices include:

- TOML-backed runtime configuration loading for `settings.toml`, `switch.toml`, `sensor_i2c.toml`, and `sensor_soil.toml`
- startup planning and runtime capability detection
- Wi-Fi bootstrap and MQTT client lifecycle
- mDNS device hostname publishing for `nodusweb` and OTA HTTP; MQTT profiles
  stay IP-literal and do not run mDNS in steady state
- switch runtime initialization, MQTT command intake, retained state publish, `config/ack`, `config/result`, and `meta/patch`
- config/calibration apply plumbing with TOML persistence support and ROFS-aware volatile mode
- host-testable sensor, switch, payload, publish-cycle, and transport layers

## Validated now

Validated on supported target hardware:

- `pico2w`: Raspberry Pi Pico 2 W running CircuitPython `9.2.8`
- `xesp32s3`: Seeed Studio XIAO ESP32-S3 Sense running CircuitPython `10.2.1`

Validated runtime behavior includes:

- switch-only `sensorius` profile boot and runtime
- combined sensor + switch `sensorius` profile boot and runtime
- Wi-Fi join from root `settings.toml`
- MQTT connect to Sensorius broker by configured or startup-resolved broker IP
- switch command handling on `nodus/<channel_id>/config/set`
- relay toggle on-device from Sensorius commands
- sensor telemetry publish on `nodus/<sensor_id>/data`
- ordinary device `config/set` persistence, `config/result`, `meta/patch`, and
  live runtime update
- `calibration/set` persistence, `calibration/result`, `meta/patch`, and live
  runtime offset update for supported calibration fields
- root filesystem TOML read/write diagnostics through the same app persistence
  path used for config and calibration writes
- publish sequence for switch commands:
  - `nodus/<channel_id>/config/ack`
  - retained `nodus/<channel_id>/state`
  - `nodus/<channel_id>/config/result`
  - `nodus/<device_id>/meta/patch`
  - retained clear on `nodus/<channel_id>/config/set`
- multi-hour switch-only soak with stable MQTT connection and recovering heap usage

Current on-device diagnostics include:

- boot summary with profile, network phase, MQTT phase, and feature state
- network SSID, hostname, and IPv4 at startup
- switch channel IDs and labels at startup
- timestamped MQTT command logs for applied switch commands
- timestamped MQTT command logs for applied config and calibration commands
- periodic health lines with network state, broker, `free_mem`, and `mem_alloc`

## Still incomplete

The following areas are still in progress:

- OTA prepare, temporary HTTP transfer mode, and post-update recovery validation
- Sensorius `Add Device` onboarding regression validation
- AP onboarding and local web flow validation for the current firmware slice
- longer recovery and reconnect soak runs under adverse network conditions

## Next steps

- run an OTA prepare and HTTP transfer smoke test on a writable device
- rerun the production Sensorius `Add Device` flow end to end
- continue long-duration sensor + switch and switch-only soaks
- tighten reconnect and recovery behavior only if soak logs show a real issue
