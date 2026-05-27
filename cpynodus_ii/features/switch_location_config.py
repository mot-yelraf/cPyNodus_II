"""Low-stack device location config handling."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic


def process_device_location_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle simple location updates without the full config stack."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and topic == mqtt_topic(runtime_config, device_id, "config", "set")
    ):
        return None

    parsed = _parse_location_config(runtime_config, payload_text)
    if parsed is None:
        return None
    message_id, updates, onboard_token = parsed
    if not updates:
        return None

    duplicate = bool(message_id and message_id in tuple(handled_message_ids or ()))
    ack_topic = mqtt_topic(runtime_config, device_id, "config", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "config", "result")

    token_error = _validate_onboarding_token_fast(settings_root, onboard_token)
    if token_error:
        _publish_config_ack(transport, ack_topic, message_id, accepted=False)
        _publish_config_result(
            transport,
            result_topic,
            message_id,
            applied=False,
            updated=0,
            error=token_error,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(token_error,),
            runtime_config=runtime_config,
            message_id=message_id,
        )

    _publish_config_ack(transport, ack_topic, message_id, duplicate=duplicate)
    if duplicate:
        _publish_config_result(
            transport,
            result_topic,
            message_id,
            applied=True,
            updated=0,
            duplicate=True,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=message_id,
            duplicate=True,
        )

    persistence_errors = _persist_location_updates_fast(
        runtime_config,
        updates,
        settings_root=settings_root,
    )
    if persistence_errors:
        _publish_config_result(
            transport,
            result_topic,
            message_id,
            applied=False,
            updated=0,
            error=persistence_errors[0],
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=persistence_errors,
            runtime_config=runtime_config,
            message_id=message_id,
            persistence_mode="volatile",
        )

    updated_runtime_config = _location_runtime_config(runtime_config, updates)
    _publish_config_result(
        transport,
        result_topic,
        message_id,
        applied=True,
        updated=len(updates),
    )
    transport.publish(
        mqtt_topic(
            updated_runtime_config,
            _device_id(updated_runtime_config),
            "meta",
            "patch",
        ),
        _build_meta_patch_payload(
            updated_runtime_config,
            source="config_set",
            message_id=message_id,
            updates=updates,
        ),
        retain=False,
    )
    _clear_onboarding_state_fast(settings_root)
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="config",
        published_count=3,
        errors=(),
        runtime_config=updated_runtime_config,
        message_id=message_id,
        persistence_mode="persisted" if settings_root is not None else "",
    )


def process_switch_location_config_message(*args, **kwargs):
    """Compatibility wrapper for switch-only location updates."""
    return process_device_location_config_message(*args, **kwargs)


def _parse_location_config(runtime_config, payload_text):
    try:
        import json

        payload = json.loads(str(payload_text or "").strip())
    except (ImportError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    if not message_id:
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        return None
    updates = _location_updates_from_body(runtime_config, body)
    if updates is None:
        return None
    onboard_token = str(payload.get("onboard_token", "") or "").strip()
    return message_id, tuple(updates), onboard_token


def _location_updates_from_body(runtime_config, body):
    raw_updates = []
    updates = body.get("updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            raw_updates.append(
                (
                    str(item.get("section", "") or "").strip(),
                    str(item.get("key", "") or "").strip().upper(),
                    item.get("value"),
                )
            )
    else:
        settings = body.get("settings")
        if not isinstance(settings, dict):
            return None
        for section, values in settings.items():
            if not isinstance(values, dict):
                return None
            for key, value in values.items():
                raw_updates.append(
                    (
                        str(section or "").strip(),
                        str(key or "").strip().upper(),
                        value,
                    )
                )

    normalized = []
    seen = set()
    for section, key, value in raw_updates:
        update = _normalize_location_update(runtime_config, section, key, value)
        if update is None:
            return None
        target = (update["section"], update["key"])
        if target in seen:
            return None
        seen.add(target)
        normalized.append(update)
    return normalized


def _normalize_location_update(runtime_config, section, key, value):
    if section == "Sensor" and key == "LOCATION":
        if runtime_config.sensor.present:
            return {
                "section": "Sensor",
                "key": "LOCATION",
                "value": str(value or "").strip(),
            }
        if runtime_config.switch.present:
            return {
                "section": "Switch",
                "key": "SWITCH_LOCATION",
                "value": str(value or "").strip(),
            }
    if (
        section == "Switch"
        and key == "SWITCH_LOCATION"
        and runtime_config.switch.present
    ):
        return {
            "section": "Switch",
            "key": "SWITCH_LOCATION",
            "value": str(value or "").strip(),
        }
    return None


def _persist_location_updates_fast(runtime_config, updates, *, settings_root=None):
    if settings_root is None:
        return ()
    grouped = {}
    for update in updates:
        filename = _target_file_for_location_update(runtime_config, update)
        if not filename:
            return ("location_target_missing",)
        grouped.setdefault(filename, []).append(update)
    try:
        for filename, file_updates in grouped.items():
            errors = _write_location_file(
                _join_settings_path(settings_root, filename),
                file_updates,
            )
            if errors:
                return errors
    except MemoryError:
        return ("location_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("pystack_exhausted",)
        raise
    return ()


def _target_file_for_location_update(runtime_config, update):
    if update["section"] == "Switch":
        return "switch.toml"
    if update["section"] == "Sensor":
        filename = str(runtime_config.sensor.active_config_file or "").strip()
        if filename:
            return filename
        if runtime_config.sensor.family == "soil":
            return "sensor_soil.toml"
        return "sensor_i2c.toml"
    return ""


def _write_location_file(path, updates):
    import os

    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return (_persistence_error(exc),)

    formatted_by_target = {}
    for update in updates:
        target = (update["section"], update["key"])
        formatted_by_target[target] = _format_toml_scalar(update.get("value"))
    lines = str(text or "").splitlines()
    trailing_newline = str(text or "").endswith("\n")
    current_section = ""
    found = set()
    for index, raw_line in enumerate(lines):
        stripped = str(raw_line or "").strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            continue
        if "=" not in raw_line:
            continue
        key = raw_line.split("=", 1)[0].strip()
        target = (current_section, key)
        if target not in formatted_by_target:
            continue
        lines[index] = "{} = {}".format(key, formatted_by_target[target])
        found.add(target)
    if len(found) != len(formatted_by_target):
        return ("location_key_missing",)

    patched = "\n".join(lines)
    if trailing_newline:
        patched += "\n"
    tmp_path = "{}.tmp".format(path)
    backup_path = "{}.bak".format(path)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(patched)
            try:
                handle.flush()
            except AttributeError:
                pass
        if _path_size(tmp_path) <= 0:
            return ("toml_write_empty_tmp",)
        if _path_exists(backup_path):
            os.remove(backup_path)
        if _path_exists(path):
            os.rename(path, backup_path)
        os.rename(tmp_path, path)
    except OSError as exc:
        return (_persistence_error(exc),)
    return ()


def _location_runtime_config(runtime_config, updates):
    for update in updates:
        if update["section"] == "Sensor":
            runtime_config.sensor.location = str(update.get("value", "") or "").strip()
        elif update["section"] == "Switch":
            runtime_config.switch.location = str(update.get("value", "") or "").strip()
    return runtime_config


def _publish_config_ack(
    transport,
    topic,
    message_id,
    *,
    accepted=True,
    duplicate=False,
):
    transport.publish(
        topic,
        {
            "message_id": str(message_id or ""),
            "accepted": bool(accepted),
            "duplicate": bool(duplicate),
        },
        retain=False,
    )


def _publish_config_result(
    transport,
    topic,
    message_id,
    *,
    applied,
    updated=0,
    error="",
    duplicate=False,
):
    transport.publish(
        topic,
        {
            "message_id": str(message_id or ""),
            "applied": bool(applied),
            "updated": int(updated or 0),
            "duplicate": bool(duplicate),
            "error": str(error or ""),
        },
        retain=False,
    )


def _build_meta_patch_payload(runtime_config, *, source, message_id, updates):
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
        "device_id": _device_id(runtime_config),
        "timestamp": timestamp,
        "source": str(source or ""),
        "message_id": str(message_id or ""),
        "sections": sections,
        "updates": normalized_updates,
    }


def _format_toml_scalar(value):
    text = str(value or "")
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def _validate_onboarding_token_fast(settings_root, onboard_token):
    if settings_root is None:
        return ""
    state_path = _join_settings_path(settings_root, "onboarding_state.json")
    if _path_size(state_path) <= 0:
        return ""
    try:
        import json

        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        return ""
    if not isinstance(state, dict):
        return ""
    expected = str(state.get("onboard_token", "") or "").strip()
    if not expected:
        return ""
    if str(onboard_token or "").strip() == expected:
        return ""
    return "onboard_token_invalid"


def _clear_onboarding_state_fast(settings_root):
    if settings_root is None:
        return False
    path = _join_settings_path(settings_root, "onboarding_state.json")
    try:
        import os

        os.remove(path)
    except OSError:
        return False
    return True


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _join_settings_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


def _path_exists(path):
    try:
        import os

        os.stat(path)
        return True
    except OSError:
        return False


def _path_size(path):
    try:
        import os

        return os.stat(path)[6]
    except OSError:
        return -1


def _persistence_error(exc):
    code = getattr(exc, "errno", None)
    if code in {30} or "read-only" in str(exc).lower():
        return "read_only_filesystem"
    return "persistence_failed"
