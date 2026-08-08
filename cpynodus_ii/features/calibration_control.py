"""Handle non-mutating calibration commands through a shallow MQTT path.

Status and soil-session control remain independent of the general command
handler so every calibration/set action has a bounded call stack.
"""

from cpynodus_ii.features.command_models import CommandResult, SoilPhCalibrationSession
from cpynodus_ii.features.topics import mqtt_topic


def process_calibration_control_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle calibration status and soil-session control without heavy handlers."""
    _ = settings_root
    device_id = _device_id(runtime_config)
    if not device_id or topic != mqtt_topic(
        runtime_config, device_id, "calibration", "set"
    ):
        return None
    try:
        import json

        payload = json.loads(str(payload_text or "").strip())
    except (ImportError, ValueError):
        return _schema_error(topic, runtime_config)
    if not isinstance(payload, dict):
        return _schema_error(topic, runtime_config)
    message_id = str(payload.get("message_id", "") or "").strip()
    action = str(payload.get("action", "") or "").strip().lower()
    if not (message_id and action):
        return _schema_error(topic, runtime_config)
    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")
    transport.publish(
        ack_topic, {"message_id": message_id, "accepted": True}, retain=False
    )
    if _contains(handled_message_ids, message_id):
        _publish_result(transport, result_topic, message_id, True)
        return _command_result(topic, runtime_config, message_id, 2, duplicate=True)
    if action == "status":
        status = _status(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status,
            retain=True,
        )
        _publish_result(transport, result_topic, message_id, True, status=status)
        return _command_result(topic, runtime_config, message_id, 3)
    if action == "soil_ph_session_start":
        if runtime_config.sensor.device != "soil":
            return _unsupported(
                transport, topic, result_topic, runtime_config, message_id
            )
        body = payload.get("payload") or {}
        reference = body.get("reference_ph") if isinstance(body, dict) else None
        if reference is None:
            _publish_result(
                transport,
                result_topic,
                message_id,
                False,
                error="missing_reference_ph",
            )
            return _command_result(
                topic,
                runtime_config,
                message_id,
                2,
                phase="error",
                errors=("missing_reference_ph",),
            )
        if getattr(transport, "_soil_ph_session", None) is not None:
            _publish_result(
                transport,
                result_topic,
                message_id,
                False,
                error="soil_calibration_already_running",
            )
            return _command_result(
                topic,
                runtime_config,
                message_id,
                2,
                phase="error",
                errors=("soil_calibration_already_running",),
            )
        interval = float(body.get("sample_interval_s", 10) or 10)
        count = max(6, min(18, int(body.get("sample_count", 12) or 12)))
        transport._soil_ph_session = SoilPhCalibrationSession(
            message_id=message_id,
            reference_ph=float(reference),
            sample_interval_s=interval,
            sample_count=count,
            started_at=0.0,
            next_sample_at=0.0,
            samples=(),
        )
        status = _status(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status,
            retain=True,
        )
        _publish_result(
            transport,
            result_topic,
            message_id,
            True,
            started=True,
            status=status,
            sample_interval_s=interval,
            sample_count=count,
            reference_ph=float(reference),
        )
        return _command_result(topic, runtime_config, message_id, 3)
    if action == "soil_ph_session_cancel":
        if getattr(transport, "_soil_ph_session", None) is None:
            _publish_result(
                transport,
                result_topic,
                message_id,
                False,
                error="soil_calibration_not_running",
            )
            return _command_result(
                topic,
                runtime_config,
                message_id,
                2,
                phase="error",
                errors=("soil_calibration_not_running",),
            )
        transport._soil_ph_session = None
        status = _status(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status,
            retain=True,
        )
        _publish_result(transport, result_topic, message_id, True, status=status)
        return _command_result(topic, runtime_config, message_id, 3)
    return _unsupported(transport, topic, result_topic, runtime_config, message_id)


def _status(runtime_config, transport):
    session = getattr(transport, "_soil_ph_session", None)
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    payload = {
        "schema": "nodus-calibration-status/v1",
        "status": "active" if session is not None else "idle",
        "calibrated": False,
        "sensor_id": runtime_config.sensor.sensor_id,
        "timestamp": timestamp,
    }
    if session is not None:
        payload.update(
            {
                "soil_calibration_active": True,
                "soil_calibration_message_id": session.message_id,
                "soil_calibration_reference_ph": float(session.reference_ph),
                "soil_calibration_sample_count": int(session.sample_count),
                "soil_calibration_samples_collected": len(tuple(session.samples or ())),
            }
        )
    return payload


def _publish_result(
    transport, topic, message_id, applied, updated=0, error="", **extra
):
    payload = {
        "message_id": message_id,
        "applied": bool(applied),
        "updated": int(updated),
        "error": str(error or ""),
    }
    payload.update(extra)
    transport.publish(topic, payload, retain=False)


def _unsupported(transport, topic, result_topic, config, message_id):
    _publish_result(
        transport,
        result_topic,
        message_id,
        False,
        error="calibration_not_supported",
    )
    return _command_result(
        topic,
        config,
        message_id,
        2,
        phase="error",
        errors=("calibration_not_supported",),
    )


def _schema_error(topic, config):
    return CommandResult(
        phase="error",
        topic=topic,
        command_type="calibration",
        published_count=0,
        errors=("schema_invalid",),
        runtime_config=config,
    )


def _command_result(
    topic, config, message_id, count, *, phase="published", errors=(), duplicate=False
):
    return CommandResult(
        phase=phase,
        topic=topic,
        command_type="calibration",
        published_count=count,
        errors=tuple(errors),
        runtime_config=config,
        message_id=message_id,
        duplicate=duplicate,
    )


def _contains(items, value):
    for item in items or ():
        if item == value:
            return True
    return False


def _device_id(config):
    return config.sensor.sensor_id or config.switch.device_id or config.network.hostname
