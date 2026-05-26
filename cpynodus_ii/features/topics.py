"""Small MQTT topic helpers safe to import from startup paths."""


def mqtt_base_topic(runtime_config):
    """Return the normalized MQTT topic prefix for the active runtime."""
    topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "").strip()
    return topic or "nodus"


def mqtt_topic(runtime_config, *parts):
    """Build one MQTT topic under the active runtime base topic."""
    members = [
        str(part or "").strip("/") for part in parts if str(part or "").strip("/")
    ]
    if not members:
        return mqtt_base_topic(runtime_config)
    return "{}/{}".format(mqtt_base_topic(runtime_config), "/".join(members))
