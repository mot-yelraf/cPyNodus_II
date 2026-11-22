"""Validate and persist per-probe derived-soil calibration.

MQTT and the local calibration page share the same bounded validation and
persistence. The caller can publish the patch only after a durable save.
"""

import json
from math import isfinite
from time import time

from cpynodus_ii.core.soil_derived import SOIL_DERIVED_GROUPS
from cpynodus_ii.features.calibration_persistence import persist_calibration_updates
from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic

_OUTCOME_LIMIT = 64
_OUTCOME_UPDATED = 4
_OUTCOME_ERROR = 5


def save_soil_derived_group(
    config, channel, group, values, *, settings_root, message_id, source
):
    """Validate and persist one group, then install the prepared live settings."""
    error, overrides, patch = _prepare_soil_update(
        config.sensor, channel, group, values=values, source=source,
        message_id=message_id,
    )
    if error:
        return error, None
    if settings_root is None:
        return "settings_not_writable", None
    if patch["updates"]:
        errors = persist_calibration_updates(
            config, patch["updates"], settings_root=settings_root, require_commit=True
        )
        if errors:
            return _soil_persistence_error(errors), None
        config.sensor.soil_derived_overrides = overrides
    return "", patch


def _prepare_soil_update(
    sensor, channel, group, *, message_id, source, values=None, key=None, value=None
):
    if channel not in ("CH1", "CH2") or not sensor.modbus:
        return "soil_channel_not_found", None, None
    for probe in sensor.modbus.channels:
        if probe.name == channel:
            break
    else:
        return "soil_channel_not_found", None, None
    spec = SOIL_DERIVED_GROUPS.get(group) if type(group) is str else None
    if spec is None or (group == "NPK" and probe.variant.lower() != "soil_7in1"):
        return "unsupported_soil_group", None, None

    attribute, model, keys, fields = spec
    scalar = key is not None
    if scalar:
        if key not in keys:
            return "invalid_soil_derived_values", None, None
    elif not isinstance(values, dict) or len(values) != len(keys):
        return "invalid_soil_derived_values", None, None

    overrides = dict(sensor.soil_derived_overrides or ())
    shared = getattr(sensor, attribute) or model()
    probe_overrides = dict(overrides.get((channel, group), ()))
    numbers = []
    updates = []
    section = "{}.{}".format(group, channel)
    for field_key, field in zip(keys, fields):
        previous = probe_overrides.get(field, getattr(shared, field))
        if scalar:
            raw = value if key == field_key else previous
        else:
            raw = values.get(field_key)
        if type(raw) not in (int, float):
            return "invalid_soil_derived_values", None, None
        try:
            number = float(raw)
        except (OverflowError, ValueError):
            return "invalid_soil_derived_values", None, None
        if not isfinite(number):
            return "invalid_soil_derived_values", None, None
        numbers.append(number)
        if (scalar and key == field_key) or (not scalar and number != previous):
            probe_overrides[field] = number
            updates.append(
                {
                    "sensor_id": sensor.sensor_id,
                    "name": "sensor_soil.toml",
                    "section": section,
                    "key": field_key,
                    "value": number,
                }
            )

    if group == "SoilDeficit":
        wet, dry = numbers
        valid = 0 <= dry < wet <= 100
    elif group == "SoilStress":
        low_crit, low_ok, high_ok, high_crit, moisture, temp = numbers
        valid = (
            low_crit < low_ok <= high_ok < high_crit
            and 0 <= moisture <= 100
            and 0 <= temp <= 100
            and moisture + temp > 0
        )
    else:
        n_target, p_target, k_target, weight = numbers
        valid = min(n_target, p_target, k_target) > 0 and 0 <= weight <= 100
    if not valid:
        return "invalid_soil_derived_values", None, None

    overrides[channel, group] = probe_overrides
    patch = {
        "schema": "nodus-meta-patch/v1",
        "device_id": sensor.sensor_id,
        "message_id": message_id,
        "timestamp": int(time()),
        "source": source,
        "sections": [section],
        "updates": updates,
    }
    return "", overrides, patch


def _soil_persistence_error(errors):
    if errors[0] == "read_only_filesystem":
        return "settings_not_writable"
    return "soil_derived_persistence_failed"


def process_soil_derived_message(
    transport, config, *, topic, payload_text, settings_root=None
):
    """Acknowledge a derived calibration command and replay recent outcomes."""
    try:
        command = json.loads(payload_text)
    except ValueError:
        return None
    if not isinstance(command, dict) or command.get("action") != "soil_derived_apply":
        return None
    message_id = command.get("message_id")
    if type(message_id) is not str or not message_id.strip():
        return CommandResult(
            phase="ignored",
            topic=topic,
            command_type="soil_derived_calibration",
            published_count=0,
            errors=("invalid_message_id",),
            runtime_config=config,
        )

    sensor = config.sensor
    device_id = sensor.sensor_id or config.switch.device_id or config.network.hostname
    ack_topic = mqtt_topic(config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(config, device_id, "calibration", "result")
    patch_topic = mqtt_topic(config, device_id, "meta", "patch")
    transport.publish(
        ack_topic, {"message_id": message_id, "accepted": True}, retain=False
    )
    outcomes = getattr(transport, "_soil_derived_outcomes", None)
    if outcomes is None:
        outcomes = []
        transport._soil_derived_outcomes = outcomes
    for previous_id, outcome in outcomes:
        if previous_id == message_id:
            duplicate = True
            break
    else:
        duplicate = False
    patch = None
    if not duplicate:
        payload = command.get("payload")
        sensor_id = command.get("sensor_id")
        name = command.get("name")
        del command
        reply_sensor = sensor.sensor_id if sensor_id == sensor.sensor_id else ""
        reply_name = "sensor_soil.toml" if name == "sensor_soil.toml" else ""
        # Reserve the replay record before any write, including for rejections.
        outcome = [reply_sensor, reply_name, "", "", 0, "invalid_soil_derived_target"]
        if len(outcomes) == _OUTCOME_LIMIT:
            del outcomes[0]
        outcomes.append((message_id, outcome))
        if (
            sensor.family == "soil"
            and sensor.sensor_id
            and sensor_id == sensor.sensor_id
            and name == sensor.active_config_file == "sensor_soil.toml"
        ):
            try:
                error, overrides, candidate_patch = _prepare_soil_mqtt_update(
                    sensor, payload, message_id, outcome,
                )
                del payload
                if not error:
                    if settings_root is None:
                        error = "settings_not_writable"
                    else:
                        errors = persist_calibration_updates(
                            config, candidate_patch["updates"],
                            settings_root=settings_root, require_commit=True,
                        )
                        if errors:
                            error = _soil_persistence_error(errors)
                if not error:
                    sensor.soil_derived_overrides = overrides
                    patch = candidate_patch
                    outcome[_OUTCOME_UPDATED] = 1
                outcome[_OUTCOME_ERROR] = error
            except MemoryError:
                outcome[_OUTCOME_ERROR] = "calibration_handler_memory"
            except RuntimeError as exc:
                if "pystack exhausted" not in str(exc).lower():
                    raise
                outcome[_OUTCOME_ERROR] = "calibration_handler_pystack"
    reply_sensor, reply_name, section, key, updated, error = outcome
    persistence_mode = "persisted" if updated else ""
    result = {
        "message_id": message_id,
        "sensor_id": reply_sensor,
        "name": reply_name,
        "section": section,
        "key": key,
        "applied": bool(updated),
        "persistence_mode": persistence_mode,
        "updated": updated,
        "error": error,
    }
    transport.publish(result_topic, result, retain=False)
    if patch is not None:
        transport.publish(patch_topic, patch, retain=False)
    return CommandResult(
        phase="error" if error else "published",
        topic=topic,
        command_type="soil_derived_calibration",
        published_count=3 if patch is not None else 2,
        errors=(error,) if error else (),
        runtime_config=config,
        message_id=message_id,
        duplicate=duplicate,
        persistence_mode=persistence_mode,
    )


def _prepare_soil_mqtt_update(sensor, payload, message_id, outcome):
    if (
        not isinstance(payload, dict)
        or len(payload) != 3
        or "value" not in payload
        or type(payload.get("section")) is not str
        or type(payload.get("key")) is not str
    ):
        return "invalid_soil_derived_values", None, None
    section = payload["section"]
    key = payload["key"]
    group, _, channel = section.partition(".")
    spec = SOIL_DERIVED_GROUPS.get(group)
    if spec is not None and channel in ("CH1", "CH2"):
        outcome[2] = section
        if key in spec[2]:
            outcome[3] = key
    return _prepare_soil_update(
        sensor, channel, group, key=key, value=payload["value"],
        message_id=message_id, source="calibration_set",
    )
