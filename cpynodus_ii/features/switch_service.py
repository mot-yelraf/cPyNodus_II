"""Manage switch state application and switch-service snapshots.

This layer translates desired switch states into hardware operations, tracks
persisted and live state, and exposes small result objects for callers and
tests.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchApplyResult:
    """Describe the result of one switch state apply operation."""

    phase: str
    key: str
    device_id: str
    channel_id: str
    applied_state: bool | None
    errors: tuple = ()


@dataclass(frozen=True)
class SwitchChannelService:
    """Describe one active switch channel service."""

    key: str
    channel_id: str
    phase: str
    desired_state: bool
    enable_handle: object | None = None
    control_handle: object | None = None
    errors: tuple = ()


@dataclass(frozen=True)
class SwitchService:
    """Describe the active switch service state."""

    phase: str
    device_id: str
    channel_count: int
    channels: tuple = ()
    errors: tuple = ()


def start_switch_service(switch_runtime, switch_adapter):
    """Apply initial switch state to bound hardware handles."""
    if switch_adapter.phase != "bound":
        return SwitchService(
            phase=switch_adapter.phase,
            device_id=switch_runtime.device_id,
            channel_count=switch_runtime.channel_count,
            channels=(),
            errors=switch_adapter.errors or switch_runtime.errors,
        )

    channel_services = []
    errors = []
    for channel in switch_adapter.channels:
        if channel.phase != "bound":
            channel_services.append(
                SwitchChannelService(
                    key=channel.key,
                    channel_id="",
                    phase=channel.phase,
                    desired_state=channel.desired_state,
                    errors=channel.errors,
                )
            )
            errors.extend(f"{channel.key}:{error}" for error in channel.errors)
            continue

        _set_pin_value(channel.enable_handle, True)
        _set_pin_value(channel.control_handle, channel.desired_state)
        channel_services.append(
            SwitchChannelService(
                key=channel.key,
                channel_id=getattr(channel, "channel_id", ""),
                phase="ready",
                desired_state=channel.desired_state,
                enable_handle=channel.enable_handle,
                control_handle=channel.control_handle,
                errors=(),
            )
        )

    return SwitchService(
        phase="ready" if not errors else "error",
        device_id=switch_runtime.device_id,
        channel_count=switch_runtime.channel_count,
        channels=tuple(channel_services),
        errors=tuple(errors),
    )


def stop_switch_service(switch_service):
    """Deinitialize switch handles if supported."""
    for channel in switch_service.channels:
        _safe_deinit(getattr(channel, "enable_handle", None))
        _safe_deinit(getattr(channel, "control_handle", None))


def apply_switch_state(switch_service, *, channel_id=None, channel_key=None, state):
    """Apply a new state to one switch channel."""
    if switch_service.phase != "ready":
        return SwitchApplyResult(
            phase=switch_service.phase,
            key=channel_key or "",
            device_id=switch_service.device_id,
            channel_id=channel_id or "",
            applied_state=None,
            errors=switch_service.errors,
        )

    channel = _find_channel(switch_service, channel_id=channel_id, channel_key=channel_key)
    if channel is None:
        return SwitchApplyResult(
            phase="error",
            key=channel_key or "",
            device_id=switch_service.device_id,
            channel_id=channel_id or "",
            applied_state=None,
            errors=("switch_channel_not_found",),
        )
    if channel.phase != "ready":
        return SwitchApplyResult(
            phase=channel.phase,
            key=channel.key,
            device_id=switch_service.device_id,
            channel_id=channel_id or "",
            applied_state=None,
            errors=channel.errors,
        )

    desired_state = bool(state)
    _set_pin_value(channel.enable_handle, True)
    _set_pin_value(channel.control_handle, desired_state)
    return SwitchApplyResult(
        phase="ready",
        key=channel.key,
        device_id=switch_service.device_id,
        channel_id=channel.channel_id,
        applied_state=desired_state,
        errors=(),
    )


def snapshot_switch_states(switch_service):
    """Return a compact mapping of switch channel states."""
    if switch_service.phase not in {"ready", "error"}:
        return {}
    snapshots = {}
    for channel in switch_service.channels:
        value = getattr(getattr(channel, "control_handle", None), "value", None)
        snapshots[channel.key] = {
            "phase": channel.phase,
            "state": bool(value) if value is not None else None,
        }
    return snapshots


def _set_pin_value(handle, value):
    if handle is not None and hasattr(handle, "value"):
        handle.value = bool(value)


def _find_channel(switch_service, *, channel_id=None, channel_key=None):
    for channel in switch_service.channels:
        if channel_key and channel.key == channel_key:
            return channel
        if channel_id and channel.channel_id == channel_id:
            return channel
    return None


def _safe_deinit(handle):
    if handle is None:
        return
    deinit = getattr(handle, "deinit", None)
    if callable(deinit):
        try:
            deinit()
        except Exception:
            pass
