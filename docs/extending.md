# Extending cPyNodus

This project is intended to be an educational base for adding new sensors or switches.

## Add a new I2C sensor

1. Create a driver in `sensor_modules/`.
2. Add a probe step to `cPySensorFactory.detect_device()`.
3. Add configuration keys to `sensor_i2c.toml.def` as needed.
4. Document the new sensor here.

## Add a new UART/Modbus sensor

1. Add the driver module in `sensor_modules/`.
2. Update `cPySensorFactory.detect_device()` to probe UART.
3. Update `sensor_soil.toml.def` if new config is required.

## Add a new switch/relay type

1. Update `cPySwitch.py` to control the new switch behavior.
2. Update MQTT topics or discovery payloads (if needed).
3. Document the new switch type here.

## Teaching notes

- Explain the why, not just the how.
- Keep examples minimal and easy to run.
- Prefer readable code over clever code.
