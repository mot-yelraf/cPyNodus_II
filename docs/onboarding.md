# Onboarding (AP Mode)

AP mode is used when:

- SSID or password is missing
- SSID equals `Nodus_Setup`
- Wi-Fi connection fails

## Behavior

Two setup methods are supported for Nodus onboarding:

1. Sensorius Add Device (primary flow).
2. Local `/setup` page (manual fallback).

Primary flow (Nodus implementation):
1. Nodus creates AP `Nodus_Setup` (default channel `6`).
2. Sensorius calls `POST http://192.168.4.1:8000/itaot-init` with minimal Wi-Fi + MQTT bootstrap.
3. Nodus validates and applies bootstrap, then reboots.
4. After Wi-Fi + MQTT connect, Nodus publishes:
   - `nodus/<device_id>/onboard/hello`
   - consumes `nodus/<device_id>/config/set`
   - publishes `nodus/<device_id>/config/ack`
   - publishes `nodus/<device_id>/config/result`
5. Runtime metadata is published retained on `nodus/<device_id>/meta`.
6. Sensorius should treat that retained full `meta` payload as the authoritative startup snapshot because Nodus TOML files are the source of truth.
7. After onboarding/startup, accepted runtime config changes are mirrored through `nodus/<device_id>/meta/patch`; ordinary config writes do not trigger another full retained `meta` publish.

Manual fallback:
1. Connect to `Nodus_Setup` (password `password`).
2. Browse to `http://192.168.4.1:8000/setup`.
3. Save settings to reboot into normal mode.

Implementation note:
- In `cPyNodus_II`, the bootstrap route pair (`/itaot-init` and `/itaot-meta`) is the intended first web-services slice for the rebuild.

## Tips

- Keep the onboarding UI simple to avoid memory pressure.
- If settings are invalid, AP mode will reappear.
