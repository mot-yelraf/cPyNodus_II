"""Transfer bounded device log files over MQTT in small chunks."""

import binascii
import json
import os
from dataclasses import dataclass, replace
from time import time

from cpynodus_ii.features.payloads import mqtt_topic

LOG_TRANSFER_SCHEMA = "nodus-log-transfer/v1"
DEFAULT_LOG_CHUNK_SIZE = 512
MAX_LOG_CHUNK_SIZE = 1024
ALLOWED_LOG_FILES = ("_reboot.log", "_recovery.log")


@dataclass(frozen=True)
class LogTransferCommand:
    """Describe one MQTT log retrieval command."""

    message_id: str
    filename: str = ""
    chunk_size: int = DEFAULT_LOG_CHUNK_SIZE


@dataclass(frozen=True)
class LogTransferSession:
    """Track a log transfer across steady-state loop iterations."""

    message_id: str
    filename: str
    path: str
    size: int
    chunk_size: int
    offset: int = 0
    sequence: int = 0
    file_checksum: int = 2166136261


def parse_log_transfer_command(payload_text):
    """Parse one MQTT log retrieval command payload."""
    try:
        payload = json.loads(str(payload_text or "").strip())
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        body = {}
    message_id = str(payload.get("message_id", "") or "").strip()
    filename = str(payload.get("filename", body.get("filename", "")) or "").strip()
    try:
        chunk_size = int(payload.get("chunk_size", body.get("chunk_size", 0)) or 0)
    except (TypeError, ValueError):
        chunk_size = 0
    if not message_id:
        return None
    return LogTransferCommand(
        message_id=message_id,
        filename=filename,
        chunk_size=_normalize_chunk_size(chunk_size),
    )


def subscribe_log_transfer_topics(transport, runtime_config):
    """Subscribe to the device log retrieval command topic."""
    device_id = _device_id(runtime_config)
    if not device_id:
        return ()
    return (transport.subscribe(mqtt_topic(runtime_config, device_id, "logs", "get")),)


def process_log_transfer_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse and accept one MQTT log retrieval command."""
    if not str(payload_text or "").strip():
        return _result("ignored", topic, 0, runtime_config=runtime_config)

    command = parse_log_transfer_command(payload_text)
    device_id = _device_id(runtime_config)
    ack_topic = mqtt_topic(runtime_config, device_id, "logs", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "logs", "result")
    if command is None:
        transport.publish(
            ack_topic,
            _ack_payload("", accepted=False, error="schema_invalid"),
            retain=False,
        )
        transport.publish(
            result_topic,
            _result_payload("", complete=False, error="schema_invalid"),
            retain=False,
        )
        return _result(
            "error",
            topic,
            2,
            errors=("schema_invalid",),
            runtime_config=runtime_config,
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())
    if duplicate:
        transport.publish(
            ack_topic,
            _ack_payload(command.message_id, accepted=True, duplicate=True),
            retain=False,
        )
        transport.publish(
            result_topic,
            _result_payload(command.message_id, complete=True, duplicate=True),
            retain=False,
        )
        return _result(
            "published",
            topic,
            2,
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
        )

    session, error = _build_session(command, settings_root=settings_root)
    if error:
        transport.publish(
            ack_topic,
            _ack_payload(command.message_id, accepted=False, error=error),
            retain=False,
        )
        transport.publish(
            result_topic,
            _result_payload(
                command.message_id,
                complete=False,
                filename=_safe_filename(command.filename),
                error=error,
            ),
            retain=False,
        )
        return _result(
            "error",
            topic,
            2,
            errors=(error,),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    _set_log_transfer_session(transport, session)
    transport.publish(
        ack_topic,
        _ack_payload(
            command.message_id,
            accepted=True,
            filename=session.filename,
            size=session.size,
            chunk_size=session.chunk_size,
        ),
        retain=False,
    )
    return _result(
        "published",
        topic,
        1,
        runtime_config=runtime_config,
        message_id=command.message_id,
    )


def process_log_transfer_session(transport, runtime_config):
    """Publish the next chunk for an active log transfer session."""
    session = _current_log_transfer_session(transport)
    if session is None:
        return _result("ignored", "", 0, runtime_config=runtime_config)
    if not transport.connected:
        return _result(
            "ignored",
            "",
            0,
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    device_id = _device_id(runtime_config)
    chunk_topic = mqtt_topic(runtime_config, device_id, "logs", "chunk")
    result_topic = mqtt_topic(runtime_config, device_id, "logs", "result")

    try:
        with open(session.path, "rb") as handle:
            handle.seek(int(session.offset))
            chunk = handle.read(int(session.chunk_size))
    except OSError:
        _set_log_transfer_session(transport, None)
        transport.publish(
            result_topic,
            _result_payload(
                session.message_id,
                complete=False,
                filename=session.filename,
                error="log_read_failed",
            ),
            retain=False,
        )
        return _result(
            "error",
            result_topic,
            1,
            errors=("log_read_failed",),
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    if not chunk:
        _set_log_transfer_session(transport, None)
        transport.publish(
            result_topic,
            _result_payload(
                session.message_id,
                complete=True,
                filename=session.filename,
                size=session.size,
                chunks=session.sequence,
                checksum=_checksum_hex(session.file_checksum),
            ),
            retain=False,
        )
        return _result(
            "published",
            result_topic,
            1,
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    next_offset = int(session.offset) + len(chunk)
    chunk_checksum = _checksum32(chunk)
    file_checksum = _checksum32(chunk, seed=session.file_checksum)
    encoded = binascii.b2a_base64(chunk).decode("ascii").strip()
    transport.publish(
        chunk_topic,
        {
            "schema": LOG_TRANSFER_SCHEMA,
            "message_id": session.message_id,
            "filename": session.filename,
            "sequence": int(session.sequence),
            "offset": int(session.offset),
            "next_offset": next_offset,
            "size": int(session.size),
            "encoding": "base64",
            "data": encoded,
            "checksum": _checksum_hex(chunk_checksum),
            "timestamp": int(time()),
        },
        retain=False,
    )
    _set_log_transfer_session(
        transport,
        replace(
            session,
            offset=next_offset,
            sequence=int(session.sequence) + 1,
            file_checksum=file_checksum,
        ),
    )
    return _result(
        "published",
        chunk_topic,
        1,
        runtime_config=runtime_config,
        message_id=session.message_id,
    )


def _build_session(command, *, settings_root=None):
    filename = _safe_filename(command.filename)
    if not filename:
        return None, "log_file_invalid"
    root = str(settings_root or "/").rstrip("/") or "/"
    path = "/{}".format(filename) if root == "/" else "{}/{}".format(root, filename)
    try:
        stat = os.stat(path)
    except OSError:
        return None, "log_file_missing"
    try:
        size = int(stat[6])
    except (TypeError, ValueError, IndexError):
        size = 0
    return (
        LogTransferSession(
            message_id=command.message_id,
            filename=filename,
            path=path,
            size=size,
            chunk_size=command.chunk_size,
        ),
        "",
    )


def _safe_filename(filename):
    requested = str(filename or "").strip()
    if not requested:
        requested = ALLOWED_LOG_FILES[0]
    requested = requested.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if requested not in ALLOWED_LOG_FILES:
        return ""
    return requested


def _normalize_chunk_size(chunk_size):
    if int(chunk_size or 0) <= 0:
        return DEFAULT_LOG_CHUNK_SIZE
    return min(MAX_LOG_CHUNK_SIZE, max(1, int(chunk_size)))


def _ack_payload(
    message_id,
    *,
    accepted,
    duplicate=False,
    filename="",
    size=0,
    chunk_size=0,
    error="",
):
    payload = {
        "schema": LOG_TRANSFER_SCHEMA,
        "message_id": str(message_id or ""),
        "accepted": bool(accepted),
        "duplicate": bool(duplicate),
        "timestamp": int(time()),
    }
    if filename:
        payload["filename"] = filename
    if size:
        payload["size"] = int(size)
    if chunk_size:
        payload["chunk_size"] = int(chunk_size)
    if error:
        payload["error"] = str(error)
    return payload


def _result_payload(
    message_id,
    *,
    complete,
    filename="",
    size=0,
    chunks=0,
    checksum="",
    error="",
    duplicate=False,
):
    payload = {
        "schema": LOG_TRANSFER_SCHEMA,
        "message_id": str(message_id or ""),
        "complete": bool(complete),
        "duplicate": bool(duplicate),
        "timestamp": int(time()),
    }
    if filename:
        payload["filename"] = filename
    if size:
        payload["size"] = int(size)
    if chunks:
        payload["chunks"] = int(chunks)
    if checksum:
        payload["checksum"] = str(checksum)
    if error:
        payload["error"] = str(error)
    return payload


def _checksum32(data, seed=2166136261):
    checksum = int(seed) & 0xFFFFFFFF
    for value in data or b"":
        checksum ^= int(value)
        checksum = (checksum * 16777619) & 0xFFFFFFFF
    return checksum


def _checksum_hex(value):
    return "{:08x}".format(int(value or 0) & 0xFFFFFFFF)


def _current_log_transfer_session(transport):
    return getattr(transport, "_log_transfer_session", None)


def _set_log_transfer_session(transport, session):
    setattr(transport, "_log_transfer_session", session)


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _result(
    phase,
    topic,
    published_count,
    *,
    errors=(),
    runtime_config=None,
    message_id="",
    duplicate=False,
):
    from cpynodus_ii.features.command_intake import CommandResult

    return CommandResult(
        phase=phase,
        topic=topic,
        command_type="logs",
        published_count=published_count,
        errors=tuple(errors),
        runtime_config=runtime_config,
        message_id=message_id,
        duplicate=bool(duplicate),
    )
