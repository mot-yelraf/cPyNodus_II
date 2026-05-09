# Architecture

## Direction

`cPyNodus_II` rebuilds Nodus behavior from a narrower, easier-to-verify core.

The architecture is split into three layers:

- `core`: transport, startup planning, settings, scheduling primitives
- `features`: sensor, switch, config, calibration, onboarding behavior
- entrypoints: `boot.py`, `code.py`, and `cpynodus_ii.app`

## Constraints

- CircuitPython 9.2.8 on Pico 2 W
- MQTT transport must remain small and measurable
- Feature code should not own socket lifecycle
- Hot paths should minimize allocations and long-lived task retention

## Rebuild strategy

1. Characterize existing `cPyNodus` behavior.
2. Write tests that pin that behavior.
3. Implement the smallest slice needed to satisfy those tests.
4. Soak test on hardware before adding the next slice.

## AP vs normal mode

- AP mode is used when credentials are missing/invalid.
- AP mode hosts a minimal onboarding UI at `192.168.4.1`.
- Normal mode connects to Wi-Fi, configures mDNS, and serves routes.

## Profiles

- `sensorius` is the networked profile used by Sensorius onboarding and management.
  - MQTT is enabled.
  - NTP sync is started.
  - Broker settings come from `[MQTT]`.
  - Runtime metadata and sensor data are published over MQTT.
  - Webserver startup is intentionally skipped in this profile.
- `weewx` is a networked MQTT profile.
  - MQTT is enabled.
  - NTP sync is started.
  - Broker settings come from `[MQTT]`.
  - Webserver startup is intentionally skipped in this profile.
- `homeassistant` is a networked MQTT profile.
  - MQTT is enabled.
  - NTP sync is started.
  - Broker settings come from `[MQTT]`.
  - Home Assistant behavior is further configured in `[HomeAssistant]`.
  - Webserver startup is intentionally skipped in this profile.
  - Devices are expected to be provisioned through AP/nodusweb mode before switching into this profile.
- `nodusweb` is the default local-only profile.
  - Webserver startup is started in this profile.
  - NTP sync is started after normal network bring-up.
  - MQTT startup is skipped; the device will run without an MQTT broker.

## Developer Note: MQTT-First Onboarding and Runtime Metadata (2026-02-24)

- Add Device provisioning uses `POST /itaot-init` only for AP bootstrap (Wi-Fi + MQTT seed + reboot).
- After reboot, onboarding and config apply are MQTT-authoritative:
  - `nodus/<device_id>/onboard/hello`
  - `nodus/<device_id>/config/set`
  - `nodus/<device_id>/config/ack`
  - `nodus/<device_id>/config/result`
- Nodus TOML files are the source of truth for accepted device configuration.
- Retained `nodus/<device_id>/meta` is the compact authoritative startup snapshot and is published on successful MQTT startup/reconnect.
- Retained `nodus/<device_id>/meta/switch` carries detailed switch channel control topics and is published after MQTT startup subscriptions are healthy when switch channels are present.
- After startup, accepted runtime config writes are mirrored to Sensorius through non-retained `nodus/<device_id>/meta/patch` only; config writes do not trigger another full retained `meta` publish.
- Runtime liveness and device materialization should come from MQTT heartbeat/availability/data topics.
- `GET /itaot-meta` remains as optional fallback metadata for user-initiated enrichment, not background polling.
- `nodusweb` remains the only normal-mode profile that starts the built-in web server.
