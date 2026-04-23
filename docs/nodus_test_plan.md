# Nodus Validation Test Plan

## Purpose

Use this plan to verify end-to-end Nodus behavior across seven physical devices
running firmware builds `v0.26.113.10` through `v0.26.113.16`, with emphasis on
the remaining Sensorius `Add Device` onboarding path.

This is a manual system-validation plan. It complements host-side `pytest`
coverage and is intended to capture device, broker, and Sensorius integration
behavior that cannot be proven from local tests alone.

## Scope

Validate:

- AP bootstrap and onboarding prerequisites
- Sensorius `Add Device` flow
- MQTT onboarding hello, config ack/result, and retained `meta`
- steady-state sensor publishing
- switch control and switch state persistence
- runtime `config/set`
- runtime `calibration/set`
- reconnect and restart behavior
- mixed-version field behavior across `113.10` to `113.16`

Out of scope:

- Home Assistant discovery validation
- destructive flash / reimage recovery unless already needed
- broad RF / long-duration soak testing beyond the optional section below

## References

- [docs/onboarding.md](./onboarding.md)
- [docs/sensorius_contract.md](./sensorius_contract.md)
- [docs/onboarding_v2_sensorius_requirements.md](./onboarding_v2_sensorius_requirements.md)
- [docs/mqtt.md](./mqtt.md)
- [docs/calibration_mqtt_contract.md](./calibration_mqtt_contract.md)

## Test Inventory

Use one row per physical Nodus.

| Device | Firmware | Mode | Sensor Present | Switch Present | Serial Log | Result |
| --- | --- | --- | --- | --- | --- | --- |
| Nodus-1 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-2 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-3 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-4 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-5 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-6 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |
| Nodus-7 | `v0.26.113.xx` | sensor / switch / combo | yes/no | yes/no | path | pass/fail |

## Evidence To Capture

For each test case, record:

- device id
- firmware version
- timestamp
- Sensorius action taken
- MQTT topic(s) observed
- serial log excerpt
- pass/fail
- notes on version-specific behavior

Recommended capture set:

- Sensorius UI screenshot for `Add Device`
- broker trace for `onboard/hello`, `config/ack`, `config/result`, `meta`
- serial log around reboot, Wi-Fi join, MQTT connect, and first publish
- retained `meta` payload after onboarding completes

## Environment Prerequisites

Before starting the matrix:

1. Confirm broker reachability from the target Wi-Fi network.
2. Confirm Sensorius is connected to the same broker it will use for onboarding.
3. Confirm each device has a known serial console path.
4. Confirm you can observe MQTT traffic with retained messages visible.
5. Confirm each device can still enter AP mode if credentials are cleared or invalid.
6. Confirm at least one device of each hardware role is available:
   sensor-only, switch-only, and sensor+switch if present in your fleet.

## Exit Criteria

The validation pass is complete when all of the following are true:

1. Every device completes one successful Sensorius `Add Device` onboarding run.
2. Every onboarded device publishes `onboard/hello`, `config/ack`,
   `config/result`, and retained `meta` in the expected order.
3. Sensor-capable devices publish at least three valid sensor payloads after
   onboarding.
4. Switch-capable devices accept at least three toggle actions from Sensorius
   and republish retained state correctly.
5. `config/set` and `calibration/set` each succeed at least once on every
   applicable device.
6. At least one reconnect / restart scenario passes on every applicable device.
7. Any version-specific deviations across `113.10` to `113.16` are documented.

## Test Cases

### TC-01: Inventory And Baseline

Goal: establish device identity and current behavior before onboarding.

Steps:

1. Power each device and attach serial logging.
2. Record reported hostname, device id, serial, role, and firmware version.
3. Note whether the device currently reaches normal runtime or AP mode.
4. If already onboarded, record current retained `meta` and active topics.

Pass criteria:

- All seven devices have an inventory row.
- Each device has a confirmed firmware version and role.

### TC-02: AP Bootstrap Sanity

Goal: reconfirm the AP prerequisites that feed Sensorius onboarding.

Steps:

1. Put the device into AP mode if needed.
2. Verify `GET /itaot-meta` succeeds.
3. Verify `POST /itaot-init` accepts the minimal bootstrap payload.
4. Confirm the device stores bootstrap state and reboots.

Pass criteria:

- `itaot-meta` is reachable in AP mode.
- `itaot-init` accepts a valid payload and triggers reboot.

Note:

- You already validated this with `apvpd`; rerun only if a device behaves
  differently during `Add Device`.

### TC-03: Sensorius Add Device Onboarding

Goal: validate the full production onboarding path that still remains open.

Run this on all seven devices.

Steps:

1. In Sensorius, start `Add Device`.
2. Point Sensorius at the Nodus AP and let it submit the canonical
   `POST /itaot-init` payload.
3. Confirm the device reboots, joins Wi-Fi, and connects MQTT.
4. Observe `nodus/<device_id>/onboard/hello`.
5. Confirm Sensorius publishes exactly one onboarding
   `nodus/<device_id>/config/set` envelope.
6. Observe `nodus/<device_id>/config/ack`.
7. Observe `nodus/<device_id>/config/result`.
8. Observe retained `nodus/<device_id>/meta`.
9. Confirm Sensorius marks the device onboarded and online.

Pass criteria:

- `onboard/hello` contains the expected `onboard_token`, `device_id`,
  `hostname`, `serial`, `type`, `version`, and `capabilities`.
- `config/ack.accepted == true`.
- `config/result.applied == true`.
- retained `meta` is present after onboarding success.
- Sensorius shows the device online without requiring a manual retry.

Failure clues to record:

- missing `onboard/hello`
- token mismatch or token reuse behavior
- `config/ack` present but missing `config/result`
- `config/result.error` non-empty
- retained `meta` delayed or absent
- Sensorius UI state diverges from broker-observed state

### TC-04: Retained Meta Validation

Goal: confirm the retained startup snapshot is usable after onboarding.

Steps:

1. Read retained `nodus/<device_id>/meta`.
2. Verify identity fields: `schema`, `device_id`, `hostname`, `serial`,
   `version`, `type`.
3. Verify `capabilities`.
4. Verify `status.heartbeat_topic`.
5. Verify sensor and switch sections match the physical device role.
6. For switch devices, verify per-channel topics are present.

Pass criteria:

- Retained `meta` matches the actual device role and topic layout.
- No stale data from a previous hostname or device id remains retained.

### TC-05: Sensor Publish Cycle

Goal: verify steady-state telemetry after onboarding.

Run on sensor-capable devices.

Steps:

1. Wait for the normal publish interval.
2. Capture at least three `nodus/<sensor_id>/data` messages.
3. Capture sensor availability state.
4. Compare topic names and payload fields against current contract.

Pass criteria:

- At least three payloads arrive without requiring reboot.
- Payload includes expected `schema`, `sensor_id`, `values`, and timestamp.
- Availability is online.

### TC-06: Switch Control From Sensorius

Goal: verify live switch control and result publishing.

Run on switch-capable devices.

Steps:

1. From Sensorius, toggle each switch channel ON.
2. Observe channel `config/ack`, `config/result`, and retained `state`.
3. Toggle each channel OFF and observe the same sequence.
4. Power-cycle the device if `LAST_STATE` persistence is expected for that
   channel and confirm the restored state matches expectation.

Pass criteria:

- Each requested toggle changes the physical output.
- `config/result.applied == true`.
- retained `state` matches the actual channel state after each command.
- Expected persistence behavior survives reboot.

### TC-07: Generic Runtime Config Write

Goal: verify ordinary device config writes after onboarding.

You already validated simple `config/set`; rerun this through Sensorius on at
least one device per firmware build if practical.

Steps:

1. Choose a safe runtime setting visible in `meta/patch` or behavior.
2. Publish one `nodus/<device_id>/config/set` update.
3. Observe `config/ack` and `config/result`.
4. Observe `nodus/<device_id>/meta/patch` if applicable.
5. Confirm the new runtime behavior or reported metadata matches.

Pass criteria:

- `config/ack.accepted == true`.
- `config/result.applied == true`.
- runtime state changes match the requested update.

### TC-08: Calibration Write

Goal: verify MQTT calibration flow on applicable sensor devices.

You already validated simple `calibration/set`; include this to make the field
run complete.

Steps:

1. Publish one valid calibration command to
   `nodus/<device_id>/calibration/set`.
2. Observe `calibration/ack`.
3. Observe `calibration/result`.
4. Confirm follow-on data or calibration status reflects the new values.

Pass criteria:

- `calibration/ack.accepted == true`.
- `calibration/result.applied == true`.
- The device continues publishing normally after calibration change.

### TC-09: Reconnect And Recovery

Goal: verify practical recovery after normal interruptions.

Steps:

1. Reboot the device after it has been onboarded.
2. Confirm Wi-Fi reconnect, MQTT reconnect, retained `meta`, and steady-state
   publish resume.
3. If practical, briefly interrupt broker availability or Wi-Fi and restore it.
4. Confirm the device returns to normal publishing without manual reflash.

Pass criteria:

- Device recovers from reboot cleanly.
- Retained `meta` is republished on reconnect.
- Sensor and switch topics resume without stale or duplicate onboarding flow.

### TC-10: Duplicate / Idempotency Spot Check

Goal: verify onboarding/runtime robustness under repeated messages.

Run on at least one representative device from the fleet.

Steps:

1. Replay the same `config/set` `message_id` once.
2. Observe whether `config/ack` and `config/result` behave idempotently.
3. Confirm the device does not double-apply the change.

Pass criteria:

- Duplicate handling is stable and does not corrupt runtime state.
- Any `duplicate` field observed is recorded for the firmware build tested.

### TC-11: Optional Soak

Goal: catch intermittent broker or publish-stall issues.

Steps:

1. Leave at least one sensor-capable and one switch-capable device running for
   2 to 12 hours.
2. Periodically confirm telemetry, retained state, and Sensorius online status.
3. Note any gap where serial logs report success but broker-observed traffic
   stops.

Pass criteria:

- No unexplained publish stall occurs during the soak window.

If a publish stall appears:

1. Capture serial logs and broker traces immediately.
2. Compare against the failure mode documented in [docs/mqtt.md](./mqtt.md).
3. Do not mark the device validated until the issue is explained or cleared.

## Recommended Run Order

Use this order per device:

1. `TC-01` inventory
2. `TC-03` Sensorius `Add Device`
3. `TC-04` retained `meta`
4. `TC-05` sensor publish cycle if applicable
5. `TC-06` switch control if applicable
6. `TC-07` runtime config
7. `TC-08` calibration if applicable
8. `TC-09` reconnect and recovery

Run `TC-10` and `TC-11` on a smaller representative subset if time is limited.

## Result Summary Template

```text
Date:
Operator:
Broker:
Sensorius build:

Device:
Firmware:
Role:

TC-01:
TC-03:
TC-04:
TC-05:
TC-06:
TC-07:
TC-08:
TC-09:
TC-10:
TC-11:

Overall result:
Notes:
```

## Minimum Sign-Off Recommendation

For a release decision, require:

- all seven devices passing `TC-03`
- all applicable devices passing `TC-05`, `TC-06`, `TC-07`, `TC-08`, `TC-09`
- no unresolved version-specific onboarding failure
- no unexplained retained-`meta` mismatch
- no publish-stall issue in the optional soak subset
