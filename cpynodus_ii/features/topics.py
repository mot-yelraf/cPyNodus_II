"""Build canonical MQTT topics without loading feature implementations.

``mqtt_base_topic`` and ``mqtt_topic`` normalize configured base paths and
device identifiers. Keep this module allocation-light because startup and
low-stack command paths import it directly.
"""


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
