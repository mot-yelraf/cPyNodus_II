"""Low-stack calibration apply handling for sensor offset updates."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic


def process_calibration_apply_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle simple calibration apply payloads without the full command stack."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and runtime_config.sensor.present
        and topic == mqtt_topic(runtime_config, device_id, "calibration", "set")
    ):
        return None

    parsed = _parse_calibration_apply(payload_text)
    if parsed is None:
        return None
    message_id, updates = parsed
    if not updates:
        return None

    duplicate = bool(message_id and message_id in tuple(handled_message_ids or ()))
    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")
    _publish_calibration_ack(transport, ack_topic, message_id)

    if duplicate:
        _publish_calibration_result(
            transport,
            result_topic,
            message_id,
            applied=True,
            updated=0,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=message_id,
            duplicate=True,
        )

    persistence_errors = _persist_calibration_updates_fast(
        runtime_config,
        updates,
        settings_root=settings_root,
    )
    if persistence_errors:
        _publish_calibration_result(
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
            command_type="calibration",
            published_count=2,
            errors=persistence_errors,
            runtime_config=runtime_config,
            message_id=message_id,
            persistence_mode="volatile",
        )

    updated_runtime_config = _calibration_runtime_config(runtime_config, updates)
    _publish_calibration_result(
        transport,
        result_topic,
        message_id,
        applied=True,
        updated=len(updates),
    )
    transport.publish(
        mqtt_topic(updated_runtime_config, device_id, "meta", "patch"),
        _build_meta_patch_payload(
            updated_runtime_config,
            source="calibration_set",
            message_id=message_id,
            updates=updates,
        ),
        retain=False,
    )
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="calibration",
        published_count=3,
        errors=(),
        runtime_config=updated_runtime_config,
        message_id=message_id,
        persistence_mode="persisted" if settings_root is not None else "",
    )


def _parse_calibration_apply(payload_text):
    try:
        import json

        payload = json.loads(str(payload_text or "").strip())
    except (ImportError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    action = str(payload.get("action", "") or "").strip().lower()
    if not message_id or action not in {"apply", "set", "update"}:
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        return None
    updates = _calibration_updates_from_body(body)
    if updates is None:
        return None
    return message_id, tuple(updates)


def _calibration_updates_from_body(body):
    if isinstance(body.get("offsets"), list):
        updates = []
        for item in body["offsets"]:
            if not isinstance(item, dict):
                continue
            section, key = _normalize_calibration_key(item.get("key"))
            if not _supported_calibration_key(section, key):
                return None
            updates.append(
                {
                    "section": section,
                    "key": key,
                    "value": item.get("value"),
                }
            )
        return updates

    calibration = body.get("calibration")
    if isinstance(calibration, dict):
        updates = []
        for branch, values in calibration.items():
            section = _calibration_section_for_branch(branch)
            if not section or not isinstance(values, dict):
                return None
            for key, value in values.items():
                key = str(key or "").strip().upper()
                if not _supported_calibration_key(section, key):
                    return None
                updates.append({"section": section, "key": key, "value": value})
        return updates
    return None


def _persist_calibration_updates_fast(runtime_config, updates, *, settings_root=None):
    if settings_root is None:
        return ()
    filename = str(runtime_config.sensor.active_config_file or "").strip()
    if not filename:
        filename = "sensor_soil.toml" if runtime_config.sensor.family == "soil" else ""
    if not filename:
        filename = "sensor_i2c.toml"
    try:
        return _write_calibration_file(
            _join_settings_path(settings_root, filename),
            updates,
        )
    except MemoryError:
        return ("calibration_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("pystack_exhausted",)
        raise


def _write_calibration_file(path, updates):
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
        return ("calibration_key_missing",)

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


def _calibration_runtime_config(runtime_config, updates):
    for update in updates:
        section = update["section"]
        attr = _calibration_attr_name(section, update["key"])
        if not attr:
            continue
        value = float(update.get("value") or 0.0)
        if section == "Calibration.System":
            setattr(runtime_config.sensor.calibration_system, attr, value)
        elif section == "Calibration.Device":
            setattr(runtime_config.sensor.calibration_device, attr, value)
    return runtime_config


def _normalize_calibration_key(key):
    text = str(key or "").strip()
    if text == "soil_ph_offset":
        return "Calibration.Device", "SOIL_PH_CAL_VAL"
    parts = text.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-1]), parts[-1].upper()
    return "", ""


def _calibration_section_for_branch(branch):
    normalized = str(branch or "").strip().lower()
    if normalized == "system":
        return "Calibration.System"
    if normalized == "device":
        return "Calibration.Device"
    return ""


def _supported_calibration_key(section, key):
    return bool(_calibration_attr_name(section, key))


def _calibration_attr_name(section, key):
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
        "ALTITUDE_METERS": "altitude_meters",
    }
    return mapping.get(str(key or "").strip().upper(), "")


def _publish_calibration_ack(transport, topic, message_id, *, accepted=True):
    transport.publish(
        topic,
        {
            "message_id": str(message_id or ""),
            "accepted": bool(accepted),
        },
        retain=False,
    )


def _publish_calibration_result(
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
