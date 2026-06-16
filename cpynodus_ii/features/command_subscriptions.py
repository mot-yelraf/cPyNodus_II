"""Tiny MQTT command topic subscription helpers.

The steady-state loop uses these helpers during MQTT connect without importing
the heavier command parsing and persistence modules.
"""

from cpynodus_ii.features.topics import mqtt_topic


def subscribe_runtime_topics(transport, runtime_config):
    """Subscribe the transport to current runtime command topics."""
    topics = []
    device_id = _device_id(runtime_config)
    if device_id:
        topics.append(
            transport.subscribe(mqtt_topic(runtime_config, device_id, "config", "set"))
        )
        topics.append(
            transport.subscribe(
                mqtt_topic(runtime_config, device_id, "calibration", "set")
            )
        )
        topics.append(
            transport.subscribe(mqtt_topic(runtime_config, device_id, "fwupdate"))
        )
        topics.extend(_subscribe_log_transfer_topics(transport, runtime_config))
    topics.extend(subscribe_switch_runtime_topics(transport, runtime_config))
    return tuple(topics)


def subscribe_device_runtime_topics(transport, runtime_config):
    """Subscribe the transport to device-level command topics."""
    topics = []
    device_id = _device_id(runtime_config)
    if not device_id:
        return tuple(topics)
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "config", "set"))
    )
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "calibration", "set"))
    )
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "fwupdate"))
    )
    topics.extend(_subscribe_log_transfer_topics(transport, runtime_config))
    return tuple(topics)


def subscribe_switch_runtime_topics(transport, runtime_config):
    """Subscribe the transport to switch channel command topics."""
    topics = []
    for channel in runtime_config.switch.channels:
        topics.append(
            transport.subscribe(
                mqtt_topic(runtime_config, channel.channel_id, "config", "set")
            )
        )
    return tuple(topics)


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _subscribe_log_transfer_topics(transport, runtime_config):
    device_id = _device_id(runtime_config)
    if not device_id:
        return ()
    return (transport.subscribe(mqtt_topic(runtime_config, device_id, "logs", "get")),)
