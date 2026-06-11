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

1. In AP mode, Nodus exposes `GET /itaot-meta` and `POST /itaot-init`.
2. Sensorius reads `/itaot-meta` so the device provides its own identity.
3. Sensorius sends a minimal bootstrap payload to `/itaot-init`, including
   available hub `[Time]` values in top-level `time`.
4. Nodus validates and applies bootstrap to existing TOML schema fields,
   writes short-lived `onboarding_state.json` outside `settings.toml`, then
   reboots.
5. After Wi-Fi + MQTT connect, Nodus publishes `nodus/<device_id>/onboard/hello`.
6. Sensorius responds with one full onboarding `nodus/<device_id>/config/set`
   envelope. Full onboarding Time values use `payload.settings.Time`.
7. Nodus publishes `config/ack`, then `config/result`.
8. Nodus publishes retained compact `nodus/<device_id>/meta`.
9. If switch channels are present, Nodus publishes retained
   `nodus/<device_id>/meta/switch` in the startup identity publish batch.
10. After accepted runtime changes, Nodus publishes only
   `nodus/<device_id>/meta/patch`.

## Manual Fallback

1. Connect to `Nodus_Setup`.
2. Browse to `http://192.168.4.1:8000/setup` for the lightweight local UI.
3. Use `POST /config` for supported JSON configuration writes that are not
   rendered by the page, such as `Network.PASSWORD`.
4. Use `POST /restart` to reboot into normal mode after restart-required
   settings are saved.

## Notes

- Bootstrap route exposure is tied to AP mode.
- In AP mode, serial logs emit `web request path=/itaot-init ...` and
  `web request path=/itaot-meta ...` markers for bootstrap request receipt,
  parse result, normalized field presence, update counts, persistence begin/end,
  onboarding-state persistence, response return, reboot scheduling, reboot
  execution, and unexpected apply exceptions. These markers do not include
  Wi-Fi passwords, MQTT passwords, or onboarding tokens.
- Onboarding protocol state should stay outside normal TOML config schema.
- Current Nodus token enforcement checks the persisted token when present and
  clears `onboarding_state.json` after a successful config apply. On-device
  token TTL enforcement is not currently implemented; Sensorius should still
  enforce short onboarding-session TTLs.
- Sensorius should treat retained startup `meta` as the authoritative compact
  device/sensor snapshot and retained `meta/switch` as the detailed switch
  control-topic map.
- Ordinary runtime config writes should not trigger a full retained `meta`
  republish.
