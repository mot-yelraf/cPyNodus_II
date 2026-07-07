# Contributing

Thanks for your interest in improving `cPyNodus_II`.

This project is intended to be educational and approachable, while keeping
field stability and constrained-board reliability as primary goals.

It is:

- A constrained CircuitPython firmware system
- Network lifecycle aware
- Sensitive to MQTT resiliency behavior
- Coupled to board startup, recovery, and filesystem mount policy
- Strict about board, CircuitPython, and MPY build requirements

## Supported Hardware and Firmware

Verified targets:

- Raspberry Pi Pico2 W (`pico2w`): CircuitPython `9.2.8`
- Seeed Studio XIAO ESP32-S3 Sense (`xesp32s3`): CircuitPython `10.2.1`

Pico W (`RP2040`) is not supported. Contributions must stay compatible with
the verified CircuitPython version for each target unless a target/version
change has been discussed, tested on hardware, and documented.

Target-specific MPY builds must use a compiler that reports the matching
CircuitPython version and emits `mpy v6.3`. Use:

- `scripts/nodus_mpy.sh --target pico2w`
- `scripts/nodus_mpy.sh --target xesp32s3`

The deploy content options are `runtime`, `pico2w-mpy`, and `xesp32s3-mpy`
alongside the default `full` deploy. The older `mpy` option remains a
compatibility alias for `pico2w-mpy`.

## Ways to Help

- Improve docs and examples
- Fix bugs or edge cases
- Add sensor drivers or switch behavior
- Improve onboarding, OTA, or recovery flow
- Improve focused host tests for existing behavior

## Development Setup

1. Install the matching CircuitPython build on the target board.
2. Clone this repository locally.
3. Build MPY artifacts when doing compiled firmware validation:

   ```bash
   scripts/nodus_mpy.sh --target pico2w
   scripts/nodus_mpy.sh --target xesp32s3
   ```

4. Deploy with `scripts/deploy_nodus.sh` to a mounted or remote `CIRCUITPY`
   target. The operator owns copying and deployment to device volumes.
5. Copy or let first boot create live config files from the deployed templates:

   - `boards/settings.toml.def` -> `settings.toml`
   - `boards/pico2w/templates/sensor_i2c.toml.def` -> `sensor_i2c.toml`
   - `boards/pico2w/templates/sensor_soil.toml.def` -> `sensor_soil.toml`
   - `boards/pico2w/templates/switch.toml.def` -> `switch.toml`
   - `boards/xesp32s3/templates/sensor_i2c.toml.def` -> `sensor_i2c.toml`
   - `boards/xesp32s3/templates/sensor_soil.toml.def` -> `sensor_soil.toml`
   - `boards/xesp32s3/templates/switch.toml.def` -> `switch.toml`

On a clean factory deploy, Nodus creates `settings.toml` plus only the detected
live sensor and switch TOML files needed for the detected hardware.

Do not write directly to `/Volumes/CIRCUITPY` from automated agent work. Use
the deployment script only when the operator explicitly asks for deployment.

## Project Layout

- `boot.py`: target-specific filesystem/USB guard and ROFS/RWFS setup
- `code.py`: CircuitPython entrypoint and fatal traceback wrapper
- `cpynodus_ii/app.py`: main async runtime orchestration, startup, recovery,
  MQTT lifecycle, and steady state
- `cpynodus_ii/core/`: settings, config models, board profiles, network stack,
  MQTT adapter, NTP, recovery, and reboot/recovery logs
- `cpynodus_ii/features/`: sensor/switch services, publish cycles, command
  intake, web handlers, web config, log transfer, and derived metrics
- `cpynodus_ii/hardware/`: CircuitPython adapters for I2C, UART/RS485, and
  switch GPIO
- `cpynodus_ii/ota/`: private OTA state and temporary HTTP-only OTA runtime
- `lib/`: CircuitPython libraries copied to device
- `boards/`: shared and target-specific config templates
- `scripts/`: deploy, MPY build, OTA package/push, MQTT log retrieval, and
  analysis tools
- `tests/`: host-side pytest coverage
- `testApparatus/`: hardware/integration support routines

## Code Style

- Keep modules small and focused.
- Prefer clear, explicit names over clever abstractions.
- Avoid heavy allocations in hot loops; board heap is tight.
- Avoid large inline HTML, JSON, or lookup tables.
- Add short docstrings to public functions and classes.
- Keep HTTP handlers and routes minimal.
- Use CircuitPython-compatible APIs and drivers.
- Avoid CPython-only modules, threads, subprocesses, and filesystem assumptions
  that do not hold on CircuitPython.
- Do not use concatenated multiline f-strings; use a single f-string or
  `.format(...)`.

Ruff configuration lives in `pyproject.toml`.

## Adding a New Sensor

There is no `sensor_modules/` folder and no `cPySensorFactory` in
`cPyNodus_II`. The current sensor path is:

1. Add or stage any needed CircuitPython driver dependency through
   `LIB_MANIFEST` in `scripts/nodus_mpy.sh`.
2. Add any new config keys to the relevant target template:
   - `boards/<target>/templates/sensor_i2c.toml.def`
   - `boards/<target>/templates/sensor_soil.toml.def`
3. Update config loading in `cpynodus_ii/core/settings.py` and config models in
   `cpynodus_ii/core/config.py` when needed.
4. Implement startup and read behavior in
   `cpynodus_ii/features/sensor_service.py`.
5. Add or update hardware binding in
   `cpynodus_ii/hardware/sensor_adapter.py` when needed.
6. Keep metric names stable and document user-visible config or metric changes
   in `docs/README.md`, `docs/configuration.md`, and `docs/extending.md`.
7. Add focused host tests.

Current I2C `DEVICE` values include `aht`, `apvpd_aht`, `apvpd`, `aqi`,
`avpd`, `co2`, and `lux`. Soil sensors use `sensor_soil.toml`.

## Adding a New Switch

Switch support is centered in `cpynodus_ii/features/switch_service.py` and
`cpynodus_ii/hardware/switch_adapter.py`; there is no `cPySwitch.py` module in
this repo.

For switch changes:

1. Keep `switch.toml` as the normal-runtime switch gate.
2. Add any needed keys to `boards/<target>/templates/switch.toml.def`.
3. Load new settings in `cpynodus_ii/core/settings.py`.
4. Implement control/state behavior in the switch service or adapter.
5. Update MQTT payloads, discovery, or topics only deliberately.
6. Update `docs/sensorius_contract.md`, `docs/mqtt.md`,
   `docs/configuration.md`, and tests when public behavior changes.

Do not break existing channel `config/set`, `config/ack`, `config/result`,
`event`, `state`, or availability topics without an explicit migration plan.

## Stability-Sensitive Changes

Investigate first and propose behavior before changing runtime code that
touches:

- Startup ordering
- Network stack lifecycle
- MQTT adapter behavior or reconnect policy
- Recovery and reboot depth
- OTA prepare or temporary HTTP runtime
- Config persistence
- Switch persistence or public MQTT topics

The current startup contract is:

1. Load settings and handle reset/profile work.
2. Consume soft-reload cleanup markers and warm-start radio cleanup.
3. Check private OTA state before normal profile startup.
4. Build the `NetworkStack`.
5. Fall back to AP recovery before feature services start if station join
   fails.
6. Refresh or persist `MQTT.BROKER_IP` when needed and writable.
7. Build MQTT from the current socket pool and SSL context.
8. Run MQTT preflight/probe logic before MiniMQTT owns the socket.
9. Start sensor, switch, web, NTP, and MQTT loops from the resolved plan.

Soft reboot means `supervisor.reload()`. Hard reset means
`microcontroller.reset()`. Persistent Wi-Fi, MQTT, low-memory MQTT, AP idle,
and repeated sensor-not-found failures can escalate to hard reset when the
radio/socket state may outlive a Python reload.

Serial logs alone are not proof of MQTT success. Broker-visible MQTT output is
required when validating MQTT behavior.

## Testing

Host-side verification uses `pytest`; it does not execute on-device
CircuitPython firmware.

Run focused tests for the touched area:

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
- Docs-only edits: `git diff --check`

Manual hardware behaviors worth validating when relevant:

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

If hardware integration behavior changes, also run or document the relevant
manual routines under `testApparatus/`.

## Versioning

Update `cpynodus_ii/__init__.py::__version__` only when changing runtime code
that affects firmware behavior on-device.

Do not bump the version for docs-only changes, test-only changes, comments,
formatting, or other non-runtime repository edits.

Runtime version format is:

```text
v0.<year>.<doy>.<x>
```

If the current version has today's year/day-of-year, increment `<x>`. If the
day changed, reset `<x>` to `1`.

## Pull Requests

- Keep PRs small and focused.
- Avoid large refactors that change internal structure without clear stability
  benefit.
- Keep formatting-only changes separate from functional changes.
- Include a clear summary of what changed and why.
- Describe testing performed, including hardware target, CircuitPython version,
  MQTT broker type, and host tests.
- Update docs, templates, and tests together when behavior or config changes.

For MQTT-related changes, include:

- Broker-visible publish/subscribe evidence
- Connect success observations
- Reconnect behavior after broker restart
- Duration tested in minutes or hours
- Any soft reloads or hard resets observed

## Project Maturity

`cPyNodus_II` is a pre-1.0 project under active development. Interfaces and
internal architecture may evolve. Stability and clarity take precedence over
rapid feature expansion.
