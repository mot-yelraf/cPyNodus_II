"""Low-stack device switch-label config handling."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.switch_location_config import (
    _all_found,
    _build_meta_patch_payload,
    _clear_onboarding_state_fast,
    _device_id,
    _format_toml_scalar,
    _join_settings_path,
    _line_ending,
    _path_size,
    _persistence_error,
    _persistence_mode,
    _publish_config_ack,
    _publish_config_result,
    _remove_tmp,
    _validate_onboarding_token_fast,
)
from cpynodus_ii.features.topics import mqtt_topic


def process_device_switch_label_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle simple Switch.SWITCH_N_LABEL updates without the full config stack."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and runtime_config.switch.present
        and topic == mqtt_topic(runtime_config, device_id, "config", "set")
    ):
        return None

    parsed = _parse_switch_label_config(runtime_config, payload_text)
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

    updated_runtime_config = _switch_label_runtime_config(runtime_config, updates)
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
    persistence_errors = _persist_switch_label_updates_fast(
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


def _parse_switch_label_config(runtime_config, payload_text):
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
    updates = _switch_label_updates_from_body(runtime_config, body)
    if updates is None:
        return None
    onboard_token = str(payload.get("onboard_token", "") or "").strip()
    return message_id, tuple(updates), onboard_token


def _switch_label_updates_from_body(runtime_config, body):
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
        values = settings.get("Switch")
        if not isinstance(values, dict) or len(settings) != 1:
            return None
        for key, value in values.items():
            raw_updates.append(("Switch", str(key or "").strip().upper(), value))

    normalized = []
    seen = []
    for section, key, value in raw_updates:
        if section != "Switch" or not _switch_label_key(runtime_config, key):
            return None
        target = (section, key)
        if target in seen:
            return None
        seen.append(target)
        normalized.append(
            {
                "section": "Switch",
                "key": key,
                "value": str(value or "").strip(),
            }
        )
    return normalized


def _switch_label_key(runtime_config, key):
    key_upper = str(key or "").strip().upper()
    if not (key_upper.startswith("SWITCH_") and key_upper.endswith("_LABEL")):
        return ""
    for channel in getattr(runtime_config.switch, "channels", ()):
        channel_key = str(getattr(channel, "key", "") or "").strip().upper()
        if key_upper == "{}_LABEL".format(channel_key):
            return key_upper
    return ""


def _switch_label_runtime_config(runtime_config, updates):
    for update in updates:
        key = str(update.get("key", "") or "").strip().upper()
        value = str(update.get("value", "") or "").strip()
        for channel in getattr(runtime_config.switch, "channels", ()):
            channel_key = str(getattr(channel, "key", "") or "").strip().upper()
            if key == "{}_LABEL".format(channel_key):
                channel.label = value
                break
    return runtime_config


def _persist_switch_label_updates_fast(updates, *, settings_root=None):
    if settings_root is None:
        return ()
    try:
        return _write_switch_label_file(
            _join_settings_path(settings_root, "switch.toml"),
            updates,
        )
    except MemoryError:
        return ("switch_label_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("switch_label_persist_pystack",)
        raise


def _write_switch_label_file(path, updates):
    import os

    targets = _switch_label_patch_targets(updates)
    if not targets:
        return ("switch_label_key_missing",)

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
                if current_section == "Switch":
                    _append_missing_switch_label_targets(target, targets, found)
                current_section = stripped[1:-1].strip()
                if current_section not in sections_seen:
                    sections_seen.append(current_section)
                target.write(raw_line)
                continue
            line_key = ""
            if current_section == "Switch":
                line_body = str(raw_line or "").split("#", 1)[0]
                equal_index = line_body.find("=")
                if equal_index >= 0:
                    line_key = line_body[:equal_index].strip().upper()
            replacement = ""
            if line_key:
                replacement = _replacement_switch_label_line(
                    line_key,
                    raw_line,
                    targets,
                    found,
                )
            target.write(replacement if replacement else raw_line)
        if current_section == "Switch":
            _append_missing_switch_label_targets(target, targets, found)
        elif "Switch" not in sections_seen:
            target.write("\n[Switch]\n")
            _append_missing_switch_label_targets(target, targets, found)
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
            return ("switch_label_key_missing",)
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


def _switch_label_patch_targets(updates):
    targets = []
    seen = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip().upper()
        if not (
            section == "Switch"
            and key.startswith("SWITCH_")
            and key.endswith("_LABEL")
        ):
            return ()
        target = ("Switch", key)
        if target in seen:
            return ()
        seen.append(target)
        targets.append((key, _format_toml_scalar(update.get("value"))))
    return tuple(targets)


def _append_missing_switch_label_targets(handle, targets, found):
    for index, target in enumerate(targets):
        if found[index]:
            continue
        handle.write("{} = {}\n".format(target[0], target[1]))
        found[index] = True


def _replacement_switch_label_line(key, raw_line, targets, found):
    key_upper = str(key or "").strip().upper()
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if key_upper == target[0]:
            found[index] = True
            return "{} = {}{}".format(target[0], target[1], _line_ending(raw_line))
    return ""
