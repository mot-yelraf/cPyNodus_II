"""Synchronize logical switch configuration and scalar persistence.

MQTT and HTTP callers run these steps sequentially after applying an output.
Update the mutable channel in place and reuse the streaming writer to limit
allocations and Python stack depth without loading a complete TOML document.
"""


def update_switch_runtime_config(config, channel_key, state):
    """Record an applied state in the existing mutable channel configuration."""
    for channel in config.switch.channels:
        if channel.key == channel_key:
            channel.last_state = bool(state)
            break
    return config


def persist_switch_state(settings_root, channel_key, state):
    """Persist one applied state, returning mode and bounded failure details."""
    if settings_root is None:
        return "volatile", ("settings_not_writable",)
    try:
        from cpynodus_ii.features.scalar_persistence import write_toml_scalar

        path = "{}/switch.toml".format(str(settings_root or ".").rstrip("/"))
        errors = write_toml_scalar(
            path, "Switch", "{}_LAST_STATE".format(channel_key), bool(state)
        )
    except MemoryError:
        errors = ("switch_state_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" not in str(exc).lower():
            raise
        errors = ("switch_state_persist_pystack",)
    return ("volatile" if errors else "persisted"), tuple(errors)
