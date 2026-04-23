# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

- Project: `cPyNodus_II`
- Target platform: Raspberry Pi Pico2 W only
- Runtime: CircuitPython `9.2.8`
- Purpose: firmware for Nodus sensor/switch devices with AP onboarding, lightweight web UI, MQTT integration for Sensorius and Home Assistant, and constrained-memory operation

This is not a general CPython application. Prefer CircuitPython-compatible APIs and patterns throughout.


## Working Constraints

- Keep code lean. Memory is tight on Pico2 W.
- Avoid heavy allocations in hot paths.
- Prefer simple, explicit code over extra abstraction.
- Use CircuitPython and `adafruit_*` APIs where appropriate.
- Avoid CPython-only modules or patterns, circuitpython is not cpython.
- Avoid large inline HTML or JSON blobs.
- Add short docstrings to public functions and classes.
- Do not use concatenated multiline f-strings; use a single f-string or `.format(...)`.

## Configuration and Docs

Default config templates:

- `settings.toml.def` -> `settings.toml`
- `sensor_i2c.toml.def` -> `sensor_i2c.toml`
- `sensor_soil.toml.def` -> `sensor_soil.toml`
- `switch.toml.def` -> `switch.toml`

When changing config schema or adding settings:

- Update the relevant `*.toml.def` template
- Update `docs/configuration.md`
- Keep runtime behavior and docs aligned

When adding features:

- New sensor:
  - add any needed config keys to `sensor_i2c.toml.def`
  - document in `docs/extending.md`
- New switch:
  - implement control/state in `cPySwitch.py`
  - update MQTT topics and discovery payloads
  - document in `docs/extending.md`

## Testing and Verification

Host-side verification uses `pytest`. It does not execute on-device CircuitPython firmware.

For changes, run applicable tests before claiming verification:

- General host-side verification: `pytest tests`
- Targeted checks for touched areas, for example:
  - `pytest tests/test_mqtt_client.py`
  - `pytest tests/test_web_routes.py`
- If hardware integration behavior changes, also run relevant routines under `testApparatus/`

Manual behaviors worth validating when relevant:

- Missing SSID falls back to AP mode
- Saving onboarding settings triggers reboot and Wi-Fi join
- Sensor data publishes at the configured interval
- Switch commands are honored and persisted
- `switch.toml` remains the normal-runtime switch gate
- Network loss triggers restart and recovery

If you report verification, state exactly which test routines ran and whether they passed.

## Tooling

- Primary lint config lives in `pyproject.toml`
- Ruff settings:
  - line length `88`
  - target version `py38`
  - enabled rules `E`, `F`, `I`

## Versioning Rule

When you make a code change, update `__version__` in `__init__.py` using:

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

- Read existing patterns before refactoring.
- Preserve constrained-memory behavior unless there is a strong reason to change it.
- Prefer targeted edits over broad rewrites.
- Treat web UI, MQTT reconnect logic, and config persistence as stability-sensitive areas.
- Do not silently change public config keys, MQTT topics, or recovery semantics.

