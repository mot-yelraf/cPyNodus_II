"""Payload builders for normalized runtime and MQTT publication."""

from time import time


def mqtt_base_topic(runtime_config):
    """Return the normalized MQTT topic prefix for the active runtime."""
    topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "").strip()
    return topic or "nodus"


def mqtt_topic(runtime_config, *parts):
    """Build one MQTT topic under the active runtime base topic."""
    members = [str(part or "").strip("/") for part in parts if str(part or "").strip("/")]
    if not members:
        return mqtt_base_topic(runtime_config)
    return "{}/{}".format(mqtt_base_topic(runtime_config), "/".join(members))


def build_sensor_data_payload(runtime_config, sensor_snapshot):
    """Build a compact sensor data payload from an enriched sensor snapshot."""
    sensor = runtime_config.sensor
    return {
        "schema": "nodus-sensor-data/v1",
        "sensor_id": sensor.sensor_id,
        "device": sensor.device,
        "location": sensor.location,
        "values": dict(sensor_snapshot.metrics or {}),
        "timestamp": int(time()),
    }


def build_sensor_availability_payload(runtime_config, *, online):
    sensor = runtime_config.sensor
    return {
        "schema": "nodus-availability/v1",
        "sensor_id": sensor.sensor_id,
        "status": "online" if online else "offline",
        "timestamp": int(time()),
    }


def build_switch_state_payload(runtime_config, switch_state_snapshot):
    """Build per-channel switch state payloads keyed by switch channel key."""
    payloads = {}
    for channel in runtime_config.switch.channels:
        snapshot = switch_state_snapshot.get(channel.key, {})
        state = snapshot.get("state")
        payloads[channel.key] = {
            "schema": "nodus-switch-state/v1",
            "device_id": runtime_config.switch.device_id,
            "channel_id": channel.channel_id,
            "label": channel.label,
            "state": "ON" if state else "OFF",
            "timestamp": int(time()),
        }
    return payloads


def build_device_heartbeat_payload(runtime_config, *, online):
    """Build the compact heartbeat payload for device liveness."""
    device_id = runtime_config.sensor.sensor_id or runtime_config.switch.device_id or runtime_config.network.hostname
    return {
        "schema": "nodus-heartbeat/v1",
        "device_id": device_id,
        "status": "online" if online else "offline",
        "timestamp": int(time()),
    }


def build_runtime_meta_payload(runtime_config, *, version, active_broker=""):
    """Build the retained runtime metadata payload."""
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = sensor.sensor_id or switch.device_id or runtime_config.network.hostname
    location = sensor.location or switch.location
    members = [member for member in [sensor.sensor_id] + [channel.channel_id for channel in switch.channels] if member]

    payload = {
        "schema": "nodus-meta/v1",
        "device_id": device_id,
        "hostname": runtime_config.network.hostname,
        "serial": sensor.serial_number or switch.serial_number,
        "type": "nodus",
        "version": version,
        "capabilities": {
            "sensor": sensor.present,
            "switch": switch.present,
        },
        "status": {
            "heartbeat_topic": mqtt_topic(runtime_config, device_id, "status", "heartbeat"),
        },
        "mqtt": {
            "broker": runtime_config.mqtt.broker,
            "broker_ip": runtime_config.mqtt.broker_ip,
            "active_broker": str(active_broker or runtime_config.mqtt.preferred_host or ""),
            "port": runtime_config.mqtt.port,
        },
        "location_group": {
            "location": location,
            "members": members,
        },
        "timestamp": int(time()),
    }

    if sensor.present:
        payload["sensor"] = {
            "sensor_id": sensor.sensor_id,
            "location": sensor.location,
            "data_topic": mqtt_topic(runtime_config, sensor.sensor_id, "data"),
            "event_topic": mqtt_topic(runtime_config, sensor.sensor_id, "event"),
            "availability_topic": mqtt_topic(runtime_config, sensor.sensor_id, "availability"),
        }

    if switch.present:
        channels = []
        for index, channel in enumerate(switch.channels, start=1):
            channels.append(
                {
                    "index": index,
                    "label": channel.label,
                    "channel_id": channel.channel_id,
                    "enable_pin": channel.enable_pin,
                    "pin": channel.control_pin,
                    "state_topic": mqtt_topic(runtime_config, channel.channel_id, "state"),
                    "set_topic": mqtt_topic(runtime_config, channel.channel_id, "config", "set"),
                    "result_topic": mqtt_topic(runtime_config, channel.channel_id, "config", "result"),
                    "availability_topic": mqtt_topic(runtime_config, channel.channel_id, "availability"),
                }
            )
        payload["switch"] = {
            "device_id": switch.device_id,
            "location": switch.location,
            "channels": channels,
        }

    return payload


def build_config_ack_payload(message_id, *, accepted=True, duplicate=False):
    return {
        "message_id": str(message_id or ""),
        "accepted": bool(accepted),
        "duplicate": bool(duplicate),
    }


def build_config_result_payload(
    message_id,
    *,
    applied,
    updated=0,
    error="",
    duplicate=False,
):
    return {
        "message_id": str(message_id or ""),
        "applied": bool(applied),
        "updated": int(updated or 0),
        "duplicate": bool(duplicate),
        "error": str(error or ""),
    }


def build_meta_patch_payload(runtime_config, *, source, message_id, updates):
    device_id = (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )
    normalized_updates = []
    sections = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        if not (section and key):
            continue
        if section not in sections:
            sections.append(section)
        normalized_updates.append(
            {
                "section": section,
                "key": key,
                "value": update.get("value"),
            }
        )
    return {
        "schema": "nodus-meta-patch/v1",
        "device_id": device_id,
        "timestamp": int(time()),
        "source": str(source or ""),
        "message_id": str(message_id or ""),
        "sections": sections,
        "updates": normalized_updates,
    }


def build_calibration_ack_payload(message_id, *, accepted=True):
    return {
        "message_id": str(message_id or ""),
        "accepted": bool(accepted),
    }


def build_calibration_result_payload(
    message_id,
    *,
    applied,
    updated=0,
    error="",
    started=False,
    status=None,
    sample_interval_s=None,
    sample_count=None,
    reference_ph=None,
):
    payload = {
        "message_id": str(message_id or ""),
        "applied": bool(applied),
        "updated": int(updated or 0),
        "error": str(error or ""),
    }
    if started:
        payload["started"] = True
    if status is not None:
        payload["status"] = dict(status)
    if sample_interval_s is not None:
        payload["sample_interval_s"] = float(sample_interval_s)
    if sample_count is not None:
        payload["sample_count"] = int(sample_count)
    if reference_ph is not None:
        payload["reference_ph"] = float(reference_ph)
    return payload


def build_calibration_status_payload(runtime_config, *, status="idle", calibrated=False, extra=None):
    sensor = runtime_config.sensor
    payload = {
        "schema": "nodus-calibration-status/v1",
        "status": str(status or "idle"),
        "calibrated": bool(calibrated),
        "sensor_id": sensor.sensor_id,
        "timestamp": int(time()),
    }
    if isinstance(extra, dict):
        payload.update(extra)
    return payload
