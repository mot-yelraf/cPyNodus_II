"""Build normalized payloads for MQTT publication and web consumption.

The payload helpers keep topic-specific formatting, discovery metadata, and
state serialization in one place so publish paths remain consistent across the
runtime.
"""

from time import time

from cpynodus_ii.core.obfuscation import encode_password


def _slugify(value):
    text = str(value or "").strip().lower()
    out = []
    previous_underscore = False
    for char in text:
        code = ord(char)
        if (48 <= code <= 57) or (97 <= code <= 122) or char == "_":
            out.append(char)
            previous_underscore = char == "_"
            continue
        if not previous_underscore:
            out.append("_")
            previous_underscore = True
    return "".join(out).strip("_") or "field"


def _short_hash(value):
    accumulator = 2166136261
    for byte in str(value or "").encode("utf-8", "ignore"):
        accumulator ^= byte
        accumulator = (accumulator * 16777619) & 0xFFFFFFFF
    return "{:08x}".format(accumulator)[:6]


def _ha_device_block(runtime_config):
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = _slugify(
        sensor.sensor_id
        or switch.device_id
        or runtime_config.network.hostname
        or "nodus"
    )
    name = (
        sensor.sensor_id
        or switch.device_id
        or runtime_config.network.hostname
        or "Nodus"
    )
    location = sensor.location or switch.location
    device = {
        "identifiers": [device_id],
        "name": "{} {}".format(name, location).strip() if location else name,
        "manufacturer": "Nodus",
        "model": sensor.device or "switch" if switch.present else "",
    }
    if not device["model"]:
        device.pop("model")
    return device


def _ha_state_base_topic(runtime_config, entity_id):
    return mqtt_topic(runtime_config, entity_id)


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


def build_switch_event_payload(runtime_config, channel, state, *, message_id=""):
    """Build a non-retained switch event payload for accepted state changes."""
    return {
        "schema": "nodus-switch-event/v1",
        "device_id": runtime_config.switch.device_id,
        "channel_id": channel.channel_id,
        "label": channel.label,
        "state": "ON" if state else "OFF",
        "message_id": str(message_id or ""),
        "timestamp": int(time()),
    }


def build_switch_meta_payload(runtime_config, switch_state_snapshot=None):
    """Build retained switch channel metadata for the split meta contract."""
    switch = runtime_config.switch
    device_id = (
        runtime_config.sensor.sensor_id
        or switch.device_id
        or runtime_config.network.hostname
    )
    state_snapshot = switch_state_snapshot or {}
    channels = []
    for index, channel in enumerate(switch.channels, start=1):
        snapshot = state_snapshot.get(channel.key, {})
        state = snapshot.get("state")
        if state is None:
            state = bool(channel.last_state)
        channels.append(
            {
                "index": index,
                "label": channel.label,
                "channel_id": channel.channel_id,
                "state": bool(state),
                "event_topic": mqtt_topic(runtime_config, channel.channel_id, "event"),
                "state_topic": mqtt_topic(runtime_config, channel.channel_id, "state"),
                "set_topic": mqtt_topic(
                    runtime_config, channel.channel_id, "config", "set"
                ),
                "ack_topic": mqtt_topic(
                    runtime_config, channel.channel_id, "config", "ack"
                ),
                "result_topic": mqtt_topic(
                    runtime_config, channel.channel_id, "config", "result"
                ),
                "availability_topic": mqtt_topic(
                    runtime_config, channel.channel_id, "availability"
                ),
            }
        )
    return {
        "schema": "nodus-meta-switch/v1",
        "device_id": device_id,
        "switch_device_id": switch.device_id,
        "location": switch.location,
        "channel_count": len(switch.channels),
        "channels": channels,
        "timestamp": int(time()),
    }


def build_device_heartbeat_payload(runtime_config, *, online):
    """Build the compact heartbeat payload for device liveness."""
    device_id = (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )
    return {
        "schema": "nodus-heartbeat/v1",
        "device_id": device_id,
        "status": "online" if online else "offline",
        "timestamp": int(time()),
    }


def build_runtime_meta_payload(
    runtime_config,
    *,
    version,
    active_broker="",
    include_switch_channels=True,
):
    """Build the retained runtime metadata payload."""
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = sensor.sensor_id or switch.device_id or runtime_config.network.hostname
    location = sensor.location or switch.location
    members = [
        member
        for member in [sensor.sensor_id]
        + [channel.channel_id for channel in switch.channels]
        if member
    ]

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
            "fwupdate": True,
        },
        "status": {
            "state": "online",
            "heartbeat_topic": mqtt_topic(
                runtime_config, device_id, "status", "heartbeat"
            ),
        },
        "network": {
            "ssid": runtime_config.network.ssid,
            "password": _obfuscated_password(
                runtime_config.network.password, runtime_config
            ),
            "hostname": runtime_config.network.hostname,
        },
        "profile": {
            "active_profile": runtime_config.active_profile,
        },
        "mqtt": {
            "broker": runtime_config.mqtt.broker,
            "broker_ip": runtime_config.mqtt.broker_ip,
            "active_broker": str(
                active_broker or runtime_config.mqtt.preferred_host or ""
            ),
            "port": runtime_config.mqtt.port,
            "use_tls": bool(runtime_config.mqtt.use_tls),
            "username": runtime_config.mqtt.username,
            "password": _obfuscated_password(
                runtime_config.mqtt.password, runtime_config
            ),
            "base_topic": runtime_config.mqtt.base_topic,
        },
        "fwupdate": {
            "schema": "nodus-fwupdate/v1",
            "transport": "http",
            "prepare_topic": mqtt_topic(runtime_config, device_id, "fwupdate"),
            "ack_topic": mqtt_topic(runtime_config, device_id, "fwupdate", "ack"),
            "result_topic": mqtt_topic(
                runtime_config, device_id, "fwupdate", "result"
            ),
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
            "display_metrics": _display_metrics_for_meta(sensor),
            "display_styles": _display_styles_for_meta(sensor),
            "data_topic": mqtt_topic(runtime_config, sensor.sensor_id, "data"),
            "event_topic": mqtt_topic(runtime_config, sensor.sensor_id, "event"),
            "availability_topic": mqtt_topic(
                runtime_config, sensor.sensor_id, "availability"
            ),
        }

    if switch.present:
        channels = []
        if include_switch_channels:
            for index, channel in enumerate(switch.channels, start=1):
                channels.append(
                    {
                        "index": index,
                        "label": channel.label,
                        "channel_id": channel.channel_id,
                        "enable_pin": channel.enable_pin,
                        "pin": channel.control_pin,
                        "state": bool(channel.last_state),
                        "event_topic": mqtt_topic(
                            runtime_config, channel.channel_id, "event"
                        ),
                        "state_topic": mqtt_topic(
                            runtime_config, channel.channel_id, "state"
                        ),
                        "set_topic": mqtt_topic(
                            runtime_config, channel.channel_id, "config", "set"
                        ),
                        "result_topic": mqtt_topic(
                            runtime_config, channel.channel_id, "config", "result"
                        ),
                        "availability_topic": mqtt_topic(
                            runtime_config, channel.channel_id, "availability"
                        ),
                    }
                )
        payload["switch"] = {
            "device_id": switch.device_id,
            "location": switch.location,
            "channel_count": len(switch.channels),
            "meta_topic": mqtt_topic(runtime_config, device_id, "meta", "switch"),
        }
        if include_switch_channels:
            payload["switch"]["channels"] = channels

    return payload


def _display_metrics_for_meta(sensor):
    metrics = tuple(getattr(getattr(sensor, "display", None), "metrics", ()) or ())
    return [
        str(metric or "").strip() for metric in metrics if str(metric or "").strip()
    ]


def _display_styles_for_meta(sensor):
    styles = tuple(getattr(getattr(sensor, "display", None), "styles", ()) or ())
    return [str(style or "").strip() for style in styles if str(style or "").strip()]


def _obfuscated_password(password, runtime_config):
    return encode_password(
        str(password or ""),
        hostname=getattr(getattr(runtime_config, "network", None), "hostname", ""),
    )


def build_onboarding_hello_payload(runtime_config, onboarding_state, *, version):
    """Build the MQTT onboarding hello payload when bootstrap state exists."""
    state = onboarding_state if isinstance(onboarding_state, dict) else {}
    onboard_token = str(state.get("onboard_token", "") or "").strip()
    if not onboard_token:
        return None

    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = sensor.sensor_id or switch.device_id or runtime_config.network.hostname
    serial_number = sensor.serial_number or switch.serial_number

    return {
        "onboard_token": onboard_token,
        "device_id": device_id,
        "hostname": runtime_config.network.hostname,
        "serial": serial_number,
        "type": "pico2w",
        "version": version,
        "capabilities": {
            "sensor": bool(sensor.present),
            "switch": bool(switch.present),
        },
    }


def build_homeassistant_discovery_plan(
    runtime_config,
    *,
    sensor_snapshot=None,
    retain=True,
    previous_topics=None,
):
    """Build HA discovery messages plus stale retained-topic cleanup."""
    if (
        str(getattr(runtime_config, "active_profile", "") or "").strip().lower()
        != "homeassistant"
    ):
        return ()

    discovery_prefix = str(
        getattr(runtime_config.homeassistant, "discovery_prefix", "") or "homeassistant"
    ).strip()
    if not discovery_prefix:
        discovery_prefix = "homeassistant"
    device = _ha_device_block(runtime_config)
    base_id = device["identifiers"][0]
    messages = []
    published_topics = set()

    metrics = ()
    if sensor_snapshot is not None and getattr(sensor_snapshot, "phase", "") == "ready":
        metric_map = getattr(sensor_snapshot, "metrics", {}) or {}
        metrics = tuple(metric_map.keys())
    unit_map = {
        "Temperature": "C",
        "Rel-Humidity": "%",
        "Humidity": "%",
        "CO2": "ppm",
        "Air Quality": "%",
        "Light Intensity": "lx",
        "Auto Light": "lx",
        "PPFD": "umol/m2/s",
        "Estimated PPFD": "umol/m2/s",
        "Visible Light Intensity": "mol/m2/day",
        "Pressure": "Pa",
        "Soil Temp_C": "C",
        "Soil Temp_F": "F",
        "Soil Moisture": "%",
        "Soil Moisture Deficit": "%",
        "Soil Stress Index": "%",
    }
    used_object_ids = set()
    if runtime_config.sensor.present:
        sensor_id = runtime_config.sensor.sensor_id
        sensor_data_topic = mqtt_topic(runtime_config, sensor_id, "data")
        availability_topic = mqtt_topic(runtime_config, sensor_id, "availability")
        for metric in metrics:
            metric_text = str(metric or "").strip()
            if not metric_text:
                continue
            object_id = _slugify(metric_text)
            if object_id in used_object_ids:
                object_id = "{}_{}".format(object_id, _short_hash(metric_text))
            used_object_ids.add(object_id)
            topic = "{}/sensor/{}/{}/config".format(
                discovery_prefix, base_id, object_id
            )
            payload = {
                "name": "{} {}".format(base_id, metric_text),
                "unique_id": "{}_{}".format(base_id, object_id),
                "state_topic": sensor_data_topic,
                "value_template": "{{{{ value_json['values'].get('{}', '') }}}}".format(
                    metric_text
                ),
                "availability_topic": availability_topic,
                "availability_template": "{{ value_json['status'] }}",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
            }
            unit = unit_map.get(metric_text)
            if unit is None:
                unit = _prefixed_soil_unit(metric_text, unit_map)
            if unit:
                payload["unit_of_measurement"] = unit
            messages.append((topic, payload, bool(retain), False))
            published_topics.add(topic)

    if runtime_config.switch.present:
        for channel in runtime_config.switch.channels:
            channel_id = str(channel.channel_id or "").strip()
            if not channel_id:
                continue
            object_id = _slugify(channel_id)
            topic = "{}/switch/{}/{}/config".format(
                discovery_prefix, base_id, object_id
            )
            payload = {
                "name": "{} {}".format(base_id, channel.label or channel_id),
                "unique_id": "{}_{}".format(base_id, object_id),
                "state_topic": mqtt_topic(runtime_config, channel_id, "state"),
                "command_topic": mqtt_topic(
                    runtime_config, channel_id, "config", "set"
                ),
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "availability_topic": mqtt_topic(
                    runtime_config, channel_id, "availability"
                ),
                "availability_template": "{{ value_json['status'] }}",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
            }
            messages.append((topic, payload, bool(retain), False))
            published_topics.add(topic)

    previous_topics = set(previous_topics or ())
    if retain:
        stale_topics = previous_topics - published_topics
        for topic in stale_topics:
            messages.append((topic, "", True, True))
    return tuple(messages)


def _prefixed_soil_unit(metric_text, unit_map):
    for suffix, unit in unit_map.items():
        if str(metric_text or "").endswith(" {}".format(suffix)):
            return unit
    return None


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


def build_calibration_status_payload(
    runtime_config, *, status="idle", calibrated=False, extra=None
):
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
