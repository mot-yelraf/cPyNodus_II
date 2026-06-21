"""Low-stack device display config handling."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic

_DISPLAY_SECTIONS = ("Display", "Display.Style")


def process_device_display_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle simple Display.* config updates without the full config stack."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and runtime_config.sensor.present
        and topic == mqtt_topic(runtime_config, device_id, "config", "set")
    ):
        return None

    parsed = _parse_display_config(payload_text)
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

    updated_runtime_config = _display_runtime_config(runtime_config, updates)
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
    persistence_errors = _persist_display_updates_fast(
        updated_runtime_config,
        updates,
        settings_root=settings_root,
    )
    _clear_onboarding_state_fast(settings_root)
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="config",
        published_count=3,
        errors=persistence_errors,
        runtime_config=updated_runtime_config,
        message_id=message_id,
        persistence_mode=_persistence_mode(settings_root, persistence_errors),
    )


def _parse_display_config(payload_text):
    try:
        import json

        payload = json.loads(str(payload_text or "").strip())
    except (ImportError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if bool(payload.get("restart", False)):
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    if not message_id:
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        return None
    updates = _display_updates_from_body(body)
    if updates is None:
        return None
    onboard_token = str(payload.get("onboard_token", "") or "").strip()
    return message_id, tuple(updates), onboard_token


def _display_updates_from_body(body):
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
            section_text = str(section or "").strip()
            if section_text not in _DISPLAY_SECTIONS:
                return None
            if not isinstance(values, dict):
                return None
            for key, value in values.items():
                key_text = str(key or "").strip()
                if section_text == "Display" and key_text == "Style":
                    if not isinstance(value, dict):
                        return None
                    for style_key, style_value in value.items():
                        raw_updates.append(
                            (
                                "Display.Style",
                                str(style_key or "").strip().upper(),
                                style_value,
                            )
                        )
                    continue
                raw_updates.append((section_text, key_text.upper(), value))

    normalized = []
    seen = []
    for section, key, value in raw_updates:
        if section not in _DISPLAY_SECTIONS or not _display_metric_index(key):
            return None
        target = (section, key)
        if target in seen:
            return None
        seen.append(target)
        normalized.append(
            {
                "section": section,
                "key": key,
                "value": str(value or "").strip(),
            }
        )
    return normalized


def _display_runtime_config(runtime_config, updates):
    display = runtime_config.sensor.display
    metrics = _six_slots(getattr(display, "metrics", ()))
    styles = _six_slots(getattr(display, "styles", ()))
    for update in updates:
        key = str(update.get("key", "") or "").strip().upper()
        index = _display_metric_index(key)
        if not index:
            continue
        value = str(update.get("value", "") or "").strip()
        if update.get("section") == "Display":
            metrics[index - 1] = value
        elif update.get("section") == "Display.Style":
            styles[index - 1] = value
    display.metrics = tuple(metrics)
    display.styles = tuple(styles)
    return runtime_config


def _six_slots(values):
    slots = [str(value or "").strip() for value in tuple(values or ())]
    while len(slots) < 6:
        slots.append("")
    return slots[:6]


def _persist_display_updates_fast(runtime_config, updates, *, settings_root=None):
    if settings_root is None:
        return ()
    filename = _target_sensor_config_file(runtime_config)
    if not filename:
        return ("display_target_missing",)
    try:
        return _write_display_file(
            _join_settings_path(settings_root, filename),
            updates,
        )
    except MemoryError:
        return ("display_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("display_persist_pystack",)
        raise


def _target_sensor_config_file(runtime_config):
    filename = str(getattr(runtime_config.sensor, "active_config_file", "") or "")
    filename = filename.strip()
    if filename:
        return filename
    family = str(getattr(runtime_config.sensor, "family", "") or "").strip().lower()
    device = str(getattr(runtime_config.sensor, "device", "") or "").strip().lower()
    if family == "soil" or device == "soil":
        return "sensor_soil.toml"
    return "sensor_i2c.toml"


def _write_display_file(path, updates):
    import os

    targets = _display_patch_targets(updates)
    if not targets:
        return ("display_key_missing",)

    path_text = str(path or "")
    tmp_path = "{}.tmp".format(path_text)
    backup_path = "{}.bak".format(path_text)
    current_section = ""
    sections_seen = []
    found = [False] * len(targets)
    source = None
    target = None
    try:
        source = open(path_text, "r")
        target = open(tmp_path, "w")
        while True:
            raw_line = source.readline()
            if raw_line == "":
                break
            stripped = str(raw_line or "").strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if current_section in _DISPLAY_SECTIONS:
                    _append_missing_display_targets(
                        target,
                        current_section,
                        targets,
                        found,
                    )
                current_section = stripped[1:-1].strip()
                if (
                    current_section in _DISPLAY_SECTIONS
                    and current_section not in sections_seen
                ):
                    sections_seen.append(current_section)
                target.write(raw_line)
                continue
            line_key = ""
            if current_section in _DISPLAY_SECTIONS:
                line_body = str(raw_line or "").split("#", 1)[0]
                equal_index = line_body.find("=")
                if equal_index >= 0:
                    line_key = line_body[:equal_index].strip().upper()
            replacement = ""
            if current_section in _DISPLAY_SECTIONS and line_key:
                replacement = _replacement_display_line(
                    current_section,
                    line_key,
                    raw_line,
                    targets,
                    found,
                )
            target.write(replacement if replacement else raw_line)
        if current_section in _DISPLAY_SECTIONS:
            _append_missing_display_targets(target, current_section, targets, found)
        for section in _DISPLAY_SECTIONS:
            if _section_has_missing_targets(section, targets, found):
                if section not in sections_seen:
                    target.write("\n[{}]\n".format(section))
                    sections_seen.append(section)
                _append_missing_display_targets(target, section, targets, found)
        try:
            target.flush()
        except AttributeError:
            pass
        try:
            source.close()
        except AttributeError:
            pass
        source = None
        try:
            target.close()
        except AttributeError:
            pass
        target = None
        if not _all_found(found):
            _remove_tmp(tmp_path)
            return ("display_key_missing",)
        if _path_size(tmp_path) <= 0:
            _remove_tmp(tmp_path)
            return ("toml_write_empty_tmp",)
        try:
            os.stat(backup_path)
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.stat(path_text)
            os.rename(path_text, backup_path)
        except OSError:
            pass
        os.rename(tmp_path, path_text)
    except OSError as exc:
        if source is not None:
            try:
                source.close()
            except AttributeError:
                pass
        if target is not None:
            try:
                target.close()
            except AttributeError:
                pass
        _remove_tmp(tmp_path)
        return (_persistence_error(exc),)
    return ()


def _display_patch_targets(updates):
    targets = []
    seen = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip().upper()
        if section not in _DISPLAY_SECTIONS or not _display_metric_index(key):
            return ()
        target = (section, key)
        if target in seen:
            return ()
        seen.append(target)
        targets.append((section, key, _format_toml_scalar(update.get("value"))))
    return tuple(targets)


def _append_missing_display_targets(handle, section, targets, found):
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if target[0] != section:
            continue
        handle.write("{} = {}\n".format(target[1], target[2]))
        found[index] = True


def _section_has_missing_targets(section, targets, found):
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if target[0] == section:
            return True
    return False


def _replacement_display_line(section, key, raw_line, targets, found):
    key_upper = str(key or "").strip().upper()
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if section == target[0] and key_upper == target[1]:
            found[index] = True
            return "{} = {}{}".format(target[1], target[2], _line_ending(raw_line))
    return ""


def _display_metric_index(key_upper):
    if not key_upper.startswith("METRIC_"):
        return 0
    try:
        index = int(key_upper.split("_", 1)[1])
    except Exception:
        return 0
    if 1 <= index <= 6:
        return index
    return 0


def _all_found(found):
    for item in found:
        if not item:
            return False
    return True


def _format_toml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        text = repr(float(value))
        if "." not in text and "e" not in text.lower():
            text += ".0"
        return text
    text = str(value or "")
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def _line_ending(raw_line):
    text = str(raw_line or "")
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\n"):
        return "\n"
    return ""


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


def _validate_onboarding_token_fast(settings_root, onboard_token):
    if settings_root is None:
        return ""
    state_path = _join_settings_path(settings_root, "onboarding_state.json")
    if _path_size(state_path) <= 0:
        return ""
    handle = None
    try:
        import json

        handle = open(state_path, "r")
        state = json.load(handle)
    except Exception:
        return ""
    finally:
        if handle is not None:
            try:
                handle.close()
            except AttributeError:
                pass
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


def _persistence_mode(settings_root, errors):
    if settings_root is None:
        return ""
    return "volatile" if errors else "persisted"


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


def _path_size(path):
    try:
        import os

        return os.stat(path)[6]
    except OSError:
        return -1


def _remove_tmp(path):
    try:
        import os

        os.remove(path)
    except OSError:
        pass


def _persistence_error(exc):
    code = getattr(exc, "errno", None)
    if code in {30} or "read-only" in str(exc).lower():
        return "read_only_filesystem"
    return "persistence_failed"
