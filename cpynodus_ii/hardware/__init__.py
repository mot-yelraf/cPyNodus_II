"""Expose hardware adapter factories used by feature services.

The hardware package isolates board-specific bindings for sensors and switches
so higher-level feature code can operate against small adapter contracts.
"""

from cpynodus_ii.hardware.sensor_adapter import (
    SensorHardwareAdapter,
    bind_sensor_hardware,
)
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
