# Behavior Inventory Seed

This file is the starting point for preserving `cPyNodus` behavior without
copying the old implementation.

## Startup and profiles

- Map `standalone` to `nodusweb`
- `nodusweb` disables MQTT startup
- MQTT-backed profiles disable the normal web runtime during steady state
- AP/bootstrap behavior remains allowed when credentials are missing or invalid

## Device modes

- sensor-only
- switch-only
- sensor + switch

## MQTT behavior to characterize

- broker target selection and fallback rules
- connect/reconnect timing and backoff
- sensor data publish topics and payloads
- switch `config/set` consumption and publish sequence
- retained command clearing
- runtime metadata full snapshot and `meta/patch`
- calibration command flows

## Persistence behavior to characterize

- `settings.toml`
- sensor-specific TOML
- `switch.toml`
- calibration writes and reload behavior

## Failure behavior to characterize

- broker unavailable
- DNS resolution unavailable
- Wi-Fi connect failures
- publish timeout / degraded socket behavior
- low-memory protective behavior
