# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Snapshot

- Project: `cPyNodus_II`
- Target platforms:
  - `pico2w`: Raspberry Pi Pico2 W with CircuitPython `9.2.8` verified
  - `xesp32s3`: Seeed Studio XIAO ESP32-S3 Sense with CircuitPython `10.2.1` verified
- Runtime: target-specific CircuitPython builds listed above
- Purpose: firmware for Nodus sensor/switch devices with AP onboarding,
  lightweight local web UI, MQTT integration for Sensorius/WeeWX/Home
  Assistant, calibration, log retrieval, OTA prepare/HTTP transfer, and
  constrained-memory recovery behavior.

This is not a general CPython application. Prefer CircuitPython-compatible APIs
and patterns throughout, and assume constrained board heap pressure is a
primary design constraint.

## Hard Rules

- Do not write to `/Volumes/CIRCUITPY`. The operator owns all copying, editing,
  and deployment on mounted CircuitPython device volumes.
- Before changing startup, network, MQTT, recovery, OTA, config persistence, or
  switch behavior, read the relevant Start Here docs and state which files were
  reviewed.
- For startup, network, MQTT, and recovery issues, default to investigation
  first. Do not edit runtime code until the operator has approved a proposed
  behavior change.
- Serial logs alone are not proof of MQTT success. Broker-visible MQTT output
  is required when validating MQTT behavior.

## Start Here

Before changing behavior, read the current local contract:

- `docs/README.md`: operator-level feature overview
- `docs/architecture.md`: startup, network, recovery, and runtime structure
- `docs/configuration.md`: TOML files, profile behavior, setup UI/API
- `docs/sensorius_contract.md`: canonical Sensorius MQTT contract
- `docs/mqtt.md`: short topic-family overview and MQTT notes
- `docs/ota.md`: current OTA prepare and HTTP transfer behavior
- `docs/extending.md`: adding sensors or switches

`docs/debug-notes/` contains dated investigation notes. Treat them as archival
context, not as the current runtime contract.

Before changing behavior, the agent must:

1. Run `git status --short`.
2. Read the listed contract docs relevant to the proposed change.
3. Summarize the current contract in a few concrete bullets.
4. State the proposed files and behavioral change.
5. Wait for operator approval before editing stability-sensitive runtime code.

## Current Repository Shape

- `boot.py`: target-specific filesystem/USB guard and ROFS/RWFS setup
- `code.py`: CircuitPython entrypoint and fatal traceback wrapper
- `cpynodus_ii/app.py`: main async runtime orchestration, startup, recovery,
  MQTT lifecycle, and steady state
- `cpynodus_ii/core/`: settings, config models, network stack, MQTT adapter,
  NTP, recovery, reboot/recovery logs
- `cpynodus_ii/features/`: sensor/switch services, publish cycles, command
  intake, web handlers, web config, log transfer, derived metrics
- `cpynodus_ii/hardware/`: CircuitPython hardware adapters for I2C, UART/RS485,
  and switch GPIO
- `cpynodus_ii/ota/`: private OTA state and temporary HTTP-only OTA runtime
- `lib/`: CircuitPython libraries copied to device
- `scripts/`: deploy, OTA package/push, MQTT log retrieval and analysis tools
- `tests/`: host-side pytest coverage
- `testApparatus/`: hardware/integration support routines

## Working Constraints

- Keep code lean. Memory is tight on supported CircuitPython boards.
- Avoid heavy allocations in hot paths and long-lived background state.
- Prefer simple, explicit code over extra abstraction.
- Use CircuitPython and `adafruit_*` APIs where appropriate.
- Avoid CPython-only modules, reflection-heavy patterns, threads, subprocesses,
  and filesystem assumptions that do not hold on CircuitPython.
- Avoid large inline HTML or JSON blobs.
- Add short docstrings to public functions and classes.
- Do not use concatenated multiline f-strings; use a single f-string or
  `.format(...)`.
- Preserve constrained-memory behavior unless there is a strong measured reason
  to change it.
- Treat web UI, MQTT reconnect logic, network startup, recovery, OTA, and config
  persistence as stability-sensitive areas.
- Do not silently change public config keys, MQTT topics, retained payload
  shapes, recovery semantics, or switch persistence behavior.

Feature code should not own socket lifecycle. Network ownership belongs in
`cpynodus_ii/core/network.py`, with MQTT socket/client behavior kept behind the
MQTT adapter and app-level recovery flow.

## OTA Package Creation

When the operator requests an OTA package:

- Build target-specific compiled artifacts first with `scripts/nodus_mpy.sh`
  for the requested board and its verified CircuitPython version.
- Package importable firmware modules under `cpynodus_ii/` only as `.mpy`.
  Never include the corresponding `cpynodus_ii/**/*.py` source files in an OTA
  package.
- Add each replaced module's matching `cpynodus_ii/**/*.py` path to the
  manifest `delete` list so a device cannot retain both `.py` and `.mpy`
  versions after the update.
- Include root CircuitPython source entrypoints such as `boot.py` or `code.py`
  only when the operator explicitly requests them or the requested commit range
  requires them; do not treat them as importable package modules.
- Before delivering the package, inspect `manifest.json` and fail the build if
  any `cpynodus_ii/**/*.py` path appears in `files`, if an expected `.mpy`
  artifact is missing, or if a matching source-module deletion is absent.
- If the current OTA tooling cannot build a compliant MPY-only package from the
  requested refs, stop and report that tooling gap. Do not fall back to a
  source `.py` OTA package.

## MQTT Startup and Recovery Investigation

For MQTT startup/recovery failures:

- Use `testApparatus/` first to isolate the mechanism.
- Review `docs/debug-notes/` for prior failed paths before proposing runtime
  changes.
- Do not reintroduce primer, startup conditioning, MQTT startup reorder, or
  soft-reload recovery behavior without explicit operator approval and
  broker-visible validation.
- Treat serial-side `published=1`, queued publish drain, or successful local
  API return as insufficient unless the broker capture shows the expected
  topic and payload.

## Network and Recovery Rules

The current network startup path is important. Do not reorder it casually:

1. Load settings and perform factory/profile reset work.
2. Handle soft-reload cleanup markers and warm-start radio cleanup.
3. Check private OTA state before normal profile startup.
4. Build the `NetworkStack`.
5. Fall back to AP recovery before feature services start if station join fails.
6. Refresh/persist `MQTT.BROKER_IP` when needed and writable.
7. Build MQTT from the current socket pool/SSL context.
8. Run MQTT preflight/probe logic before MiniMQTT owns the socket.
9. Start sensor, switch, web, NTP, and MQTT loops from the resolved plan.

Soft reboot means `supervisor.reload()`. It is the fast recovery path for
app-level MQTT/socket issues when cleanup can close MQTT, stop services, tear
down station networking, set the warm-start cleanup marker, and rebuild cleanly
on the next run.

Hard reset means `microcontroller.reset()`. It is used when board radio/socket
state may outlive a Python reload, including AP idle timeout, Wi-Fi recovery
timeout, Wi-Fi after-ready failure, MQTT recovery timeout, repeated MQTT
connect failures, MQTT memory allocation failures, and repeated
sensor-not-found errors.

Any change to startup or recovery should include focused tests and a clear
hardware validation note when behavior touches RF, sockets, or reboot depth.

## Configuration and Docs

Default config templates:

- `boards/settings.toml.def` -> `settings.toml`
- `boards/<target>/templates/sensor_i2c.toml.def` -> `sensor_i2c.toml`
- `boards/<target>/templates/sensor_soil.toml.def` -> `sensor_soil.toml`
- `boards/<target>/templates/switch.toml.def` -> `switch.toml`

When changing config schema or adding settings:

- Update the relevant `*.toml.def` template.
- Update `docs/configuration.md`.
- Update MQTT/Sensorius docs if the key is externally visible.
- Keep runtime behavior, templates, and docs aligned.

When adding a sensor:

- Add any needed config keys to the relevant board-specific
  `sensor_i2c.toml.def` or `sensor_soil.toml.def`.
- Update config loading in `cpynodus_ii/core/settings.py` and config models in
  `cpynodus_ii/core/config.py` when needed.
- Implement startup/read behavior in `cpynodus_ii/features/sensor_service.py`
  and hardware binding in `cpynodus_ii/hardware/sensor_adapter.py` when needed.
- Keep metric names stable and document them in `docs/README.md`,
  `docs/configuration.md`, and `docs/extending.md`.
- Add focused host tests.

When adding or changing switches:

- Keep `switch.toml` as the normal-runtime switch gate.
- Add any needed config keys to the relevant board-specific `switch.toml.def`.
- Implement control/state behavior in `cpynodus_ii/features/switch_service.py`
  and GPIO binding in `cpynodus_ii/hardware/switch_adapter.py`.
- Update MQTT payload/discovery behavior only deliberately, with
  `docs/sensorius_contract.md`, `docs/mqtt.md`, and tests updated.
- Do not break existing channel `config/set`, `config/ack`, `config/result`,
  `event`, `state`, or availability topics without an explicit migration plan.

## Testing and Verification

Host-side verification uses `pytest`. It does not execute on-device
CircuitPython firmware.

For changes, run applicable tests before claiming verification:

- General host-side verification: `pytest tests`
- Config/settings:
  `pytest tests/test_runtime_config.py tests/test_settings_bootstrap.py tests/test_persistence.py`
- MQTT/command behavior:
  `pytest tests/test_mqtt_client_adapter.py tests/test_command_intake.py tests/test_publish_cycle.py`
- Web routes/config:
  `pytest tests/test_web_routes.py tests/test_web_config.py tests/test_web_runtime.py tests/test_web_services.py`
- Network/recovery:
  `pytest tests/test_network_stack.py tests/test_recovery_policy.py tests/test_app_startup.py`
- OTA:
  `pytest tests/test_ota_state.py tests/test_ota_http.py tests/test_ota_package.py tests/test_ota_runtime.py`
- Docs-only edits: at minimum run `git diff --check`.

If hardware integration behavior changes, also run or document the relevant
manual routines under `testApparatus/`.

Manual behaviors worth validating when relevant:

- Missing SSID falls back to AP mode.
- Saving onboarding settings triggers reboot and Wi-Fi join.
- Sensor data publishes at the configured interval.
- Switch commands are honored and persisted.
- `switch.toml` remains the normal-runtime switch gate.
- Temporary network loss triggers recovery without corrupting config.
- Persistent Wi-Fi/MQTT faults escalate through the intended soft or hard
  reboot path.
- OTA prepare enters temporary HTTP-only mode only when the app filesystem is
  writable.

If you report verification, state exactly which test routines ran and whether
they passed.

## Tooling

- Primary lint config lives in `pyproject.toml`.
- Ruff settings:
  - line length `88`
  - target version `py38`
  - enabled rules `E`, `F`, `I`
- Prefer `rg` for code and docs searches.
- Use existing local patterns before adding abstractions.

## Versioning Rule

Update `cpynodus_ii/__init__.py::__version__` only when changing runtime code
that affects firmware behavior on-device.

Do not bump the version for docs-only changes, test-only changes, comments,
formatting, or other non-runtime repository edits.

If a change touches both runtime code and docs/tests, bump the version once,
using:

`v0.<year>.<doy>.<x>`

- `<year>`: 2-digit year
- `<doy>`: 3-digit day of year
- `<x>`: per-day incrementing patch counter

Rule:

1. Read the current version from `__init__.py`.
2. If `<year>` and `<doy>` match today, increment `<x>` by 1.
3. If the day changed, reset `<x>` to `1`.
4. Preserve zero padding.
5. Only update the version string, not unrelated lines.

Example:

- `v0.26.057.2` -> `v0.26.057.3` on the same day
- `v0.26.057.2` -> `v0.26.058.1` on the next day

## Practical Agent Defaults

- Start with `git status --short` and avoid overwriting user changes.
- Read existing code and tests before refactoring.
- Prefer targeted edits over broad rewrites.
- Keep docs, tests, templates, and runtime behavior in sync.
- Do not write to `/Volumes/CIRCUITPY`; the user is responsible for copying,
  editing, and deploying files on that mounted device volume.
- For docs-only work, do not bump the firmware version.
- For runtime work, add or update focused tests in the touched area.
- Keep final reports concrete: files changed, tests run, tests passed/failed,
  and any hardware validation still needed.
