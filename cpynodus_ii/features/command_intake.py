"""Command intake for runtime feature operations."""

import json
from dataclasses import dataclass, replace

from cpynodus_ii.core.config import RuntimeConfig
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.payloads import (
    build_calibration_ack_payload,
    build_calibration_result_payload,
    build_calibration_status_payload,
    build_config_ack_payload,
    build_config_result_payload,
    build_meta_patch_payload,
    mqtt_topic,
)
from cpynodus_ii.features.publish_cycle import PublishCycleResult, publish_switch_result
from cpynodus_ii.features.switch_service import apply_switch_state


@dataclass(frozen=True)
class SwitchCommand:
    """Describe a parsed switch command."""

    channel_id: str
    desired_state: bool
    message_id: str = ""


@dataclass(frozen=True)
class DeviceConfigCommand:
    """Describe one parsed device config command."""

    message_id: str
    updates: tuple
    onboard_token: str = ""


@dataclass(frozen=True)
class CalibrationCommand:
    """Describe one parsed calibration command."""

    message_id: str
    action: str
    updates: tuple = ()
    reference_ph: float | None = None
    sample_interval_s: float = 10.0
    sample_count: int = 12


@dataclass(frozen=True)
class CommandResult:
    """Describe the outcome of processing one inbound command."""

    phase: str
    topic: str
    command_type: str
    published_count: int
    errors: tuple = ()
    runtime_config: RuntimeConfig | None = None
    message_id: str = ""
    duplicate: bool = False
    persistence_mode: str = ""
    requested_state: str = ""


def subscribe_runtime_topics(transport, runtime_config):
    """Subscribe the transport to current runtime command topics."""
    topics = []
    device_id = _device_id(runtime_config)
    if device_id:
        topics.append(transport.subscribe(mqtt_topic(runtime_config, device_id, "config", "set")))
        topics.append(transport.subscribe(mqtt_topic(runtime_config, device_id, "calibration", "set")))
    for channel in runtime_config.switch.channels:
        topics.append(transport.subscribe(mqtt_topic(runtime_config, channel.channel_id, "config", "set")))
    return tuple(topics)


def process_inbound_messages(
    transport,
    runtime_config,
    switch_service,
    *,
    handled_message_ids=(),
    settings_root=None,
):
    """Process all queued inbound messages and publish resulting outputs."""
    results = []
    current_runtime_config = runtime_config
    seen_message_ids = set(handled_message_ids or ())
    device_id = _device_id(runtime_config)

    for message in transport.drain_received():
        if message.topic == mqtt_topic(current_runtime_config, device_id, "config", "set"):
            result = process_device_config_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.runtime_config is not None:
                current_runtime_config = result.runtime_config
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic == mqtt_topic(current_runtime_config, device_id, "calibration", "set"):
            result = process_calibration_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic.endswith("/config/set"):
            results.append(
                process_switch_command_message(
                    transport,
                    current_runtime_config,
                    switch_service,
                    topic=message.topic,
                    payload_text=message.payload_text,
                    settings_root=settings_root,
                )
            )

    return tuple(results)


def process_switch_command_message(
    transport, runtime_config, switch_service, *, topic, payload_text, settings_root=None
):
    """Parse, apply, and publish one switch command message."""
    if not str(payload_text or "").strip():
        return CommandResult(
            phase="ignored",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=(),
            requested_state="",
        )
    command = parse_switch_command(topic, payload_text, runtime_config=runtime_config)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=("invalid_switch_command",),
            requested_state="",
        )
    ack_topic = mqtt_topic(runtime_config, command.channel_id, "config", "ack")
    transport.publish(
        ack_topic,
        build_config_ack_payload(command.message_id, accepted=True, duplicate=False),
        retain=False,
    )
    apply_result = apply_switch_state(
        switch_service,
        channel_id=command.channel_id,
        state=command.desired_state,
    )
    publish_result = publish_switch_result(
        transport,
        runtime_config,
        apply_result,
        message_id=command.message_id,
    )
    meta_result = _publish_switch_meta_patch(
        transport,
        runtime_config,
        apply_result,
        message_id=command.message_id,
    )
    persistence_errors = ()
    if settings_root is not None:
        switch_channel = _find_runtime_channel(runtime_config, command.channel_id)
        if switch_channel is not None:
            _, _, persistence_errors = Settings.apply_updates_to_directory(
                settings_root,
                runtime_config,
                (
                    {
                        "section": "Switch",
                        "key": "{}_LAST_STATE".format(switch_channel.key),
                        "value": bool(command.desired_state),
                    },
                ),
                reload_runtime=False,
            )
    transport.publish(topic, "", retain=True)
    return CommandResult(
        phase=publish_result.phase if publish_result.phase != "skipped" else meta_result.phase,
        topic=topic,
        command_type="switch",
        published_count=1 + publish_result.published_count + meta_result.published_count + 1,
        errors=publish_result.errors + meta_result.errors + tuple(persistence_errors),
        message_id=command.message_id,
        persistence_mode="volatile" if persistence_errors else "persisted" if settings_root is not None else "",
        requested_state="ON" if command.desired_state else "OFF",
    )


def process_device_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse, apply, and publish one device config command."""
    command = parse_device_config_command(payload_text)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=0,
            errors=("schema_invalid",),
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())
    ack_topic = mqtt_topic(runtime_config, _device_id(runtime_config), "config", "ack")
    result_topic = mqtt_topic(runtime_config, _device_id(runtime_config), "config", "result")

    transport.publish(
        ack_topic,
        build_config_ack_payload(command.message_id, accepted=True, duplicate=duplicate),
        retain=False,
    )

    if duplicate:
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                duplicate=True,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
        )

    updated_runtime_config, applied_updates, persistence_errors = apply_runtime_config_updates(
        runtime_config,
        command.updates,
        settings_root=settings_root,
    )
    if not applied_updates:
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=False,
                updated=0,
                error="config_rejected",
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=tuple(persistence_errors) or ("config_rejected",),
            runtime_config=runtime_config,
            message_id=command.message_id,
            persistence_mode="volatile" if persistence_errors else "persisted",
        )

    transport.publish(
        result_topic,
        build_config_result_payload(
            command.message_id,
            applied=True,
            updated=len(applied_updates),
            error="",
        ),
        retain=False,
    )
    transport.publish(
        mqtt_topic(updated_runtime_config, _device_id(updated_runtime_config), "meta", "patch"),
        build_meta_patch_payload(
            updated_runtime_config,
            source="config_set",
            message_id=command.message_id,
            updates=applied_updates,
        ),
        retain=False,
    )
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="config",
        published_count=3,
        errors=tuple(persistence_errors),
        runtime_config=updated_runtime_config,
        message_id=command.message_id,
        persistence_mode="volatile" if persistence_errors else "persisted",
    )


def process_calibration_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse and respond to one device calibration command."""
    command = parse_calibration_command(payload_text)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="calibration",
            published_count=0,
            errors=("schema_invalid",),
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())
    device_id = _device_id(runtime_config)
    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")
    published_count = 0

    transport.publish(
        ack_topic,
        build_calibration_ack_payload(command.message_id, accepted=True),
        retain=False,
    )
    published_count += 1

    if duplicate:
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 1,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
        )

    if command.action in {"apply", "set", "update"}:
        if not command.updates:
            transport.publish(
                result_topic,
                build_calibration_result_payload(
                    command.message_id,
                    applied=False,
                    updated=0,
                    error="schema_invalid",
                ),
                retain=False,
            )
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="calibration",
                published_count=published_count + 1,
                errors=("schema_invalid",),
                runtime_config=runtime_config,
                message_id=command.message_id,
            )
        updated_runtime_config = runtime_config
        applied_updates = tuple(command.updates)
        persistence_errors = ()
        if settings_root is not None:
            _, applied_updates, persistence_errors = Settings.apply_updates_to_directory(
                settings_root,
                runtime_config,
                command.updates,
                reload_runtime=False,
            )
        if applied_updates:
            updated_runtime_config, _, _ = apply_runtime_config_updates(
                runtime_config,
                applied_updates,
                settings_root=None,
            )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=len(applied_updates),
                error="",
            ),
            retain=False,
        )
        transport.publish(
            mqtt_topic(updated_runtime_config, device_id, "meta", "patch"),
            build_meta_patch_payload(
                updated_runtime_config,
                source="calibration_set",
                message_id=command.message_id,
                updates=applied_updates,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=tuple(persistence_errors),
            runtime_config=updated_runtime_config,
            message_id=command.message_id,
            persistence_mode="volatile" if persistence_errors else "persisted",
        )

    if command.action == "status":
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            build_calibration_status_payload(runtime_config, status="idle", calibrated=False),
            retain=True,
        )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                status=build_calibration_status_payload(
                    runtime_config,
                    status="idle",
                    calibrated=False,
                ),
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    transport.publish(
        result_topic,
        build_calibration_result_payload(
            command.message_id,
            applied=False,
            updated=0,
            error="calibration_not_supported",
        ),
        retain=False,
    )
    return CommandResult(
        phase="error",
        topic=topic,
        command_type="calibration",
        published_count=published_count + 1,
        errors=("calibration_not_supported",),
        runtime_config=runtime_config,
        message_id=command.message_id,
    )


def parse_switch_command(topic, payload_text, runtime_config=None):
    """Parse a compact ON/OFF or JSON switch command payload."""
    channel_id = _channel_id_from_topic(topic)
    if not channel_id:
        return None

    text = str(payload_text or "").strip()
    if not text:
        return None

    normalized = text.upper()
    if normalized in {"ON", "OFF"}:
        return SwitchCommand(
            channel_id=channel_id,
            desired_state=(normalized == "ON"),
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    desired_state = _extract_switch_state(payload)
    if desired_state is None and runtime_config is not None:
        desired_state = _extract_switch_state_from_config_updates(
            payload,
            runtime_config,
            channel_id=channel_id,
        )
    if desired_state is None:
        return None
    return SwitchCommand(
        channel_id=channel_id,
        desired_state=desired_state,
        message_id=str(payload.get("message_id", "") or "").strip(),
    )


def parse_device_config_command(payload_text):
    """Parse device-level config/set payloads."""
    payload = _parse_json_object(payload_text)
    if payload is None:
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    if not message_id:
        return None
    body = payload.get("payload") or {}
    updates = _extract_config_updates(body)
    if updates is None:
        return None
    return DeviceConfigCommand(
        message_id=message_id,
        updates=tuple(updates),
        onboard_token=str(payload.get("onboard_token", "") or "").strip(),
    )


def parse_calibration_command(payload_text):
    """Parse calibration/set payloads."""
    payload = _parse_json_object(payload_text)
    if payload is None:
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    action = str(payload.get("action", "") or "").strip().lower()
    if not (message_id and action):
        return None
    body = payload.get("payload") or {}
    updates = _extract_calibration_updates(body)
    reference_ph = body.get("reference_ph")
    sample_interval_s = float(body.get("sample_interval_s", 10) or 10)
    sample_count = int(body.get("sample_count", 12) or 12)
    return CalibrationCommand(
        message_id=message_id,
        action=action,
        updates=tuple(updates),
        reference_ph=float(reference_ph) if reference_ph is not None else None,
        sample_interval_s=sample_interval_s,
        sample_count=max(6, min(18, sample_count)),
    )


def apply_runtime_config_updates(runtime_config, updates, *, settings_root=None):
    """Apply supported runtime updates and return the new config plus applied writes."""
    if settings_root is not None:
        persisted_runtime_config, persisted_updates, persistence_errors = Settings.apply_updates_to_directory(
            settings_root,
            runtime_config,
            updates,
            reload_runtime=False,
        )
        current = runtime_config
        for update in persisted_updates:
            section = str(update.get("section", "") or "").strip()
            key = str(update.get("key", "") or "").strip()
            value = update.get("value")
            updated = _apply_runtime_config_update(current, section, key, value)
            if updated is None:
                continue
            current = updated
        return current, tuple(persisted_updates), tuple(persistence_errors)
    current = runtime_config
    applied_updates = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        value = update.get("value")
        updated = _apply_runtime_config_update(current, section, key, value)
        if updated is None:
            continue
        current = updated
        applied_updates.append(
            {
                "section": section,
                "key": key,
                "value": value,
            }
        )
    return current, tuple(applied_updates), ()


def _apply_runtime_config_update(runtime_config, section, key, value):
    key_upper = key.upper()
    if section == "Network" and key_upper == "HOSTNAME":
        return replace(
            runtime_config,
            network=replace(runtime_config.network, hostname=str(value or "").strip()),
        )
    if section == "Network" and key_upper == "HTTPPORT":
        return replace(
            runtime_config,
            network=replace(runtime_config.network, http_port=int(value or 8000)),
        )
    if section == "MQTT" and key_upper == "BROKER":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, broker=str(value or "").strip()),
        )
    if section == "MQTT" and key_upper == "BROKER_IP":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, broker_ip=str(value or "").strip()),
        )
    if section == "MQTT" and key_upper == "PORT":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, port=int(value or 1883)),
        )
    if section == "MQTT" and key_upper == "BASE_TOPIC":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, base_topic=str(value or "").strip()),
        )
    if section == "HomeAssistant" and key_upper == "DISCOVERY_PREFIX":
        return replace(
            runtime_config,
            homeassistant=replace(
                runtime_config.homeassistant,
                discovery_prefix=str(value or "").strip(),
            ),
        )
    if section == "HomeAssistant" and key_upper == "BASE_TOPIC":
        return replace(
            runtime_config,
            homeassistant=replace(
                runtime_config.homeassistant,
                base_topic=str(value or "").strip(),
            ),
        )
    if section == "Time" and key_upper == "TZ":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz=str(value or "").strip()),
        )
    if section == "Time" and key_upper == "TZ_OFFSET":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz_offset=int(value or 0)),
        )
    if section == "Time" and key_upper == "TZ_NAME":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz_name=str(value or "").strip()),
        )
    if section == "Time" and key_upper == "NTP_SERVER":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, ntp_server=str(value or "").strip()),
        )
    if section == "Profile" and key_upper == "ACTIVE_PROFILE":
        return replace(runtime_config, active_profile=str(value or "").strip())
    if section == "Sensor" and key_upper == "LOCATION":
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, location=str(value or "").strip()),
        )
    if section == "Sensor" and key_upper == "SENSOR_ID":
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, sensor_id=str(value or "").strip()),
        )
    if section == "Sensor" and key_upper == "SERIAL_NUM":
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, serial_number=str(value or "").strip()),
        )
    calibration_attr = _calibration_attr_name(section, key_upper)
    if calibration_attr and section == "Calibration.System":
        calibration = replace(
            runtime_config.sensor.calibration_system,
            **{calibration_attr: float(value or 0.0)},
        )
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, calibration_system=calibration),
        )
    if calibration_attr and section == "Calibration.Device":
        calibration = replace(
            runtime_config.sensor.calibration_device,
            **{calibration_attr: float(value or 0.0)},
        )
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, calibration_device=calibration),
        )
    return None


def _calibration_attr_name(section, key_upper):
    if section not in {"Calibration.System", "Calibration.Device"}:
        return ""
    mapping = {
        "TEMP_OFFSET": "temp_offset",
        "RH_OFFSET": "rh_offset",
        "CO2_OFFSET": "co2_offset",
        "AQI_OFFSET": "aqi_offset",
        "GAS_OFFSET": "gas_offset",
        "LUX_OFFSET": "lux_offset",
        "PPFD_OFFSET": "ppfd_offset",
        "SOIL_TEMP_CAL_VAL": "soil_temp_cal_val",
        "SOIL_TEMP_MOIST_VAL": "soil_temp_moist_val",
        "SOIL_PH_CAL_VAL": "soil_ph_cal_val",
        "SOIL_EC_CAL_VAL": "soil_ec_cal_val",
    }
    return mapping.get(key_upper, "")


def _publish_switch_meta_patch(transport, runtime_config, apply_result, *, message_id):
    if apply_result.phase != "ready":
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=apply_result.errors,
        )

    channel = _find_runtime_channel(runtime_config, apply_result.channel_id)
    if channel is None:
        return PublishCycleResult(
            phase="error",
            published_count=0,
            topics=(),
            errors=("switch_channel_not_found",),
        )

    topic = mqtt_topic(runtime_config, _device_id(runtime_config), "meta", "patch")
    transport.publish(
        topic,
        build_meta_patch_payload(
            runtime_config,
            source="switch_set",
            message_id=message_id,
            updates=(
                {
                    "section": "Switch",
                    "key": "{}_LAST_STATE".format(channel.key),
                    "value": bool(apply_result.applied_state),
                },
            ),
        ),
        retain=False,
    )
    return PublishCycleResult(
        phase="published",
        published_count=1,
        topics=(topic,),
        errors=(),
    )


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _find_runtime_channel(runtime_config, channel_id):
    for channel in getattr(runtime_config.switch, "channels", ()):
        if getattr(channel, "channel_id", "") == channel_id:
            return channel
    return None


def _channel_id_from_topic(topic):
    text = str(topic or "").strip()
    parts = text.split("/")
    if len(parts) >= 3:
        return parts[1]
    return ""


def _extract_switch_state(payload):
    candidates = [
        payload.get("state"),
        payload.get("set"),
        (payload.get("payload") or {}).get("state")
        if isinstance(payload.get("payload"), dict)
        else None,
        (payload.get("payload") or {}).get("set")
        if isinstance(payload.get("payload"), dict)
        else None,
    ]
    for value in candidates:
        normalized = _normalize_switch_state(value)
        if normalized is not None:
            return normalized
    return None


def _extract_switch_state_from_config_updates(payload, runtime_config, *, channel_id):
    if not isinstance(payload, dict):
        return None

    channel = _find_runtime_channel(runtime_config, channel_id)
    if channel is None:
        return None

    expected_keys = {
        "{}_LAST_STATE".format(channel.key).upper(),
        "{}_STATE".format(channel.key).upper(),
    }
    body = payload.get("payload")
    if not isinstance(body, dict):
        return None

    updates = body.get("updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section", "") or "").strip()
            key = str(item.get("key", "") or "").strip().upper()
            if section == "Switch" and key in expected_keys:
                normalized = _normalize_switch_state(item.get("value"))
                if normalized is not None:
                    return normalized

    settings = body.get("settings")
    if isinstance(settings, dict):
        switch_settings = settings.get("Switch")
        if isinstance(switch_settings, dict):
            for key in expected_keys:
                if key in switch_settings:
                    normalized = _normalize_switch_state(switch_settings.get(key))
                    if normalized is not None:
                        return normalized
    return None


def _normalize_switch_state(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"ON", "TRUE", "1"}:
        return True
    if text in {"OFF", "FALSE", "0"}:
        return False
    return None


def _parse_json_object(payload_text):
    try:
        payload = json.loads(str(payload_text or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _extract_config_updates(body):
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("updates"), list):
        updates = []
        for item in body["updates"]:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section", "") or "").strip()
            key = str(item.get("key", "") or "").strip()
            if not (section and key):
                continue
            updates.append(
                {
                    "section": section,
                    "key": key,
                    "value": item.get("value"),
                }
            )
        return updates
    if isinstance(body.get("settings"), dict):
        updates = []
        for section, values in body["settings"].items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                updates.append(
                    {
                        "section": str(section or "").strip(),
                        "key": str(key or "").strip(),
                        "value": value,
                    }
                )
        return updates
    return None


def _extract_calibration_updates(body):
    if not isinstance(body, dict):
        return ()
    if isinstance(body.get("offsets"), list):
        updates = []
        for item in body["offsets"]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "") or "").strip()
            value = item.get("value")
            section, normalized_key = _normalize_calibration_key(key)
            if section and normalized_key:
                updates.append(
                    {
                        "section": section,
                        "key": normalized_key,
                        "value": value,
                    }
                )
        return tuple(updates)
    calibration = body.get("calibration")
    if isinstance(calibration, dict):
        updates = []
        for branch, values in calibration.items():
            if not isinstance(values, dict):
                continue
            section = _calibration_section_for_branch(branch)
            if not section:
                continue
            for key, value in values.items():
                updates.append(
                    {
                        "section": section,
                        "key": str(key or "").strip(),
                        "value": value,
                    }
                )
        return tuple(updates)
    return ()


def _calibration_section_for_branch(branch):
    normalized = str(branch or "").strip().lower()
    if normalized == "system":
        return "Calibration.System"
    if normalized == "device":
        return "Calibration.Device"
    if normalized == "soil":
        return "Calibration.Soil"
    if normalized == "apvpd":
        return "Calibration"
    return ""


def _normalize_calibration_key(key):
    text = str(key or "").strip()
    if not text:
        return "", ""
    if "." in text:
        parts = text.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:-1]), parts[-1]
    if text == "soil_ph_offset":
        return "Calibration.Device", "SOIL_PH_CAL_VAL"
    return "", ""
