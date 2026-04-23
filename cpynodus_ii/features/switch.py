"""Plan switch-feature activation for the current boot.

This module decides whether switch control should start and prepares normalized
channel-level initialization data from the active runtime configuration.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchChannelInitialization:
    """Describe initialization intent for one switch channel."""

    key: str
    channel_id: str
    enable_pin: str
    control_pin: str
    initial_state: bool
    ready: bool
    errors: tuple = ()


@dataclass(frozen=True)
class SwitchInitialization:
    """Describe switch initialization intent for the current boot."""

    enabled: bool
    ready: bool
    device_id: str
    channel_count: int
    channels: tuple = ()
    errors: tuple = ()


def plan_switch_initialization(runtime_config):
    """Build switch initialization state from normalized runtime config."""
    switch = runtime_config.switch
    if not switch.present:
        return SwitchInitialization(
            enabled=False,
            ready=False,
            device_id="",
            channel_count=0,
            channels=(),
            errors=("no_switch_config",),
        )

    channel_states = []
    errors = []
    for channel in switch.channels:
        channel_errors = []
        if not channel.channel_id:
            channel_errors.append("missing_channel_id")
        if not channel.enable_pin:
            channel_errors.append("missing_enable_pin")
        if not channel.control_pin:
            channel_errors.append("missing_control_pin")
        channel_states.append(
            SwitchChannelInitialization(
                key=channel.key,
                channel_id=channel.channel_id,
                enable_pin=channel.enable_pin,
                control_pin=channel.control_pin,
                initial_state=channel.last_state,
                ready=not channel_errors,
                errors=tuple(channel_errors),
            )
        )
        errors.extend(f"{channel.key}:{error}" for error in channel_errors)

    if not channel_states:
        errors.append("no_switch_channels")

    return SwitchInitialization(
        enabled=True,
        ready=not errors,
        device_id=switch.device_id,
        channel_count=switch.channel_count,
        channels=tuple(channel_states),
        errors=tuple(errors),
    )
