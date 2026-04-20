"""Switch runtime handle construction."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchChannelRuntime:
    """Describe the runtime state for one switch channel."""

    key: str
    channel_id: str
    enable_pin: str
    control_pin: str
    initial_state: bool
    phase: str
    errors: tuple = ()


@dataclass(frozen=True)
class SwitchRuntime:
    """Describe the switch runtime state for the current boot."""

    phase: str
    device_id: str
    channel_count: int
    channels: tuple = ()
    errors: tuple = ()


def build_switch_runtime(switch_initialization):
    """Build a lightweight runtime handle from switch initialization state."""
    if not switch_initialization.enabled:
        return SwitchRuntime(
            phase="inactive",
            device_id="",
            channel_count=0,
            channels=(),
            errors=switch_initialization.errors,
        )

    channel_runtimes = []
    for channel in switch_initialization.channels:
        channel_runtimes.append(
            SwitchChannelRuntime(
                key=channel.key,
                channel_id=channel.channel_id,
                enable_pin=channel.enable_pin,
                control_pin=channel.control_pin,
                initial_state=channel.initial_state,
                phase="ready" if channel.ready else "blocked",
                errors=channel.errors,
            )
        )

    return SwitchRuntime(
        phase="ready" if switch_initialization.ready else "blocked",
        device_id=switch_initialization.device_id,
        channel_count=switch_initialization.channel_count,
        channels=tuple(channel_runtimes),
        errors=switch_initialization.errors,
    )
