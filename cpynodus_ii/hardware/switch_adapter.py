"""Bind configured switch channels to board-level output transports.

The switch hardware adapters keep GPIO and relay-specific details out of the
service layer and provide a normalized interface for switch state changes.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchChannelHardwareAdapter:
    """Describe the hardware binding for one switch channel runtime."""

    key: str
    channel_id: str
    phase: str
    desired_state: bool
    enable_handle: object | None = None
    control_handle: object | None = None
    errors: tuple = ()


@dataclass(frozen=True)
class SwitchHardwareAdapter:
    """Describe the hardware binding for a switch runtime."""

    phase: str
    device_id: str
    channel_count: int
    channels: tuple = ()
    errors: tuple = ()


def bind_switch_hardware(switch_runtime, *, board_module=None, digitalio_module=None):
    """Bind the switch runtime to CircuitPython pin objects when possible."""
    if switch_runtime.phase != "ready":
        return SwitchHardwareAdapter(
            phase=switch_runtime.phase,
            device_id=switch_runtime.device_id,
            channel_count=switch_runtime.channel_count,
            channels=(),
            errors=switch_runtime.errors,
        )

    errors = []
    board_module = board_module or _try_import_module(
        "board", "board_module_unavailable", errors
    )
    digitalio_module = digitalio_module or _try_import_module(
        "digitalio",
        "digitalio_module_unavailable",
        errors,
    )
    if errors:
        return SwitchHardwareAdapter(
            phase="error",
            device_id=switch_runtime.device_id,
            channel_count=switch_runtime.channel_count,
            channels=(),
            errors=tuple(errors),
        )

    channel_bindings = []
    for channel in switch_runtime.channels:
        channel_errors = []
        enable_pin = _resolve_pin(
            board_module,
            channel.enable_pin,
            "missing_enable_pin_object",
            channel_errors,
        )
        control_pin = _resolve_pin(
            board_module,
            channel.control_pin,
            "missing_control_pin_object",
            channel_errors,
        )
        if channel_errors:
            channel_bindings.append(
                SwitchChannelHardwareAdapter(
                    key=channel.key,
                    channel_id=channel.channel_id,
                    phase="error",
                    desired_state=channel.initial_state,
                    errors=tuple(channel_errors),
                )
            )
            errors.extend(f"{channel.key}:{error}" for error in channel_errors)
            continue

        enable_handle = digitalio_module.DigitalInOut(enable_pin)
        control_handle = digitalio_module.DigitalInOut(control_pin)
        _configure_output(enable_handle, digitalio_module)
        _configure_output(control_handle, digitalio_module)
        channel_bindings.append(
            SwitchChannelHardwareAdapter(
                key=channel.key,
                channel_id=channel.channel_id,
                phase="bound",
                desired_state=channel.initial_state,
                enable_handle=enable_handle,
                control_handle=control_handle,
                errors=(),
            )
        )

    return SwitchHardwareAdapter(
        phase="bound" if not errors else "error",
        device_id=switch_runtime.device_id,
        channel_count=switch_runtime.channel_count,
        channels=tuple(channel_bindings),
        errors=tuple(errors),
    )


def _configure_output(handle, digitalio_module):
    direction = getattr(getattr(digitalio_module, "Direction", None), "OUTPUT", None)
    if direction is not None and hasattr(handle, "direction"):
        handle.direction = direction


def _try_import_module(module_name, error_code, errors):
    try:
        return __import__(module_name)
    except ImportError:
        errors.append(error_code)
        return None


def _resolve_pin(board_module, pin_name, error_code, errors):
    pin = getattr(board_module, pin_name, None)
    if pin is None:
        errors.append(error_code)
    return pin
