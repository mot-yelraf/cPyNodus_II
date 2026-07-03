# Extending cPyNodus_II

This project is meant to be hackable by makers, educators, and students. The
main idea is simple: configuration tells Nodus what kind of sensor is attached,
the hardware layer opens the bus, and the sensor service starts the driver and
turns driver readings into Nodus metric names.

The current sensor startup path is:

1. `sensor_i2c.toml` or `sensor_soil.toml` describes the attached sensor.
2. `cpynodus_ii/core/settings.py` reads the TOML into a normalized
   `RuntimeConfig`.
3. `cpynodus_ii/features/sensor.py` checks that the required settings exist.
4. `cpynodus_ii/hardware/sensor_adapter.py` opens the I2C or UART bus.
5. `cpynodus_ii/features/sensor_service.py` imports the driver, creates the
   driver object, reads values, applies calibration, and returns metric names.
6. The normal publish path sends those metrics to the web UI and MQTT.

There is no `sensor_modules/` folder and no `cPySensorFactory` in
`cPyNodus_II`.

## Add a New I2C Sensor

Start with an I2C sensor if you are adding a small breakout board. Most
Adafruit and community CircuitPython drivers fit this pattern.

1. Add the CircuitPython driver to the target library staging set.

   Add the dependency path to `LIB_MANIFEST` in `scripts/nodus_mpy.sh` so the
   right 9.x or 10.x bundle copy is staged under
   `build/firmware/<target>/lib/`. Also include any required dependency
   libraries in `~/Projects/mcu_libs`. Keep the driver set small because board
   heap and filesystem space are still constrained.

2. Choose a short `DEVICE` name.

   The `DEVICE` value is the internal sensor type key. Existing examples are
   `aht`, `apvpd_aht`, `aqi`, `co2`, `lux`, `avpd`, `apvpd`, and `soil`.
   Retained `meta` and `/itaot-meta` may also expose a concrete hardware family
   through `sensor.hardware`; update `DetectedSensor.hardware` when a new
   logical device needs to report a Sensorius-visible hardware type.

   Use a short lowercase name, for example:

   ```toml
   [Sensor]
   DEVICE = "uv"
   SENSOR_ID = "uv-classroom-1"
   LOCATION = "Classroom"
   ```

3. Add the sensor settings to the board-specific `sensor_i2c.toml.def`.

   Most I2C sensors need only the existing `[Sensor]`, `[I2Cbus]`,
   `[Calibration]`, and `[Display]` sections in
   `boards/<target>/templates/sensor_i2c.toml.def`. Set an example address and
   useful display metrics:

   ```toml
   [Sensor]
   DEVICE = "uv"

   [I2Cbus]
   I2C_SCL = "GP1"
   I2C_SDA = "GP0"
   I2C_ADDR = 0x10

   [Display]
   METRIC_1 = "UV Index"
   METRIC_2 = "Visible Light"
   ```

   If the sensor needs new calibration fields, add them under
   `[Calibration.Device]`. Then update `SensorCalibration` and
   `Settings._detect_sensor()` so those fields are loaded.

4. Start the driver in `cpynodus_ii/features/sensor_service.py`.

   Add a branch to `_start_i2c_sensor_service()`. This is where Nodus imports
   the driver from the deployed CircuitPython `lib/` directory and creates the
   driver object on the already-open I2C bus.

   ```python
   if device == "uv":
       module = _load_module("adafruit_example_sensor", modules, "missing_adafruit_example_sensor")
       if module is None:
           return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_example_sensor")
       driver = module.ExampleSensor(transport, address=sensor.i2c.address)
       return SensorService(
           phase="ready",
           device=device,
           interface=sensor.interface,
           driver_kind="adafruit_example_sensor",
           driver=driver,
           transport=transport,
           errors=(),
       )
   ```

5. Read the sensor in `read_sensor_snapshot()`.

   Add a matching `if sensor.device == "uv":` block. Read simple properties
   from the driver and return a compact metric dictionary.

   ```python
   if sensor.device == "uv":
       metrics = _compact_metrics(
           {
               "UV Index": _maybe_round(getattr(sensor_service.driver, "uv_index", None), 2),
               "Visible Light": _maybe_round(getattr(sensor_service.driver, "visible", None), 0),
           }
       )
       metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
       return SensorSnapshot(
           phase="ready",
           sensor_id=sensor.sensor_id,
           device=sensor.device,
           metrics=metrics,
           errors=(),
       )
   ```

   Keep metric names stable. MQTT payloads, the web UI, Home Assistant
   discovery, and display settings all depend on these names.

6. Add derived metrics only when needed.

   If your sensor can publish raw readings directly, do not add anything to
   `cpynodus_ii/features/derived_metrics.py`. If it needs a calculated value,
   add a small device branch in `enrich_metrics()`.

7. Add tests.

   Use fake driver modules instead of requiring hardware. Good starting points:

   - `tests/test_feature_services.py` for driver startup and snapshot readings
   - `tests/test_feature_initialization.py` for planning and config behavior
   - `tests/test_runtime_config.py` if you added config fields
   - `tests/test_publish_cycle.py` if metric names affect MQTT or Home Assistant

8. Run the focused tests, then the full host suite.

   ```bash
   pytest tests/test_feature_services.py
   pytest tests
   ```

   Host tests do not run on CircuitPython hardware, but they catch most config,
   service, and payload mistakes before copying code to a board.

## Add a UART or Modbus Sensor

The existing `soil` device is the main UART/RS485 example. It uses
`sensor_soil.toml`, `SoilModbusClient`, register maps, and scale maps. The
soil path supports CH1, CH2, or both channels on the dual-channel RS485 hat
through `[Modbus.CH1]` and `[Modbus.CH2]`; leave a channel's UART pins blank to
disable it.

For another Modbus sensor:

1. Add or extend the relevant sections in
   `boards/<target>/templates/sensor_soil.toml.def`.
2. Load those fields in `Settings._detect_sensor()`.
3. Check required UART settings in `plan_sensor_initialization()`.
4. Bind the UART in `sensor_adapter.py` or reuse the existing Modbus binding.
5. Add read logic in `sensor_service.py`.
6. Document the register map and scale values in `docs/configuration.md`.

For a non-Modbus UART sensor, add a new interface deliberately. That means
updating `DetectedSensor`, `plan_sensor_initialization()`,
`build_sensor_runtime()`, `bind_sensor_hardware()`, and `start_sensor_service()`.
Keep this change small and tested because it changes the startup contract.

## When to Change Configuration Docs

Update `docs/configuration.md` whenever you add or change:

- a TOML section
- a TOML key
- a valid `DEVICE` value
- a metric name users are expected to put in `[Display]`
- a calibration field
- a register map or scaling rule

Also update the relevant template:

- I2C sensors: `boards/<target>/templates/sensor_i2c.toml.def`
- soil/Modbus sensors: `boards/<target>/templates/sensor_soil.toml.def`

## Add a New Switch or Relay Type

Switch support is intentionally simpler than sensor support. Runtime switch
state lives in `cpynodus_ii/features/switch_service.py`, while hardware pin
binding lives in `cpynodus_ii/hardware/switch_adapter.py`.

For a new switch behavior:

1. Add any needed settings to `boards/<target>/templates/switch.toml.def`.
2. Load them in `cpynodus_ii/core/settings.py`.
3. Implement control/state behavior in the switch service or adapter.
4. Update MQTT payload or discovery behavior if the public contract changes.
5. Update `docs/configuration.md` and this document.
6. Add host tests for the new behavior.

Do not silently change existing switch topics or config keys. Deployed devices
and Sensorius may already depend on them.

## CircuitPython Tips

- Use CircuitPython-compatible drivers and APIs only.
- Keep imports and objects small; board memory is limited.
- Avoid large inline JSON, HTML, or lookup tables.
- Prefer simple property reads over complex background workers.
- Add short docstrings to public functions and classes.
- Do not use CPython-only modules.
- Test with fake modules on the host, then test on real hardware.
