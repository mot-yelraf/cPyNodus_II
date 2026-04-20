# Sensorius Migration: MQTT-First Nodus Health and Onboarding

## Purpose
Define how `saiSensorius` should monitor Nodus health and finish onboarding without relying on legacy HTTP status/identity polling.

## Scope
This applies to post-onboarded devices (already on Wi-Fi and connected to MQTT), and to the handoff from AP bootstrap to MQTT onboarding.

## Summary
1. MQTT is authoritative for Nodus liveness and onboarding progress.
2. `POST /itaot-init` remains required for AP bootstrap only.
3. `GET /itaot-meta` remains optional for user-initiated metadata enrichment.
4. Background polling of legacy HTTP status/identity endpoints is removed.

## Authoritative MQTT Model
Sensorius should subscribe to:
1. `nodus/+/status/heartbeat`
2. `nodus/+/availability`
3. `nodus/+/data`
4. Onboarding/config topics while onboarding is active:
   - `nodus/+/onboard/hello`
   - `nodus/+/config/ack`
   - `nodus/+/config/result`

## Onboarding Lifecycle
1. User selects Add Device.
2. Sensorius calls `POST /itaot-init` on Nodus AP.
3. Nodus reboots and joins target Wi-Fi + broker.
4. Nodus publishes `nodus/<device_id>/onboard/hello`.
5. Sensorius publishes `nodus/<device_id>/config/set`.
6. Nodus publishes `nodus/<device_id>/config/ack` and `nodus/<device_id>/config/result`.
7. Sensorius marks onboarding complete and device online.

## Runtime Health Policy
1. Use heartbeat as canonical liveness signal.
2. Use availability/data as supplemental recovery signals.
3. Treat retained stale heartbeats as `unknown` until fresh activity arrives.

## `/itaot-meta` Guidance
1. Use only on-demand (for example, opening a device details panel).
2. Do not block onboarding success on this endpoint.
3. Do not schedule periodic fetches.

## Acceptance Criteria
1. Sensorius uses MQTT topics for liveness and onboarding progression.
2. Add Device succeeds using `POST /itaot-init` plus MQTT onboarding topics.
3. Periodic HTTP status/identity polling is absent from steady-state monitoring.
4. `/itaot-meta` remains optional and user-driven.
