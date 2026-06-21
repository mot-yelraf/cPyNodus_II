# cPyNodus Handoff: Onboarding V2 Without `settings.toml` Schema Changes

## Purpose
Define the cPyNodus-side contract for Onboarding V2 while preserving the current Nodus `settings.toml` schema.

This design keeps onboarding/session metadata out of `settings.toml` and treats it as protocol/runtime state.

## Non-Goal
Do **not** add new persistent keys to Nodus `settings.toml` for onboarding state, token state, message IDs, retries, or session tracking.

## High-Level Flow
1. Sensorius joins Nodus AP and sends `POST /itaot-init` with minimum bootstrap payload.
2. Nodus validates payload shape, applies only existing network/MQTT fields, stores onboarding runtime state outside `settings.toml`, responds `200` ack, then reboots.
3. After Wi-Fi + MQTT connect, Nodus publishes onboarding hello.
4. Sensorius validates token/session and publishes full config.
5. Nodus acks config receipt, applies config, publishes apply result.
6. Sensorius marks onboarding complete and invalidates token.

## HTTP Contract
### Endpoint
- `POST /itaot-init`

### Request (minimum bootstrap)
```json
{
  "onboard_token": "token-123",
  "ssid": "MyWiFi",
  "password": "my-password",
  "hostname": "aqi-x943fm",
  "mqtt": {
    "broker_host": "sensorius-broker.local",
    "broker_port": 1883
  },
  "time": {
    "TZ": "America/Denver",
    "TZ_OFFSET": -21600,
    "TZ_NAME": "MDT",
    "NTP_SERVER": "us.pool.ntp.org",
    "NTP_SERVER_IP": "132.163.96.6"
  }
}
```

### Required behavior on Nodus
1. Validate required fields and types.
2. Apply only existing settings fields already supported by current settings
   model, including supported `time` keys.
3. Do not persist onboarding protocol metadata into `settings.toml`.
4. Treat the flat top-level JSON shape above as canonical for Sensorius V2 clients.
5. When `mqtt.active_profile` is omitted but `mqtt.broker_host` is present, infer `ACTIVE_PROFILE = "sensorius"` so MQTT onboarding remains enabled.
6. Return JSON success ack before reboot when possible:
```json
{
  "accepted": true,
  "rebooting": true
}
```

## MQTT Contract (Canonical)
1. Hello: `nodus/<device_id>/onboard/hello`
2. Config set: `nodus/<device_id>/config/set`
3. Config ack: `nodus/<device_id>/config/ack`
4. Config result: `nodus/<device_id>/config/result`

`device_id` is authoritative identity.

## Runtime State Placement on Nodus
Because `settings.toml` is unchanged, onboarding runtime state is stored outside
the normal TOML schema.

Current `cPyNodus_II` behavior:

- writes `onboarding_state.json` after accepted `/itaot-init`
- stores `schema`, `onboard_token`, `hostname`, `base_topic`,
  `active_profile`, and `created_at`
- deletes the file after a successful MQTT config apply

Potential future state fields, if tighter on-device replay handling is added:

- `token_expires_at`
- `session_started_at`
- `last_message_id`
- `pending_config_version`

Do not merge onboarding protocol state into persistent config schema.

## Token Rules
1. Nodus stores the bootstrap token in `onboarding_state.json`, outside
   `settings.toml`.
2. When the state file is present, `config/set` must include the same token.
3. Nodus clears the state file after `config/result.applied == true`.
4. Nodus does not currently enforce an on-device TTL; Sensorius should enforce
   a short onboarding-session TTL and avoid replaying consumed tokens.

## `onboard/hello` Payload
```json
{
  "onboard_token": "token-123",
  "device_id": "aqi-x943fm",
  "hostname": "aqi-x943fm",
  "serial": "x943fm",
  "type": "nodus",
  "mcu": "pico2w",
  "version": "v0.26.xxx.x",
  "capabilities": {
    "sensor": true,
    "switch": true
  },
  "sensor": {
    "present": true,
    "device": "aqi",
    "hardware": "BME680"
  }
}
```

`type` is the device class and should be `nodus`. `mcu` is the board target
identifier for the running firmware. Verified `mcu` values are `pico2w` and
`xesp32s3`. The `sensor` block is a compact onboarding hint. It does not repeat
the sensor identity because top-level `device_id` already identifies the
onboarding device.

## `config/set` Handling Rules
1. Parse and validate payload schema.
2. Use `message_id` for idempotency:
   - duplicate `message_id` should be safe.
3. Publish `config/ack` immediately when accepted.
4. Publish `config/result` after apply attempt.
5. Apply `payload.settings.Time.TZ`, `payload.settings.Time.TZ_OFFSET`, and
   `payload.settings.Time.TZ_NAME` when present.
6. Accepted non-duplicate `Time.*` writes request a fresh NTP sync after command
   responses and queued MQTT publishes drain.

## Failure Behavior
1. Token mismatch: reject onboarding/config flow and publish negative `config/result` when applicable.
2. Schema invalid: reject with explicit error.
3. Apply failure: publish `config/result` with `applied=false` and `error` string.

## Logging and Security
1. Never log plaintext Wi-Fi password, MQTT password, or onboarding token.
2. If token is persisted in ephemeral file, prefer hash-at-rest.
3. Keep runtime onboarding artifacts short-lived.

## Compatibility Notes
1. This flow assumes all deployed Nodus devices are updated to V2 onboarding/ingesting behavior.
2. Legacy onboarding path does not need to be used for V2-enabled cohorts.

## Acceptance Criteria (Nodus side)
1. V2 onboarding succeeds without adding keys to `settings.toml`.
2. Device publishes hello on canonical V2 topic after reboot.
3. `config/ack` and `config/result` follow `message_id` correlation.
4. Duplicate `config/set` with same `message_id` is idempotent.
5. Token mismatch is rejected, and successful config apply clears the token
   state outside `settings.toml`.
