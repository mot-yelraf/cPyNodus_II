"""Tests for the host-side MQTT log retrieval tool.

The cases simulate broker messages and transfer state to verify complete,
failed, and timed-out log retrieval workflows.
"""

import json
from types import SimpleNamespace

import scripts.nodus_getlogs as nodus_getlogs
from scripts.nodus_getlogs import retrieve_log, retrieve_logs


class _Info:
    def wait_for_publish(self):
        return True


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.on_message = None
        self.subscriptions = []
        self.published = []

    def connect(self, broker, port, keepalive):
        self.connected = (broker, port, keepalive)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        for response_topic, response_payload in self.responses:
            self.on_message(
                self,
                None,
                SimpleNamespace(
                    topic=response_topic,
                    payload=json.dumps(response_payload).encode("utf-8"),
                ),
            )
        return _Info()


def test_retrieve_log_writes_completed_file(tmp_path):
    responses = (
        (
            "nodus/co2-v5p04u/logs/ack",
            {
                "schema": "nodus-log-transfer/v1",
                "message_id": "log-1",
                "accepted": True,
                "filename": "_reboot.log",
                "size": 5,
                "chunk_size": 5,
            },
        ),
        (
            "nodus/co2-v5p04u/logs/chunk",
            {
                "schema": "nodus-log-transfer/v1",
                "message_id": "log-1",
                "filename": "_reboot.log",
                "sequence": 0,
                "offset": 0,
                "next_offset": 5,
                "size": 5,
                "encoding": "base64",
                "data": "aGVsbG8=",
                "checksum": "4f9f2cab",
            },
        ),
        (
            "nodus/co2-v5p04u/logs/result",
            {
                "schema": "nodus-log-transfer/v1",
                "message_id": "log-1",
                "complete": True,
                "filename": "_reboot.log",
                "size": 5,
                "chunks": 1,
                "checksum": "4f9f2cab",
            },
        ),
    )
    client = _Client(responses)

    result = retrieve_log(
        broker="broker.local",
        port=1883,
        device_id="co2-v5p04u",
        filename="_reboot.log",
        storage_dir=tmp_path,
        message_id="log-1",
        mqtt_client_factory=lambda: client,
    )

    output = tmp_path / "co2-v5p04u" / "_reboot.log"
    assert output.read_bytes() == b"hello"
    assert result["path"] == str(output)
    assert result["bytes"] == 5
    assert client.subscriptions == [
        ("nodus/co2-v5p04u/logs/ack", 1),
        ("nodus/co2-v5p04u/logs/chunk", 1),
        ("nodus/co2-v5p04u/logs/result", 1),
    ]
    assert json.loads(client.published[0][1])["chunk_size"] == 512


def test_retrieve_logs_requests_reboot_and_recovery_logs(tmp_path, monkeypatch):
    def _message_id(*, filename=""):
        if filename == "_reboot.log":
            return "log-reboot-log-20260509T000000Z"
        return "log-recovery-log-20260509T000000Z"

    monkeypatch.setattr(nodus_getlogs, "_message_id", _message_id)
    reboot_client = _Client(
        (
            (
                "nodus/co2-v5p04u/logs/ack",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-reboot-log-20260509T000000Z",
                    "accepted": True,
                    "filename": "_reboot.log",
                    "size": 5,
                    "chunk_size": 5,
                },
            ),
            (
                "nodus/co2-v5p04u/logs/chunk",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-reboot-log-20260509T000000Z",
                    "filename": "_reboot.log",
                    "sequence": 0,
                    "offset": 0,
                    "next_offset": 5,
                    "size": 5,
                    "encoding": "base64",
                    "data": "aGVsbG8=",
                    "checksum": "4f9f2cab",
                },
            ),
            (
                "nodus/co2-v5p04u/logs/result",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-reboot-log-20260509T000000Z",
                    "complete": True,
                    "filename": "_reboot.log",
                    "size": 5,
                    "chunks": 1,
                    "checksum": "4f9f2cab",
                },
            ),
        )
    )
    recovery_client = _Client(
        (
            (
                "nodus/co2-v5p04u/logs/ack",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-recovery-log-20260509T000000Z",
                    "accepted": True,
                    "filename": "_recovery.log",
                    "size": 5,
                    "chunk_size": 5,
                },
            ),
            (
                "nodus/co2-v5p04u/logs/chunk",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-recovery-log-20260509T000000Z",
                    "filename": "_recovery.log",
                    "sequence": 0,
                    "offset": 0,
                    "next_offset": 5,
                    "size": 5,
                    "encoding": "base64",
                    "data": "d29ybGQ=",
                    "checksum": "37a3e893",
                },
            ),
            (
                "nodus/co2-v5p04u/logs/result",
                {
                    "schema": "nodus-log-transfer/v1",
                    "message_id": "log-recovery-log-20260509T000000Z",
                    "complete": True,
                    "filename": "_recovery.log",
                    "size": 5,
                    "chunks": 1,
                    "checksum": "37a3e893",
                },
            ),
        )
    )
    clients = [reboot_client, recovery_client]

    results = retrieve_logs(
        broker="broker.local",
        port=1883,
        device_id="co2-v5p04u",
        filenames=("_reboot.log", "_recovery.log"),
        storage_dir=tmp_path,
        mqtt_client_factory=lambda: clients.pop(0),
    )

    assert len(results) == 2
    assert (tmp_path / "co2-v5p04u" / "_reboot.log").read_bytes() == b"hello"
    assert (tmp_path / "co2-v5p04u" / "_recovery.log").read_bytes() == b"world"
    assert json.loads(reboot_client.published[0][1])["filename"] == "_reboot.log"
    assert json.loads(recovery_client.published[0][1])["filename"] == "_recovery.log"
