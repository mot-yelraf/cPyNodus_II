"""Handle MQTT device configuration through a shallow scalar path.

The handler accepts one Sensorius update per command, keeps startup-sensitive
writes durable-before-success, and avoids loading the full settings machinery.
"""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic

_SETTINGS_KEYS = {
    "Network": ("SSID", "PASSWORD", "HOSTNAME", "HTTPPORT", "AP_CHANNEL"),
    "MQTT": (
        "BROKER",
        "BROKER_IP",
        "PORT",
        "USE_TLS",
        "BASE_TOPIC",
        "USERNAME",
        "PASSWORD",
    ),
    "Profile": ("ACTIVE_PROFILE",),
    "HomeAssistant": (
        "DISCOVERY_PREFIX",
        "BASE_TOPIC",
        "PUBLISH_DISCOVERY_RETAIN",
        "PUBLISH_STATE_RETAIN",
        "PUBLISH_LEGACY_SENSOR_TOPIC",
    ),
    "Time": ("TZ", "TZ_OFFSET", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP"),
}
_CAL_KEYS = (
    "TEMP_OFFSET",
    "RH_OFFSET",
    "CO2_OFFSET",
    "AQI_OFFSET",
    "GAS_OFFSET",
    "LUX_OFFSET",
    "PPFD_OFFSET",
    "APVPD_TEMP_CAL_VAL",
    "APVPD_RH_CAL_VAL",
    "ALTITUDE_METERS",
    "SOIL_TEMP_CAL_VAL",
    "SOIL_MOIST_CAL_VAL",
    "SOIL_TEMP_MOIST_VAL",
    "SOIL_PH_CAL_VAL",
    "SOIL_EC_CAL_VAL",
)


def process_device_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Parse, apply, persist, and publish scalar device configuration."""
    device_id = _device_id(runtime_config)
    if not device_id or topic != mqtt_topic(runtime_config, device_id, "config", "set"):
        return None
    parsed = _parse(payload_text)
    if parsed is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=0,
            errors=("schema_invalid",),
            runtime_config=runtime_config,
        )
    message_id, updates, token, restart, restart_mode = parsed
    token_error = _validate_token(settings_root, token)
    if token_error:
        return _failure(
            transport, runtime_config, topic, message_id, token_error, accepted=False
        )
    duplicate = _contains(handled_message_ids, message_id)
    _ack(transport, runtime_config, message_id, duplicate=duplicate)
    if duplicate:
        _result(
            transport,
            runtime_config,
            message_id,
            True,
            0,
            duplicate=True,
            restart=restart,
            restart_mode=restart_mode,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            runtime_config=runtime_config,
            message_id=message_id,
            duplicate=True,
            reboot_mode=restart_mode if restart else "",
        )
    if restart and not updates:
        _result(
            transport,
            runtime_config,
            message_id,
            True,
            0,
            restart=True,
            restart_mode=restart_mode,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            runtime_config=runtime_config,
            message_id=message_id,
            requested_state="restart:{}".format(restart_mode),
            reboot_requested=True,
            reboot_mode=restart_mode,
        )
    if len(updates) != 1:
        _result(
            transport,
            runtime_config,
            message_id,
            False,
            0,
            "single_update_required",
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=("single_update_required",),
            runtime_config=runtime_config,
            message_id=message_id,
        )
    normalized = []
    for update in updates:
        item = _normalize(runtime_config, update)
        if item is None:
            _result(transport, runtime_config, message_id, False, 0, "config_rejected")
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="config",
                published_count=2,
                errors=("config_rejected",),
                runtime_config=runtime_config,
                message_id=message_id,
            )
        normalized.append(item)
    live_first = True
    for item in normalized:
        if _restart_required(item):
            live_first = False
            break
    persistence_errors = ()
    if not live_first:
        persistence_errors = _persist_all(runtime_config, normalized, settings_root)
        if persistence_errors:
            error = persistence_errors[0]
            _result(transport, runtime_config, message_id, False, 0, error)
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="config",
                published_count=2,
                errors=persistence_errors,
                runtime_config=runtime_config,
                message_id=message_id,
                persistence_mode="failed",
            )
    _apply_all(runtime_config, normalized)
    _result(
        transport,
        runtime_config,
        message_id,
        True,
        len(normalized),
        restart=restart,
        restart_mode=restart_mode,
    )
    transport.publish(
        mqtt_topic(runtime_config, _device_id(runtime_config), "meta", "patch"),
        _meta(runtime_config, message_id, normalized),
        retain=False,
    )
    if live_first:
        persistence_errors = _persist_all(runtime_config, normalized, settings_root)
    if not persistence_errors:
        _clear_token(settings_root)
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="config",
        published_count=3,
        errors=persistence_errors,
        runtime_config=runtime_config,
        message_id=message_id,
        persistence_mode=("volatile" if persistence_errors else "persisted")
        if settings_root is not None
        else "",
        reboot_requested=restart,
        reboot_mode=restart_mode if restart else "",
        requested_state="restart:{}".format(restart_mode) if restart else "",
        ntp_resync_requested=_has_time(normalized),
    )


def _parse(payload_text):
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
    restart = _truthy(payload.get("restart", False))
    restart_mode = (
        "hard"
        if str(payload.get("restart_mode", "soft") or "").lower() == "hard"
        else "soft"
    )
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        return None
    updates = []
    if isinstance(body.get("updates"), list):
        for item in body["updates"]:
            if not isinstance(item, dict):
                return None
            updates.append(
                {
                    "section": str(item.get("section", "") or "").strip(),
                    "key": str(item.get("key", "") or "").strip(),
                    "value": item.get("value"),
                }
            )
    elif isinstance(body.get("settings"), dict):
        for section, values in body["settings"].items():
            if not isinstance(values, dict):
                return None
            for key, value in values.items():
                updates.append(
                    {"section": str(section), "key": str(key), "value": value}
                )
    elif not restart:
        return None
    if not updates and not restart:
        return None
    return (
        message_id,
        tuple(updates),
        str(payload.get("onboard_token", "") or "").strip(),
        restart,
        restart_mode,
    )


def _normalize(runtime_config, update):
    section = str(update.get("section", "") or "").strip()
    key = str(update.get("key", "") or "").strip().upper()
    value = update.get("value")
    if section in _SETTINGS_KEYS and key in _SETTINGS_KEYS[section]:
        return {"section": section, "key": key, "value": value, "file": "settings.toml"}
    if section == "Sensor" and key == "LOCATION":
        if not runtime_config.sensor.present and runtime_config.switch.present:
            return {
                "section": "Switch",
                "key": "SWITCH_LOCATION",
                "value": value,
                "file": "switch.toml",
            }
        return {
            "section": section,
            "key": key,
            "value": value,
            "file": _sensor_file(runtime_config),
        }
    if section == "Switch" and key in (
        "SWITCH_LOCATION",
        "SWITCH_1_LABEL",
        "SWITCH_2_LABEL",
        "SWITCH_1_LAST_STATE",
        "SWITCH_2_LAST_STATE",
    ):
        return {"section": section, "key": key, "value": value, "file": "switch.toml"}
    if section in ("Display", "Display.Style") and key.startswith("METRIC_"):
        try:
            index = int(key.split("_", 1)[1])
        except (ValueError, IndexError):
            return None
        if 1 <= index <= 6:
            return {
                "section": section,
                "key": key,
                "value": value,
                "file": _sensor_file(runtime_config),
            }
    if (
        section == "NPK"
        and key in ("N_TARGET", "P_TARGET", "K_TARGET")
        and runtime_config.sensor.device == "soil"
    ):
        return {
            "section": section,
            "key": key,
            "value": value,
            "file": _sensor_file(runtime_config),
        }
    if section in ("Calibration.System", "Calibration.Device") and key in _CAL_KEYS:
        if key == "SOIL_TEMP_MOIST_VAL":
            key = "SOIL_MOIST_CAL_VAL"
        if section == "Calibration.System" and key == "ALTITUDE_METERS":
            section = "Calibration.Device"
        return {
            "section": section,
            "key": key,
            "value": value,
            "file": _sensor_file(runtime_config),
        }
    return None


def _persist_all(runtime_config, updates, settings_root):
    if settings_root is None:
        return ()
    try:
        from cpynodus_ii.features.scalar_persistence import write_toml_scalar
    except MemoryError:
        return ("config_persist_import_memory",)
    for update in updates:
        value = update["value"]
        if update["key"] == "PASSWORD" and update["section"] in ("Network", "MQTT"):
            try:
                from cpynodus_ii.core.obfuscation import encode_password

                value = encode_password(value, hostname=runtime_config.network.hostname)
                update["public_value"] = value
            except MemoryError:
                return ("config_password_encode_memory",)
        try:
            errors = write_toml_scalar(
                _join(settings_root, update["file"]),
                update["section"],
                update["key"],
                value,
            )
        except MemoryError:
            return ("config_persist_memory",)
        except RuntimeError as exc:
            if "pystack exhausted" in str(exc).lower():
                return ("config_persist_pystack",)
            raise
        if errors:
            return tuple(errors)
    return ()


def _apply_all(runtime_config, updates):
    for update in updates:
        section = update["section"]
        key = update["key"]
        value = update["value"]
        if section == "Network":
            attrs = {
                "HOSTNAME": "hostname",
                "HTTPPORT": "http_port",
                "AP_CHANNEL": "ap_channel",
            }
            if key in attrs:
                setattr(
                    runtime_config.network,
                    attrs[key],
                    int(value) if key != "HOSTNAME" else str(value or "").strip(),
                )
        elif section == "MQTT":
            attrs = {
                "BROKER": "broker",
                "BROKER_IP": "broker_ip",
                "PORT": "port",
                "USE_TLS": "use_tls",
                "BASE_TOPIC": "base_topic",
                "USERNAME": "username",
            }
            if key in attrs:
                if key == "PORT":
                    value = int(value or 1883)
                elif key == "USE_TLS":
                    value = bool(value)
                else:
                    value = str(value or "").strip()
                setattr(runtime_config.mqtt, attrs[key], value)
        elif section == "Profile":
            runtime_config.active_profile = str(value or "").strip()
        elif section == "HomeAssistant":
            attrs = {
                "DISCOVERY_PREFIX": "discovery_prefix",
                "BASE_TOPIC": "base_topic",
                "PUBLISH_DISCOVERY_RETAIN": "publish_discovery_retain",
                "PUBLISH_STATE_RETAIN": "publish_state_retain",
                "PUBLISH_LEGACY_SENSOR_TOPIC": "publish_legacy_sensor_topic",
            }
            target = attrs[key]
            setattr(
                runtime_config.homeassistant,
                target,
                bool(value) if key.startswith("PUBLISH_") else str(value or "").strip(),
            )
        elif section == "Time":
            attr = {
                "TZ": "tz",
                "TZ_OFFSET": "tz_offset",
                "TZ_NAME": "tz_name",
                "NTP_SERVER": "ntp_server",
                "NTP_SERVER_IP": "ntp_server_ip",
            }[key]
            if key == "TZ_OFFSET":
                value = int(value or 0)
                if -14 <= value <= 14:
                    value *= 3600
            else:
                value = str(value or "").strip()
            setattr(runtime_config.time, attr, value)
        elif section == "Sensor" and key == "LOCATION":
            runtime_config.sensor.location = str(value or "").strip()
        elif section == "Switch" and key == "SWITCH_LOCATION":
            runtime_config.switch.location = str(value or "").strip()
        elif section == "Switch" and key.endswith("_LABEL"):
            channel = _channel(runtime_config, key[:8])
            if channel is not None:
                channel.label = str(value or "").strip()
        elif section in ("Display", "Display.Style"):
            index = int(key.split("_", 1)[1]) - 1
            values = list(
                runtime_config.sensor.display.metrics
                if section == "Display"
                else runtime_config.sensor.display.styles
            )
            values[index] = str(value or "").strip()
            if section == "Display":
                runtime_config.sensor.display.metrics = tuple(values)
            else:
                runtime_config.sensor.display.styles = tuple(values)
        elif section == "NPK":
            setattr(
                runtime_config.sensor.soil_npk,
                key[0].lower() + "_target",
                float(value or 0.0),
            )
        elif section.startswith("Calibration."):
            target = (
                runtime_config.sensor.calibration_system
                if section.endswith("System")
                else runtime_config.sensor.calibration_device
            )
            attrs = {
                "TEMP_OFFSET": "temp_offset",
                "RH_OFFSET": "rh_offset",
                "CO2_OFFSET": "co2_offset",
                "AQI_OFFSET": "aqi_offset",
                "GAS_OFFSET": "gas_offset",
                "LUX_OFFSET": "lux_offset",
                "PPFD_OFFSET": "ppfd_offset",
                "APVPD_TEMP_CAL_VAL": "apvpd_temp_cal_val",
                "APVPD_RH_CAL_VAL": "apvpd_rh_cal_val",
                "ALTITUDE_METERS": "altitude_meters",
                "SOIL_TEMP_CAL_VAL": "soil_temp_cal_val",
                "SOIL_MOIST_CAL_VAL": "soil_moist_cal_val",
                "SOIL_PH_CAL_VAL": "soil_ph_cal_val",
                "SOIL_EC_CAL_VAL": "soil_ec_cal_val",
            }
            setattr(target, attrs[key], float(value or 0.0))


def _restart_required(update):
    return update["section"] in ("Network", "MQTT", "Profile", "HomeAssistant")


def _failure(transport, runtime_config, topic, message_id, error, accepted=True):
    _ack(transport, runtime_config, message_id, accepted=accepted)
    _result(transport, runtime_config, message_id, False, 0, error)
    return CommandResult(
        phase="error",
        topic=topic,
        command_type="config",
        published_count=2,
        errors=(error,),
        runtime_config=runtime_config,
        message_id=message_id,
    )


def _ack(transport, runtime_config, message_id, accepted=True, duplicate=False):
    transport.publish(
        mqtt_topic(runtime_config, _device_id(runtime_config), "config", "ack"),
        {
            "message_id": message_id,
            "accepted": bool(accepted),
            "duplicate": bool(duplicate),
        },
        retain=False,
    )


def _result(
    transport,
    runtime_config,
    message_id,
    applied,
    updated,
    error="",
    duplicate=False,
    restart=False,
    restart_mode="soft",
):
    payload = {
        "message_id": message_id,
        "applied": bool(applied),
        "updated": int(updated),
        "duplicate": bool(duplicate),
        "error": str(error or ""),
    }
    if restart:
        payload["restart"] = True
        payload["restart_mode"] = restart_mode
    transport.publish(
        mqtt_topic(runtime_config, _device_id(runtime_config), "config", "result"),
        payload,
        retain=False,
    )


def _meta(runtime_config, message_id, updates):
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    clean = []
    sections = []
    for item in updates:
        if item["section"] not in sections:
            sections.append(item["section"])
        value = item.get("public_value", item["value"])
        if item["key"] == "PASSWORD" and item["section"] in ("Network", "MQTT"):
            try:
                from cpynodus_ii.core.obfuscation import encode_password

                value = encode_password(
                    value,
                    hostname=runtime_config.network.hostname,
                )
            except MemoryError:
                value = ""
        clean.append(
            {
                "section": item["section"],
                "key": item["key"],
                "value": value,
            }
        )
    return {
        "schema": "nodus-meta-patch/v1",
        "device_id": _device_id(runtime_config),
        "timestamp": timestamp,
        "source": "config_set",
        "message_id": message_id,
        "sections": sections,
        "updates": clean,
    }


def _validate_token(root, token):
    if root is None:
        return ""
    path = _join(root, "onboarding_state.json")
    try:
        import os

        if os.stat(path)[6] <= 0:
            return ""
        import json

        handle = open(path, "r")
        state = json.load(handle)
        handle.close()
    except (OSError, ValueError):
        return ""
    expected = (
        str(state.get("onboard_token", "") or "").strip()
        if isinstance(state, dict)
        else ""
    )
    return (
        ""
        if not expected or expected == str(token or "").strip()
        else "onboard_token_invalid"
    )


def _clear_token(root):
    if root is None:
        return
    try:
        import os

        os.remove(_join(root, "onboarding_state.json"))
    except OSError:
        pass


def _device_id(config):
    return config.sensor.sensor_id or config.switch.device_id or config.network.hostname


def _sensor_file(config):
    return str(config.sensor.active_config_file or "") or (
        "sensor_soil.toml" if config.sensor.family == "soil" else "sensor_i2c.toml"
    )


def _join(root, name):
    text = str(root or ".")
    return (
        "{}{}".format(text, name) if text.endswith("/") else "{}/{}".format(text, name)
    )


def _contains(items, value):
    for item in items or ():
        if item == value:
            return True
    return False


def _channel(config, key):
    for channel in config.switch.channels:
        if channel.key == key:
            return channel
    return None


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _has_time(updates):
    for item in updates:
        if item["section"] == "Time":
            return True
    return False
