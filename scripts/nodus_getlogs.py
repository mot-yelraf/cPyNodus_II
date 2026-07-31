"""Retrieve bounded Nodus device logs over MQTT and store them locally.

``retrieve_log`` and ``retrieve_logs`` implement the chunked transfer protocol;
``load_config`` and ``main`` support command-line use. Files are finalized only
after sequence, size, and digest validation succeeds.
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None

SCHEMA = "nodus-log-transfer/v1"
DEFAULT_CONFIG = "scripts/nodus_getlogs.toml"
DEFAULT_CHUNK_SIZE = 512
DEFAULT_LOG_FILES = ("_reboot.log", "_recovery.log")


class LogTransferError(ValueError):
    """Raised when a Nodus MQTT log transfer fails."""


def retrieve_log(
    *,
    broker,
    device_id,
    storage_dir,
    port=1883,
    base_topic="nodus",
    username="",
    password="",
    use_tls=False,
    filename="_reboot.log",
    chunk_size=DEFAULT_CHUNK_SIZE,
    timeout_s=60.0,
    message_id="",
    mqtt_client_factory=None,
    log_fn=None,
):
    """Request one Nodus log file and store it after successful transfer."""
    broker = str(broker or "").strip()
    device_id = str(device_id or "").strip().strip("/")
    if not broker:
        raise LogTransferError("broker_missing")
    if not device_id or "/" in device_id:
        raise LogTransferError("device_id_invalid")
    request_id = message_id or _message_id()
    topics = _topics(device_id, base_topic=base_topic)
    client_factory = mqtt_client_factory or _paho_client_factory
    client = client_factory()
    transfer = _TransferState(
        message_id=request_id,
        device_id=device_id,
        filename=str(filename or "_reboot.log").strip(),
        storage_dir=Path(storage_dir or "."),
        log_fn=log_fn,
    )

    _configure_client(
        client,
        username=username,
        password=password,
        use_tls=bool(use_tls),
    )
    _bind_callbacks(client, transfer, topics)

    _log(log_fn, "mqtt connect broker={} port={}".format(broker, int(port or 1883)))
    client.connect(broker, int(port or 1883), 60)
    client.loop_start()
    try:
        for topic in (topics["ack"], topics["chunk"], topics["result"]):
            client.subscribe(topic, qos=1)
        time.sleep(0.2)
        payload = {
            "schema": SCHEMA,
            "message_id": request_id,
            "filename": transfer.filename,
            "chunk_size": int(chunk_size or DEFAULT_CHUNK_SIZE),
        }
        _log(
            log_fn,
            "request topic={} device={} file={} chunk={}".format(
                topics["get"],
                device_id,
                transfer.filename,
                int(chunk_size or DEFAULT_CHUNK_SIZE),
            ),
        )
        info = client.publish(
            topics["get"],
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        wait = getattr(info, "wait_for_publish", None)
        if callable(wait):
            wait()
        result = transfer.wait(timeout_s)
    finally:
        client.loop_stop()
        client.disconnect()
    return result


def retrieve_logs(
    *,
    broker,
    device_id,
    storage_dir,
    filenames=DEFAULT_LOG_FILES,
    port=1883,
    base_topic="nodus",
    username="",
    password="",
    use_tls=False,
    chunk_size=DEFAULT_CHUNK_SIZE,
    timeout_s=60.0,
    message_id="",
    mqtt_client_factory=None,
    log_fn=None,
):
    """Request multiple Nodus log files and return successful transfers."""
    normalized_filenames = _normalize_filenames(filenames)
    results = []
    for index, filename in enumerate(normalized_filenames, start=1):
        _log(
            log_fn,
            "transfer {}/{} file={}".format(
                index,
                len(normalized_filenames),
                filename,
            ),
        )
        request_id = ""
        if message_id and len(normalized_filenames) == 1:
            request_id = message_id
        results.append(
            retrieve_log(
                broker=broker,
                port=port,
                base_topic=base_topic,
                username=username,
                password=password,
                use_tls=use_tls,
                device_id=device_id,
                filename=filename,
                chunk_size=chunk_size,
                timeout_s=timeout_s,
                storage_dir=storage_dir,
                message_id=request_id or _message_id(filename=filename),
                mqtt_client_factory=mqtt_client_factory,
                log_fn=log_fn,
            )
        )
    return tuple(results)


class _TransferState:
    def __init__(self, *, message_id, device_id, filename, storage_dir, log_fn=None):
        self.message_id = message_id
        self.device_id = device_id
        self.filename = filename
        self.storage_dir = storage_dir
        self.log_fn = log_fn
        self.started_at = time.monotonic()
        self.expected_size = 0
        self.next_offset = 0
        self.chunks = 0
        self.file_checksum = 2166136261
        self.data = bytearray()
        self.complete = False
        self.error = ""
        self.output_path = None

    def handle_message(self, topic, payload):
        document = _json_payload(payload)
        if document.get("message_id") != self.message_id:
            return
        if topic.endswith("/logs/ack"):
            self._handle_ack(document)
        elif topic.endswith("/logs/chunk"):
            self._handle_chunk(document)
        elif topic.endswith("/logs/result"):
            self._handle_result(document)

    def wait(self, timeout_s):
        deadline = time.monotonic() + float(timeout_s or 60.0)
        while time.monotonic() < deadline:
            if self.complete:
                return {
                    "device_id": self.device_id,
                    "filename": self.filename,
                    "path": str(self.output_path),
                    "bytes": len(self.data),
                    "chunks": self.chunks,
                    "checksum": _checksum_hex(self.file_checksum),
                    "elapsed_s": time.monotonic() - self.started_at,
                }
            if self.error:
                raise LogTransferError(self.error)
            time.sleep(0.05)
        raise LogTransferError("transfer_timeout")

    def _handle_ack(self, document):
        if document.get("accepted") is not True:
            self.error = str(document.get("error", "") or "request_rejected")
            return
        self.expected_size = int(document.get("size", 0) or 0)
        _log(
            self.log_fn,
            "accepted file={} bytes={} chunk={}".format(
                document.get("filename", self.filename),
                self.expected_size,
                document.get("chunk_size", ""),
            ),
        )

    def _handle_chunk(self, document):
        try:
            offset = int(document.get("offset", -1))
            raw = base64.b64decode(str(document.get("data", "") or ""))
        except (TypeError, ValueError):
            self.error = "chunk_invalid"
            return
        if offset != self.next_offset:
            self.error = "chunk_offset_mismatch:{}!={}".format(offset, self.next_offset)
            return
        expected_checksum = str(document.get("checksum", "") or "")
        actual_checksum = _checksum_hex(_checksum32(raw))
        if expected_checksum and actual_checksum != expected_checksum:
            self.error = "chunk_checksum_mismatch:{}".format(offset)
            return
        self.data.extend(raw)
        self.next_offset += len(raw)
        self.file_checksum = _checksum32(raw, seed=self.file_checksum)
        self.chunks += 1
        total = int(document.get("size", self.expected_size) or self.expected_size)
        _log(
            self.log_fn,
            "chunk {} offset={}/{} checksum={} elapsed_s={:.1f}".format(
                self.chunks,
                self.next_offset,
                total,
                actual_checksum,
                time.monotonic() - self.started_at,
            ),
        )

    def _handle_result(self, document):
        if document.get("complete") is not True:
            self.error = str(document.get("error", "") or "transfer_failed")
            return
        expected_checksum = str(document.get("checksum", "") or "")
        actual_checksum = _checksum_hex(self.file_checksum)
        if expected_checksum and expected_checksum != actual_checksum:
            self.error = "file_checksum_mismatch"
            return
        expected_size = int(document.get("size", 0) or 0)
        if expected_size and expected_size != len(self.data):
            self.error = "file_size_mismatch"
            return
        self.output_path = self._write_file()
        self.complete = True
        _log(
            self.log_fn,
            "complete file={} bytes={} chunks={} checksum={}".format(
                self.output_path,
                len(self.data),
                self.chunks,
                actual_checksum,
            ),
        )

    def _write_file(self):
        device_dir = self.storage_dir / self.device_id
        device_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(self.filename).name
        output_path = device_dir / safe_name
        tmp_path = output_path.with_name(".{}.part".format(safe_name))
        tmp_path.write_bytes(bytes(self.data))
        tmp_path.replace(output_path)
        return output_path


def load_config(path):
    """Load host-side log retrieval settings from TOML."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    if tomllib is None:
        raise LogTransferError("tomllib_unavailable")
    with config_path.open("rb") as handle:
        document = tomllib.load(handle)
    return document if isinstance(document, dict) else {}


def main(argv=None):
    """Run the Nodus MQTT log retrieval command line tool."""
    parser = argparse.ArgumentParser(prog="nodus-getlogs")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--broker", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--base-topic", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument(
        "--filename",
        action="append",
        default=None,
        help="Log file to request. May be repeated.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Request _reboot.log and _recovery.log.",
    )
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--storage-dir", default="")
    parser.add_argument("--message-id", default="")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    mqtt_config = config.get("mqtt", {}) if isinstance(config.get("mqtt"), dict) else {}
    transfer_config = (
        config.get("transfer", {}) if isinstance(config.get("transfer"), dict) else {}
    )
    log = timestamp_logger()
    try:
        filenames = _filenames_from_args(args, transfer_config)
        results = retrieve_logs(
            broker=args.broker or mqtt_config.get("broker", ""),
            port=args.port or mqtt_config.get("port", 1883),
            base_topic=args.base_topic or mqtt_config.get("base_topic", "nodus"),
            username=args.username or mqtt_config.get("username", ""),
            password=args.password or mqtt_config.get("password", ""),
            use_tls=bool(args.tls or mqtt_config.get("use_tls", False)),
            device_id=args.device_id,
            filenames=filenames,
            chunk_size=args.chunk_size
            or transfer_config.get("chunk_size", DEFAULT_CHUNK_SIZE),
            timeout_s=args.timeout or transfer_config.get("timeout_s", 60.0),
            storage_dir=args.storage_dir
            or transfer_config.get("storage_dir", "build/nodus-logs"),
            message_id=args.message_id,
            log_fn=log,
        )
    except LogTransferError as exc:
        log("failed: {}".format(exc))
        return 1
    for result in results:
        log(
            "saved device={device_id} file={filename} bytes={bytes} path={path}".format(
                **result
            )
        )
    log("summary device={} files={}".format(args.device_id, len(results)))
    return 0


def timestamp_logger(stream=None, *, clock=None):
    """Return a console logger that prefixes messages with local timestamps."""
    output = stream or sys.stdout
    clock_fn = clock or datetime.now

    def _logger(message):
        stamp = clock_fn().replace(microsecond=0).isoformat()
        print("[{}] {}".format(stamp, message), file=output)

    return _logger


def _bind_callbacks(client, transfer, topics):
    def _on_message(_client, _userdata, message):
        transfer.handle_message(str(message.topic), message.payload)

    client.on_message = _on_message


def _configure_client(client, *, username="", password="", use_tls=False):
    if username:
        username_pw_set = getattr(client, "username_pw_set", None)
        if callable(username_pw_set):
            username_pw_set(username, password or None)
    if use_tls:
        tls_set_context = getattr(client, "tls_set_context", None)
        tls_set = getattr(client, "tls_set", None)
        if callable(tls_set_context):
            tls_set_context(ssl.create_default_context())
        elif callable(tls_set):
            tls_set()


def _topics(device_id, *, base_topic="nodus"):
    base = str(base_topic or "nodus").strip().strip("/")
    return {
        "get": "{}/{}/logs/get".format(base, device_id),
        "ack": "{}/{}/logs/ack".format(base, device_id),
        "chunk": "{}/{}/logs/chunk".format(base, device_id),
        "result": "{}/{}/logs/result".format(base, device_id),
    }


def _json_payload(payload):
    raw = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload or "")
    try:
        document = json.loads(raw)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _checksum32(data, seed=2166136261):
    checksum = int(seed) & 0xFFFFFFFF
    for value in data or b"":
        checksum ^= int(value)
        checksum = (checksum * 16777619) & 0xFFFFFFFF
    return checksum


def _checksum_hex(value):
    return "{:08x}".format(int(value or 0) & 0xFFFFFFFF)


def _message_id(*, filename=""):
    suffix = Path(str(filename or "log")).name.replace(".", "-").replace("_", "-")
    return "log-{}-{}".format(
        suffix.strip("-") or "file",
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )


def _filenames_from_args(args, transfer_config):
    if bool(getattr(args, "all", False)):
        return DEFAULT_LOG_FILES
    if args.filename:
        return tuple(args.filename)
    configured = transfer_config.get("filenames")
    if configured:
        return _normalize_filenames(configured)
    return _normalize_filenames((transfer_config.get("filename", "_reboot.log"),))


def _normalize_filenames(filenames):
    if isinstance(filenames, str):
        values = (filenames,)
    else:
        values = tuple(filenames or ())
    normalized = []
    for filename in values:
        value = str(filename or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        return DEFAULT_LOG_FILES
    return tuple(normalized)


def _log(log_fn, message):
    if callable(log_fn):
        log_fn(message)


def _paho_client_factory():
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise LogTransferError("paho_mqtt_not_installed") from exc
    return mqtt.Client()


if __name__ == "__main__":
    raise SystemExit(main())
