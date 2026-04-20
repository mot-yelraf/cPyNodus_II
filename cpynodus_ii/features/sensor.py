"""Sensor feature initialization planning."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SensorInitialization:
    """Describe sensor initialization intent for the current boot."""

    enabled: bool
    ready: bool
    family: str
    interface: str
    device: str
    sensor_id: str
    config_file: str
    errors: tuple = ()


def plan_sensor_initialization(runtime_config):
    """Build sensor initialization state from normalized runtime config."""
    sensor = runtime_config.sensor
    if not sensor.present:
        return SensorInitialization(
            enabled=False,
            ready=False,
            family="",
            interface="",
            device="",
            sensor_id="",
            config_file="",
            errors=("no_sensor_config",),
        )

    errors = []
    if sensor.interface == "i2c":
        if sensor.i2c is None:
            errors.append("missing_i2c_config")
        else:
            if not sensor.i2c.scl_pin:
                errors.append("missing_i2c_scl_pin")
            if not sensor.i2c.sda_pin:
                errors.append("missing_i2c_sda_pin")
            if sensor.i2c.address <= 0:
                errors.append("missing_i2c_address")
    elif sensor.interface == "modbus_rs485":
        if sensor.modbus is None:
            errors.append("missing_modbus_config")
        else:
            if not sensor.modbus.uart_tx:
                errors.append("missing_modbus_uart_tx")
            if not sensor.modbus.uart_rx:
                errors.append("missing_modbus_uart_rx")
            if sensor.modbus.address <= 0:
                errors.append("missing_modbus_address")
            if sensor.modbus.baud <= 0:
                errors.append("invalid_modbus_baud")
    else:
        errors.append("unsupported_sensor_interface")

    return SensorInitialization(
        enabled=True,
        ready=not errors,
        family=sensor.family,
        interface=sensor.interface,
        device=sensor.device,
        sensor_id=sensor.sensor_id,
        config_file=sensor.active_config_file,
        errors=tuple(errors),
    )
