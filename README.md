# cPyNodus_II

`cPyNodus_II` is a fresh CircuitPython 9.2.8 codebase for the Raspberry Pi Pico 2 W.

This project is intentionally not a wholesale copy of `cPyNodus`. It uses the
smaller `cPySwitch` runtime shape as a starting point and treats the current
`cPyNodus` behavior as a specification to re-implement in controlled slices.

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
- mDNS-preferred MQTT broker connection with pinned-IP fallback when configured
- switch runtime initialization, MQTT command intake, retained state publish, `config/ack`, `config/result`, and `meta/patch`
- config/calibration apply plumbing with TOML persistence support and ROFS-aware volatile mode
- host-testable sensor, switch, payload, publish-cycle, and transport layers

## Validated now

Validated on a Raspberry Pi Pico 2 W running CircuitPython `9.2.8`:

- switch-only `sensorius` profile boot
- Wi-Fi join from root `settings.toml`
- MQTT connect to Sensorius broker by hostname
- switch command handling on `nodus/<channel_id>/config/set`
- relay toggle on-device from Sensorius commands
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
- periodic health lines with network state, broker, `free_mem`, and `mem_alloc`

## Still incomplete

The following areas are still in progress:

- sensor-backed on-device validation
- combined sensor + switch runtime validation
- AP onboarding and local web flows
- full reconnect/recovery hardening under adverse network conditions
- broader long-duration soak coverage beyond the current switch-only case

## Next steps

- continue the switch-only overnight soak
- validate one sensor-backed device on hardware
- validate combined sensor + switch behavior
- tighten reconnect and recovery behavior only if soak logs show a real issue
