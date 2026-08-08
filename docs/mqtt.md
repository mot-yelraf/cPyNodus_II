# MQTT

This file is now a short overview. The canonical forward-only contract between
`cPyNodus_II` and Sensorius lives in
[docs/sensorius_contract.md](./sensorius_contract.md).

If this page and the contract page ever disagree, `docs/sensorius_contract.md`
wins.

## Current Contract Summary

- AP bootstrap uses `/itaot-meta` and `/itaot-init`.
- Runtime device config uses `nodus/<device_id>/config/set`.
- Runtime device restart uses `nodus/<device_id>/config/set` with
  `restart = true`.
- Runtime switch config uses `nodus/<channel_id>/config/set`.
- Calibration uses `nodus/<device_id>/calibration/set`.
- Log retrieval uses `nodus/<device_id>/logs/get` and returns chunked MQTT
  payloads on `nodus/<device_id>/logs/chunk`.
- OTA prepare uses signed `nodus-fwupdate/v2` state on
  `nodus/<device_id>/fwupdate`; signed package files move over HTTP after Nodus
  reboots into temporary OTA mode.
- Nodus publishes retained compact `nodus/<device_id>/meta` on
  connect/reconnect. The `sensor.hardware` field reports the concrete sensor
  family when known while logical device IDs remain unchanged.
- Nodus publishes retained `nodus/<device_id>/meta/switch` in the startup
  identity publish batch when switch channels are present.
- Nodus publishes non-retained `nodus/<device_id>/meta/patch` after accepted
  runtime changes.
- Nodus publishes retained heartbeat and availability online/offline payloads.
- MQTT configures a retained offline Last Will on the device heartbeat topic.
  Abrupt power, radio, or socket loss can therefore become broker-visible
  without waiting for the next device heartbeat. Preflight probes omit the
  Will and cannot mark the device offline when their temporary socket closes.
- In `homeassistant`, Nodus also publishes retained Home Assistant discovery
  messages under `[HomeAssistant].DISCOVERY_PREFIX`.
- In `weewx`, Nodus publishes the normal `nodus-sensor-data/v1` payload. The
  host-side MQTTSubscribe mapping, schema, report skin, retained identity, and
  optional switch automation are documented in [WeeWX](./weewx.md); there is
  no separate WeeWX firmware payload.
- Local `nodusweb` automations create no MQTT traffic. Unlike cPyNodus_III,
  cPyNodus_II does not subscribe to controller automation-status or
  automation-availability topics because all MQTT profiles remain headless.
- `/set` commands should normally be published non-retained. When a `/set`
  command is intentionally published retained by Sensorius, Sensorius owns
  clearing it with an empty retained publish to the same topic after successful
  handling.
- Sensorius paces ordinary runtime config writes one key at a time per
  physical Nodus host and waits for `ack` plus successful `result`.
- Nodus enforces that pacing: multi-key `config/set` and multi-offset
  `calibration/set` mutations return `single_update_required`.
- Device config, calibration, and channel switch mutations use shallow
  dispatch and streaming scalar TOML persistence; they do not enter the
  general command handler or full-document TOML serializer.

## Current Topic Families

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<device_id>/meta/switch`
- `nodus/<device_id>/meta/patch`
- `nodus/<device_id>/onboard/hello`
- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`
- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/logs/get`
- `nodus/<device_id>/logs/ack`
- `nodus/<device_id>/logs/chunk`
- `nodus/<device_id>/logs/result`
- `nodus/<device_id>/fwupdate`
- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`
- `nodus/<sensor_id>/data`
- `nodus/<sensor_id>/availability`
- `nodus/<channel_id>/event`
- `nodus/<channel_id>/state`
- `nodus/<channel_id>/availability`
- `nodus/<channel_id>/config/set`
- `nodus/<channel_id>/config/ack`
- `nodus/<channel_id>/config/result`
- `homeassistant/sensor/<device>/<object>/config` when
  `ACTIVE_PROFILE=homeassistant`
- `homeassistant/switch/<device>/<object>/config` when
  `ACTIVE_PROFILE=homeassistant`

## Deprecated Doc Shapes

The following older doc shapes are deprecated and should not be treated as the
current contract:

- `nodus/<channel_id>/set`
- switch-control docs centered on plain `ON` and `OFF`
- docs that imply ordinary runtime config writes trigger a full retained
  `meta` republish

## Runtime Command Ownership

- `/set` topics are command topics, not state topics. Prefer non-retained
  publishes for commands. If Sensorius publishes any `/set` command retained,
  Sensorius must clear that retained command by publishing an empty retained
  payload to that exact topic after successful `result`. Nodus ignores empty
  `/set` payloads defensively.
- Startup retained `meta` publishing belongs to startup and reconnect handling.
  It is compact and excludes `switch.channels[*]`; detailed switch control
  topics live in retained `meta/switch`. Retained startup identity publishes
  drain before runtime command subscriptions. New compact `meta` payloads
  expose top-level `mcu`, `switch.meta_topic`, and current runtime
  `network.ipv4addr`; older payloads may still embed `switch.channels`.
- A connection is operational only after its startup publish and subscription
  queues drain. Slow startup publishes and errno 119, 116, and related errno 12
  failures retain one recovery epoch across reconnects, reuse the pending
  startup queues, and escalate through MQTT rebuild, station/radio reset, two
  NVM-counted warm reload attempts, then hard-reset recovery. Only the
  operational checkpoint or true power-on clears the warm-attempt count.
  PUBACK timeouts and errno 12 from TCP preflight or raw MQTT CONNECT are
  classified immediately instead of retrying with a zero failure count.
- During the bounded SUBACK wait, Nodus accepts and delivers up to eight
  interleaved broker packets. This includes retained PUBLISH messages that can
  arrive immediately after a subscription request.
- Retained startup publishes are separated by 350 ms. For temporary hardware
  diagnosis, retained device `meta` and `meta/switch` publish at QoS 1 so PUBACK
  distinguishes broker receipt from a stalled send or lost acknowledgement.
  Slow raw-send chunk diagnostics are capped at four lines per packet and are
  emitted only when an individual chunk takes at least 250 ms.
- Any publish taking at least five seconds is classified as stalled and enters
  MQTT recovery, even if the local socket eventually reports all bytes sent.
  This prevents approximately ten-second Pico 2 W socket stalls from being
  counted as successful while broker-visible traffic is absent.
- Current Nodus IPv4 is runtime state only. It is published in retained `meta`
  as `network.ipv4addr` when available and is not persisted in
  `settings.toml`.
- Broker host resolution updates runtime targets before MQTT connects:
  `MQTT.BROKER` remains the canonical broker hostname, while
  `MQTT.BROKER_IP` is the current resolved IP target. Hostname refresh does not
  open extra verification sockets before the normal MQTT preflight/connect path.
  On RWFS, after MQTT connects successfully, Nodus makes a best-effort scalar
  TOML update for `MQTT.BROKER_IP`.
- On Pico 2 W CircuitPython, hostname lookup can return only one address from
  the underlying resolver. A broker hostname such as `samhain.local` should not
  be expected to expose both broker interfaces automatically. Current firmware
  does not keep or try a configured alternate broker target; stale
  `BROKER_IP_ALT` keys are ignored.
- Log-transfer topics are deterministic from the device id and are not embedded
  in retained startup `meta`; use the topic family listed above when
  `capabilities.log_transfer` is true.
- Device config uses `config/set`, `config/ack`, `config/result`, and
  `meta/patch`. Nodus does not clear device `config/set`; Sensorius owns any
  retained command cleanup.
- Standalone device restart uses device `config/set` with a `message_id`,
  empty `payload`, `restart = true`, and optional `restart_mode` of `"soft"` or
  `"hard"`. Nodus publishes `config/ack` and successful `config/result` before
  rebooting. In MQTT profiles, app-requested soft restarts are promoted to hard
  reset by firmware policy.
- Accepted device `Time.*` config writes request a fresh NTP sync after command
  responses and queued MQTT publishes drain.
- Accepted device `Time.*` config writes are applied live before best-effort
  TOML persistence, so persistence stack or memory failures are reported in
  serial logs as volatile without turning the MQTT command result into failure.
- Accepted device `Display.METRIC_*` and `Display.Style.METRIC_*` config writes
  are applied live before best-effort sensor TOML persistence, so persistence
  stack or memory failures are reported in serial logs as volatile without
  turning the MQTT command result into failure.
- Accepted device `Sensor.LOCATION` and `Switch.SWITCH_LOCATION` config writes
  are applied live before best-effort TOML persistence, so persistence stack or
  memory failures are reported in serial logs as volatile without turning the
  MQTT command result into failure.
- Switch config uses channel-scoped `config/set`, `config/ack`,
  `config/result`, retained `state`, and `meta/patch`. Nodus does not clear
  switch `config/set`; Sensorius owns any retained command cleanup.
- Switch `event` payloads are JSON. Retained switch `state` is currently
  startup-refreshed as a JSON state snapshot and command-refreshed as raw
  `ON`/`OFF`; consumers should tolerate both and use `event`/`config/result`
  for correlated command handling.
- Calibration uses `calibration/set`, `calibration/ack`,
  `calibration/result`, and `meta/patch`. Nodus does not clear
  `calibration/set`; Sensorius owns any retained command cleanup.
- Log retrieval uses `logs/get`, `logs/ack`, `logs/chunk`, and `logs/result`.
  Nodus accepts only known bounded runtime logs (`_reboot.log` and
  `_recovery.log`), publishes chunks non-retained, and includes a per-chunk
  checksum plus final file checksum. Sensorius or a host tool owns request
  timeout handling and retained command cleanup if a retained request is used.

## MQTT Log Retrieval

Request payload on `nodus/<device_id>/logs/get`:

```json
{"schema":"nodus-log-transfer/v1","message_id":"log-1","filename":"_reboot.log","chunk_size":512}
```

Nodus publishes:

- `logs/ack`: request acceptance, file size, and selected chunk size.
- `logs/chunk`: base64 data, offset, next offset, sequence, size, and FNV-1a
  checksum for that raw chunk.
- `logs/result`: completion state, total chunks, size, and final FNV-1a file
  checksum.

Host-side retrieval tool:

```sh
cp scripts/nodus_getlogs.toml.def scripts/nodus_getlogs.toml
python scripts/nodus_getlogs.py --device-id co2-v5p04u --all
python scripts/nodus_log_analyze.py --device-id co2-v5p04u
```

## Notes

- Keep publish intervals conservative to reduce power usage.
- If MQTT is disabled, the device still runs locally.
- Calibration details remain documented in `docs/calibration_mqtt_contract.md`.

## Troubleshooting

### MQTT Publish Stall With False Local Success

Field testing on Pico 2 W devices has shown a failure mode where:

- Nodus logs local MQTT publish success (`ok=True`, normal `Published data ...` lines).
- Broker-observed traffic stops after startup or only retained startup topics arrive.
- Serial logs may also show repeated `~10s` switch-state or sensor publish timings.

When this specific failure mode appears, a normal CircuitPython reflash by itself may not fix it.

Observed remediation:

1. Save the device TOML files.
2. Flash `flash_nuke.uf2`.
3. Flash a fresh CircuitPython `9.2.8` UF2.
4. Deploy a clean Nodus build.
5. Restore the TOML files.

In recent validation, two separate Nodus devices that exhibited this MQTT publish-stall / broker-mismatch behavior were restored to normal operation only after the full `flash_nuke.uf2` + fresh CircuitPython reflash sequence. A plain CircuitPython reflash alone did not clear the issue.
