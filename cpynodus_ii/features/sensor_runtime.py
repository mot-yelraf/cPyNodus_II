"""Construct sensor runtime handles from initialization decisions.

These helpers convert sensor plans into concrete runtime objects that bind
settings, adapters, and service state for use by the main application loop.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SensorRuntime:
    """Describe the sensor runtime state for the current boot."""

    phase: str
    family: str
    interface: str
    device: str
    sensor_id: str
    config_file: str
    transport_target: str
    errors: tuple = ()


def build_sensor_runtime(sensor_initialization, runtime_config):
    """Build a lightweight runtime handle from sensor initialization state."""
    if not sensor_initialization.enabled:
        return SensorRuntime(
            phase="inactive",
            family="",
            interface="",
            device="",
            sensor_id="",
            config_file="",
            transport_target="",
            errors=sensor_initialization.errors,
        )

    if not sensor_initialization.ready:
        return SensorRuntime(
            phase="blocked",
            family=sensor_initialization.family,
            interface=sensor_initialization.interface,
            device=sensor_initialization.device,
            sensor_id=sensor_initialization.sensor_id,
            config_file=sensor_initialization.config_file,
            transport_target="",
            errors=sensor_initialization.errors,
        )

    sensor = runtime_config.sensor
    transport_target = ""
    if sensor.interface == "i2c" and sensor.i2c is not None:
        transport_target = "i2c:{}@0x{:02x}".format(sensor.i2c.bus, sensor.i2c.address)
    elif sensor.interface == "modbus_rs485" and sensor.modbus is not None:
        targets = []
        channels = getattr(sensor.modbus, "channels", ()) or (sensor.modbus,)
        for channel in tuple(channels):
            targets.append(
                "{}={}:{}@{}".format(
                    getattr(channel, "name", "") or "MODBUS",
                    channel.uart_tx,
                    channel.uart_rx,
                    channel.address,
                )
            )
        transport_target = ",".join(targets)

    return SensorRuntime(
        phase="ready",
        family=sensor_initialization.family,
        interface=sensor_initialization.interface,
        device=sensor_initialization.device,
        sensor_id=sensor_initialization.sensor_id,
        config_file=sensor_initialization.config_file,
        transport_target=transport_target,
        errors=(),
    )
