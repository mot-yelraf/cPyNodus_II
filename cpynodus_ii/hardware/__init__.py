"""Hardware adapter bindings for cPyNodus_II."""

from cpynodus_ii.hardware.sensor_adapter import SensorHardwareAdapter, bind_sensor_hardware
from cpynodus_ii.hardware.switch_adapter import (
    SwitchChannelHardwareAdapter,
    SwitchHardwareAdapter,
    bind_switch_hardware,
)

__all__ = [
    "SensorHardwareAdapter",
    "SwitchChannelHardwareAdapter",
    "SwitchHardwareAdapter",
    "bind_sensor_hardware",
    "bind_switch_hardware",
]
