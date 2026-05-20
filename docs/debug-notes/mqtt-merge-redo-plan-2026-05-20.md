# MQTT Merge Redo Plan - 2026-05-20

## Goal

Redo the post-`v0.26.139.3` merge into `trunk` without losing broker-visible
MQTT discovery and identity publishing.

The protected behavior is:

- Sensor devices publish retained `/meta`.
- Switch-capable devices publish retained `/meta` and `/meta/switch`.
- `/data` continues at the configured sensor cadence.
- `/status/heartbeat` and `/availability` continue at the configured status
  cadence.
- Recovery may occur, but recovery must restore broker-visible identity
  messages, not only data publishing.

## Current Branch State

Primary working tree:

- Path: `/Users/twfarley/Projects/cPyNodus_II`
- Branch: `mqtt-merge-redo-from-139.3`
- Base commit: `9db8189`
- Base version: `v0.26.139.3`

Archived failed merge/debug work:

- Branch: `archive/post-merge-mqtt-debug-2026-05-20`
- Commit: `748c459`

Disposable reference checkout:

- Path: `/Users/twfarley/tmp/cPyNodus_II-139.3`
- Detached at `9db8189`
- Remove after the redo branch no longer needs it for comparison.

## Known Evidence

`v0.26.139.3` is pre-merge firmware. It can have MQTT recoveries, but the
broker-visible MQTT contract remains intact. In the overnight run, `139.3`
devices continued producing `/meta`, `/meta/switch` where applicable, data,
heartbeat, and availability after recovery.

`v0.26.139.4` is post-merge firmware. The most significant regression is that
`co2-frank` produced data, heartbeat, and availability, but the broker capture
did not show `/meta`. Serial logs showed `/meta` as published, so serial-side
`published=1` is not a sufficient pass condition.

## Merge Rules

1. Treat MQTT identity publishing as a protected contract.
2. Do not accept serial logs alone as proof of MQTT success.
3. Verify broker-visible retained `/meta` after startup and after recovery.
4. Verify broker-visible retained `/meta/switch` for switch-capable devices.
5. Merge in small milestones and stop immediately when the MQTT contract breaks.
6. Avoid importing warm-reboot or recovery rewrites blindly.
7. Keep generated `__pycache__` and build artifacts out of the branch unless
   they are deliberately part of a test fixture.

## Commit Risk Map

Commits from `mqtt-merge-redo-from-139.3` to `origin/trunk`:

- `f6eef83` - High risk. Cold boot MQTT workaround using warm reboot.
- `607465d` - Low risk. Docs-only architecture wording.
- `46ecd17` - Medium risk. Nodusweb UI work plus network/mDNS changes.
- `b9a3f1e` - Medium risk. mDNS only when web is enabled; reconnect behavior.
- `1f2fea9` - High risk. Commit message says MQTT is busted.
- `c201a6c` - Medium risk. MQTT contract cleanup and reboot log sizing.
- `8969d98` through `4cf87b8` - Medium/high risk. OTA implementation touches
  app startup, MQTT, command intake, payloads, and memory-sensitive paths.
- `6814c97` - Medium risk. Cleanup touches `app.py` and MQTT client.
- `7116106` - High risk. MQTT recovery process changes and recovery log.
- `bb8c133` - High risk. `/meta` split, startup sequence changes.
- `0873296` - High risk. MQTT sync priority and slow-operation disconnects.
- `3c2bc68` and `d42bd26` - Medium risk. MQTT log retrieval tooling.

## Milestones

### Milestone 0: Baseline

Status: complete.

Actions:

- Confirm branch is `mqtt-merge-redo-from-139.3`.
- Confirm version is `v0.26.139.3`.
- Confirm working tree is clean before migration work.
- Run host-side baseline checks.

Checks run:

- `pytest tests/test_mqtt_client_adapter.py`
- `pytest tests/test_publish_cycle.py`
- `pytest tests/test_app_startup.py`

Result:

- All passed.

### Milestone 1: Add MQTT Contract Validation

Before importing risky runtime changes, add or document a repeatable validation
step that confirms broker-visible messages.

Required broker checks:

- On boot, observe retained `/meta`.
- On recovery, observe retained `/meta` again.
- On switch-capable devices, observe retained `/meta/switch`.
- Observe `/data` at roughly 60 seconds, unless configuration says otherwise.
- Observe heartbeat and availability at roughly 120 seconds.

Recommended devices:

- Sensor-only: `aht-rvwi73` or `co2-frank`
- Switch-capable: `co2-ykdvea`

### Milestone 2: Low-Risk Non-MQTT Changes

Bring in changes that do not alter MQTT startup, recovery, socket handling, or
publish queue semantics.

Candidate:

- `607465d` if not already effectively present.

Validation:

- Host tests.
- No required hardware validation if no runtime files changed.

### Milestone 3: Web/Nodusweb Changes Without MQTT Recovery Changes

Consider selected parts of `46ecd17`, but inspect and avoid network/mDNS changes
that affect socket allocation or station reconnect behavior.

Validation:

- `pytest tests/test_network_stack.py`
- `pytest tests/test_web_handlers.py`
- `pytest tests/test_web_runtime.py`
- Hardware boot test on one sensor-only device.
- Broker-visible `/meta` check.

### Milestone 4: Network/mDNS Changes

Evaluate `b9a3f1e` separately. The intent may be useful, but it touches reconnect
behavior and socket artifact preservation.

Validation:

- Hardware boot and warm reload.
- Induced MQTT recovery.
- Broker-visible `/meta` after recovery.
- Confirm no extra mDNS allocation in `sensorius`.

### Milestone 5: MQTT Contract and Recovery Changes

Handle `c201a6c`, `7116106`, `bb8c133`, and `0873296` one at a time. These are
the most likely places to lose retained identity replay.

Validation after each commit or selected patch:

- Sensor-only broker contract.
- Switch-capable broker contract.
- Recovery after publish failure.
- Recovery after subscribe failure.
- Heap before and after recovery.

Do not proceed if `/meta` or `/meta/switch` disappears from broker captures.

### Milestone 6: OTA and Log Retrieval

Bring in OTA and MQTT log retrieval after MQTT identity is stable.

Relevant commits:

- `8969d98`
- `cb2ed9e`
- `6679a94`
- `e7c8eb4`
- `4cf87b8`
- `3c2bc68`
- `d42bd26`

Validation:

- Host OTA tests.
- Memory check on-device.
- Broker contract after OTA/log tooling is present.

## Stop Conditions

Stop the merge immediately if any of these occur:

- Broker does not receive `/meta` after boot.
- Broker does not receive `/meta` after MQTT recovery.
- Switch-capable device does not publish `/meta/switch`.
- Serial says `published=1` but broker does not receive the message.
- A recovery leaves only `/data` publishing while identity/status replay is lost.
- Free heap drops sharply without a clear reason.

## Notes for Final Merge to Trunk

When all milestones pass:

1. Run host tests.
2. Run cold boot and warm reload hardware checks.
3. Run broker-visible MQTT checks on sensor-only and switch-capable devices.
4. Bump `cpynodus_ii/__init__.py` once for runtime changes.
5. Merge `mqtt-merge-redo-from-139.3` into `trunk`.
