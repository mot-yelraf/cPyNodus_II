# Onboarding (AP Mode)

This page is now a short overview. The canonical forward-only onboarding and
runtime contract with Sensorius lives in
[docs/sensorius_contract.md](./sensorius_contract.md).

## AP Mode Conditions

AP mode is used when:

- SSID or password is missing
- SSID equals `Nodus_Setup`
- Wi-Fi connection fails

## Canonical Add Device Flow

1. Nodus exposes `GET /itaot-meta` and `POST /itaot-init`.
2. Sensorius reads `/itaot-meta` so the device provides its own identity.
3. Sensorius sends a minimal bootstrap payload to `/itaot-init`.
4. Nodus validates and applies bootstrap, then reboots.
5. After Wi-Fi + MQTT connect, Nodus publishes `nodus/<device_id>/onboard/hello`.
6. Sensorius responds with one full onboarding `nodus/<device_id>/config/set`
   envelope.
7. Nodus publishes `config/ack`, then `config/result`.
8. Nodus publishes retained `nodus/<device_id>/meta`.
9. After accepted runtime changes, Nodus publishes only
   `nodus/<device_id>/meta/patch`.

## Manual Fallback

1. Connect to `Nodus_Setup`.
2. Browse to `http://192.168.4.1:8000/setup`.
3. Save settings to reboot into normal mode.

## Notes

- Bootstrap route exposure is tied to AP mode.
- Onboarding protocol state should stay outside normal TOML config schema.
- Sensorius should treat retained startup `meta` as the authoritative snapshot.
- Ordinary runtime config writes should not trigger a full retained `meta`
  republish.
