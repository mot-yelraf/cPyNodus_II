"""Lightweight runtime command subscriptions and lazy dispatch.

The normal steady-state loop imports this module on every MQTT-enabled device.
Heavy command handlers stay behind function wrappers so startup does not load
settings mutation, OTA, log transfer, calibration, or switch-control modules
until an inbound command actually needs them.
"""

from cpynodus_ii.features.command_models import (  # noqa: F401
    CalibrationCommand,
    CommandResult,
    DeviceConfigCommand,
    FwUpdateCommand,
    SoilPhCalibrationSession,
    SwitchCommand,
)
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


def process_inbound_messages(
    transport,
    runtime_config,
    switch_service,
    *,
    handled_message_ids=(),
    settings_root=None,
):
    """Process queued inbound messages, importing handlers only when needed."""
    if not getattr(transport, "received_messages", ()):
        return ()
    messages = tuple(transport.drain_received())
    if not messages:
        return ()

    results = []
    for index, message in enumerate(messages):
        if _is_switch_command_topic(message.topic, runtime_config):
            results.append(
                process_switch_command_message(
                    transport,
                    runtime_config,
                    switch_service,
                    topic=message.topic,
                    payload_text=message.payload_text,
                    settings_root=settings_root,
                )
            )
            continue

        _restore_received_messages(transport, messages[index:])
        heavy_results = _process_heavy_inbound_messages(
            transport,
            runtime_config,
            switch_service,
            handled_message_ids=handled_message_ids,
            settings_root=settings_root,
            topic=message.topic,
        )
        return tuple(results) + tuple(heavy_results)

    return tuple(results)


def process_soil_calibration_session(
    transport,
    runtime_config,
    sensor_service,
    *,
    now_monotonic,
    settings_root=None,
):
    """Advance an active soil pH calibration session when one exists."""
    if getattr(transport, "_soil_ph_session", None) is None:
        return CommandResult(
            phase="ignored",
            topic="",
            command_type="calibration_session",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
        )
    return _handlers().process_soil_calibration_session(
        transport,
        runtime_config,
        sensor_service,
        now_monotonic=now_monotonic,
        settings_root=settings_root,
    )


def process_fwupdate_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse and persist one firmware-update prepare command."""
    return _handlers().process_fwupdate_message(
        transport,
        runtime_config,
        topic=topic,
        payload_text=payload_text,
        duplicate_message_ids=duplicate_message_ids,
        settings_root=settings_root,
    )


def process_switch_command_message(
    transport,
    runtime_config,
    switch_service,
    *,
    topic,
    payload_text,
    settings_root=None,
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

    try:
        command = parse_switch_command(
            topic,
            payload_text,
            runtime_config=runtime_config,
        )
    except MemoryError:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=("switch_command_memory",),
            requested_state="",
        )
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=("invalid_switch_command",),
            requested_state="",
        )

    transport.publish(
        mqtt_topic(runtime_config, command.channel_id, "config", "ack"),
        _build_config_ack_payload(command.message_id, accepted=True, duplicate=False),
        retain=False,
    )
    apply_result = _apply_switch_state(
        switch_service,
        channel_id=command.channel_id,
        state=command.desired_state,
    )
    publish_result = _publish_switch_result(
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
    persistence_errors = _persist_switch_state(
        runtime_config,
        command,
        settings_root=settings_root,
    )
    return CommandResult(
        phase=publish_result.phase
        if publish_result.phase != "skipped"
        else meta_result.phase,
        topic=topic,
        command_type="switch",
        published_count=1
        + int(publish_result.published_count or 0)
        + int(meta_result.published_count or 0),
        errors=publish_result.errors + meta_result.errors + tuple(persistence_errors),
        message_id=command.message_id,
        persistence_mode="volatile"
        if persistence_errors
        else "persisted"
        if settings_root is not None
        else "",
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
    return _handlers().process_device_config_message(
        transport,
        runtime_config,
        topic=topic,
        payload_text=payload_text,
        duplicate_message_ids=duplicate_message_ids,
        settings_root=settings_root,
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
    return _handlers().process_calibration_message(
        transport,
        runtime_config,
        topic=topic,
        payload_text=payload_text,
        duplicate_message_ids=duplicate_message_ids,
        settings_root=settings_root,
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
        import gc
        import json

        gc.collect()
        payload = json.loads(text)
    except (ImportError, ValueError):
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
    return _handlers().parse_device_config_command(payload_text)


def parse_calibration_command(payload_text):
    """Parse calibration/set payloads."""
    return _handlers().parse_calibration_command(payload_text)


def parse_fwupdate_command(payload_text):
    """Parse firmware-update control payloads."""
    return _handlers().parse_fwupdate_command(payload_text)


def _handlers():
    from cpynodus_ii.features import command_handlers

    return command_handlers


def _process_heavy_inbound_messages(
    transport,
    runtime_config,
    switch_service,
    *,
    handled_message_ids,
    settings_root,
    topic,
):
    try:
        import gc

        gc.collect()
    except Exception:
        pass
    try:
        return _handlers().process_inbound_messages(
            transport,
            runtime_config,
            switch_service,
            handled_message_ids=handled_message_ids,
            settings_root=settings_root,
        )
    except MemoryError:
        _clear_received_messages(transport)
        return (
            CommandResult(
                phase="error",
                topic=topic,
                command_type="command",
                published_count=0,
                errors=("command_handler_memory",),
                runtime_config=runtime_config,
            ),
        )


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


def _restore_received_messages(transport, messages):
    existing = list(getattr(transport, "received_messages", ()) or ())
    transport.received_messages = list(messages) + existing


def _clear_received_messages(transport):
    try:
        transport.received_messages.clear()
    except AttributeError:
        transport.received_messages = []


def _is_switch_command_topic(topic, runtime_config):
    text = str(topic or "").strip()
    if not text.endswith("/config/set"):
        return False
    for channel in getattr(runtime_config.switch, "channels", ()):
        if text == mqtt_topic(runtime_config, channel.channel_id, "config", "set"):
            return True
    return False


def _apply_switch_state(switch_service, *, channel_id, state):
    from cpynodus_ii.features.switch_service import apply_switch_state

    return apply_switch_state(
        switch_service,
        channel_id=channel_id,
        state=state,
    )


def _publish_switch_result(transport, runtime_config, apply_result, *, message_id=""):
    if apply_result.phase != "ready":
        return _publish_result(
            "skipped",
            0,
            errors=apply_result.errors,
        )
    channel = _find_runtime_channel(runtime_config, apply_result.channel_id)
    if channel is None:
        return _publish_result(
            "error",
            0,
            errors=("switch_channel_not_found",),
        )

    result_topic = mqtt_topic(runtime_config, channel.channel_id, "config", "result")
    event_topic = mqtt_topic(runtime_config, channel.channel_id, "event")
    state_topic = mqtt_topic(runtime_config, channel.channel_id, "state")
    state_payload = "ON" if apply_result.applied_state else "OFF"
    result_message = transport.publish(
        result_topic,
        _build_config_result_payload(
            message_id,
            applied=True,
            updated=1,
            error="",
        ),
        retain=False,
    )
    event_message = transport.publish(
        event_topic,
        _build_switch_event_payload(
            runtime_config,
            channel,
            bool(apply_result.applied_state),
            message_id=message_id,
        ),
        retain=False,
    )
    state_message = transport.publish(state_topic, state_payload, retain=True)
    return _publish_result(
        "published",
        3,
        topics=(result_message.topic, event_message.topic, state_message.topic),
    )


def _publish_switch_meta_patch(transport, runtime_config, apply_result, *, message_id):
    if apply_result.phase != "ready":
        return _publish_result("skipped", 0, errors=apply_result.errors)

    channel = _find_runtime_channel(runtime_config, apply_result.channel_id)
    if channel is None:
        return _publish_result("error", 0, errors=("switch_channel_not_found",))

    topic = mqtt_topic(runtime_config, _device_id(runtime_config), "meta", "patch")
    transport.publish(
        topic,
        _build_meta_patch_payload(
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
    return _publish_result("published", 1, topics=(topic,))


def _persist_switch_state(runtime_config, command, *, settings_root=None):
    if settings_root is None:
        return ()
    switch_channel = _find_runtime_channel(runtime_config, command.channel_id)
    if switch_channel is None:
        return ()
    try:
        from cpynodus_ii.core.settings import Settings

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
        return tuple(persistence_errors)
    except MemoryError:
        return ("switch_state_persist_memory",)


def _build_config_ack_payload(message_id, *, accepted=True, duplicate=False):
    return {
        "message_id": str(message_id or ""),
        "accepted": bool(accepted),
        "duplicate": bool(duplicate),
    }


def _build_config_result_payload(
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


def _build_switch_event_payload(runtime_config, channel, state, *, message_id=""):
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    return {
        "schema": "nodus-switch-event/v1",
        "device_id": runtime_config.switch.device_id,
        "channel_id": channel.channel_id,
        "label": channel.label,
        "state": "ON" if state else "OFF",
        "message_id": str(message_id or ""),
        "timestamp": timestamp,
    }


def _build_meta_patch_payload(runtime_config, *, source, message_id, updates):
    device_id = _device_id(runtime_config)
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
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    return {
        "schema": "nodus-meta-patch/v1",
        "device_id": device_id,
        "timestamp": timestamp,
        "source": str(source or ""),
        "message_id": str(message_id or ""),
        "sections": sections,
        "updates": normalized_updates,
    }


def _publish_result(phase, published_count, *, topics=(), errors=()):
    return _PublishResult(
        phase=phase,
        published_count=published_count,
        topics=tuple(topics),
        errors=tuple(errors),
    )


class _PublishResult:
    __slots__ = ("errors", "phase", "published_count", "topics")

    def __init__(self, *, phase, published_count, topics=(), errors=()):
        self.phase = phase
        self.published_count = int(published_count or 0)
        self.topics = tuple(topics)
        self.errors = tuple(errors)


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
    if not isinstance(payload, dict):
        return None
    nested = payload.get("payload")
    candidates = [
        payload.get("state"),
        payload.get("set"),
        nested.get("state") if isinstance(nested, dict) else None,
        nested.get("set") if isinstance(nested, dict) else None,
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
