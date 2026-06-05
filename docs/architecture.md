# Architecture

## Direction

`cPyNodus_II` implements Nodus behavior from a narrower, easier-to-verify core
than the earlier flat-module firmware.

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

- AP mode is used when credentials are missing/invalid or station join fails.
- AP mode hosts minimal routes at `192.168.4.1:8000`, including `/setup`,
  `/config`, `/itaot-meta`, and `/itaot-init`.
- Normal mode connects to Wi-Fi and configures mDNS/socket artifacts.
- Normal-mode web routes are started only for `ACTIVE_PROFILE = "nodusweb"`;
  MQTT profiles run headless after provisioning.

## Network Startup Path

The current network startup path is intentional and stability-sensitive:

1. Load settings and perform factory/profile reset work before network objects
   are created.
2. Consume any soft-reload cleanup marker and cycle station/AP state before
   rebuilding Wi-Fi artifacts on warm starts.
3. Check private OTA state before normal profile startup so OTA mode can join
   Wi-Fi with the smallest possible runtime surface.
4. Build the `NetworkStack` once from the resolved runtime config. Station
   startup uses a preconnect scan and targeted connect hints when possible.
5. Fall back to AP recovery before starting feature services when station join
   fails.
6. Resolve and persist `MQTT.BROKER_IP` from `MQTT.BROKER` when MQTT is enabled
   and the filesystem is writable.
7. Create the MQTT adapter from the network stack's current socket pool and SSL
   context, then run MQTT preflight/probe logic before MiniMQTT owns the socket.
8. Start sensor, switch, web, NTP, and MQTT loops from that single startup
   plan.

This ordering matters on Pico2 W. Socket pools, SSL contexts, mDNS/DNS, and
MiniMQTT state are tied to the current radio mode. Starting features before the
network phase is known can leave stale sockets, partial AP/station state, or
feature-owned network objects that recovery cannot replace cleanly. Keeping
network ownership in `core.network` lets recovery rebuild socket artifacts,
reset station mode, or enter AP mode without sensor, switch, web, or
calibration code trying to manage the radio directly.

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

## Current Feature Slices

- Factory bootstrap creates `settings.toml` plus only the detected live sensor
  and switch TOML files from root `*.def` templates.
- I2C sensors support `aht`, `apvpd_aht`, `apvpd`, `aqi`, `avpd`, `co2`, and
  `lux`.
- Soil sensors use `sensor_soil.toml` and support one or two RS485 channels
  through `[Modbus.CH1]` and `[Modbus.CH2]`.
- Switch runtime supports up to two GPIO relay channels, gated by `switch.toml`
  plus populated and grounded per-channel `SWITCH_N_ENABLE_PIN` entries.
- MQTT runtime supports startup `meta`, split `meta/switch`, heartbeat,
  availability, sensor data, device `config/set`, switch `config/set`,
  calibration commands, log transfer, and OTA prepare.
- OTA uses MQTT only to request prepare mode; package bytes transfer through
  the temporary HTTP-only OTA runtime.
- Recovery logic handles AP idle timeout, Wi-Fi reassociation windows, MQTT
  rebuild/reconnect windows, repeated MQTT connect failures, low-memory MQTT
  failures, and repeated sensor-not-found errors.

## Recovery and Reboot Semantics

Recovery is deliberately layered so transient faults do not immediately reboot
the device:

- Wi-Fi recovery first retries reassociation. After repeated scan-miss or
  unknown-station failures, recovery resets station mode, rebuilds socket
  artifacts, and retries with a short reconnect window. If a previously good
  Wi-Fi link cannot recover within the bounded window, the runtime escalates.
- MQTT recovery first closes or rebuilds the MQTT client and refreshes socket
  artifacts while the station link is still healthy. Plain broker outages are
  paced more slowly after the initial recovery window so Nodus does not spin on
  an offline broker.
- MQTT preflight distinguishes several cases before MiniMQTT connect:
  TCP failure, raw MQTT CONNACK timeout, and socket-progress/stuck-socket
  patterns. Those cases can trigger socket refresh, station reset, adapter
  rebuild, a targeted `wifi.radio.enabled` power cycle, or escalation depending
  on elapsed time and failure count.
- Sensor-not-found recovery tries bounded reinitialization before rebooting.

Soft reboot means `supervisor.reload()`. Before a soft recovery reload, Nodus
tries to stop the web runtime, close or disconnect MQTT, stop feature services,
tear down station networking, collect garbage, and set a warm-start cleanup
marker. On the next startup, the marker causes station/AP state to be cycled
before normal network initialization. This is the quick recovery path for
app-level socket or MiniMQTT state that can be cleared without power-cycling the
microcontroller.

Hard reset means `microcontroller.reset()`. It is used when the failure may
live below the Python app or when the runtime needs the radio/socket stack
fully reset. Current hard-reset recovery reasons are:

- AP idle timeout
- Wi-Fi recovery timeout
- Wi-Fi after-ready failure
- MQTT recovery timeout
- repeated MQTT connect failures
- MQTT client memory allocation failures
- repeated sensor-not-found errors

For repeated MQTT connect failures, Nodus also uses an NVM marker to avoid an
immediate hard-reset loop. The first persistent stuck-socket/CONNACK failure can
reset the MCU; if the same marker is still present too soon on the next boot,
the runtime defers another hard reset and keeps recovery bounded.

The soft reload path still matters even with hard resets in the policy. It is
faster, preserves the ability to log and shut down cleanly, and is appropriate
when recovery has enough confidence that a clean app reload plus warm-start
radio cleanup will clear the socket issue. Hard reset is reserved for the cases
where field behavior suggests the Pico2 W radio/socket state may outlive a
normal reload.

## Developer Note: MQTT-First Onboarding and Runtime Metadata (2026-02-24)

- Add Device provisioning uses `POST /itaot-init` only for AP bootstrap (Wi-Fi + MQTT seed + reboot).
- After reboot, onboarding and config apply are MQTT-authoritative:
  - `nodus/<device_id>/onboard/hello`
  - `nodus/<device_id>/config/set`
  - `nodus/<device_id>/config/ack`
  - `nodus/<device_id>/config/result`
- Nodus TOML files are the source of truth for accepted device configuration.
- Retained `nodus/<device_id>/meta` is the compact authoritative startup snapshot and is published on successful MQTT startup/reconnect.
- Retained `nodus/<device_id>/meta/switch` carries detailed switch channel control topics and is published in the startup identity batch when switch channels are present.
- After startup, accepted runtime config writes are mirrored to Sensorius through non-retained `nodus/<device_id>/meta/patch` only; config writes do not trigger another full retained `meta` publish.
- Runtime liveness and device materialization should come from MQTT heartbeat/availability/data topics.
- `GET /itaot-meta` remains as optional fallback metadata for user-initiated enrichment, not background polling.
- `nodusweb` remains the only normal-mode profile that starts the built-in web server.
