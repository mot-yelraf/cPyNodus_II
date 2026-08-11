"""Tests for temporary OTA HTTP endpoints.

The cases exercise route registration, upload validation, status responses,
transfer cleanup, and reboot scheduling with host-side server doubles.
"""

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from cpynodus_ii.core.config import RuntimeConfig
from cpynodus_ii.ota import http as ota_http
from cpynodus_ii.ota.http import OtaHttpController
from cpynodus_ii.ota.state import FwUpdateState, load_ota_state, save_ota_state
from scripts.ota_signing import generate_signing_key, sign_manifest


class _FakeRequest:
    def __init__(self, body=b"", query_params=None, headers=None):
        self.body = body
        self.query_params = dict(query_params or {})
        self.headers = {"X-Nodus-OTA-Session": "s" * 32}
        self.headers.update(headers or {})


class _MappingLike:
    def __init__(self, values):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakeResponse:
    def __init__(self, request, body=None, content_type="", status=None):
        self.request = request
        self.body = body
        self.content_type = content_type
        self.status = status


class _FakeJSONResponse(_FakeResponse):
    pass


class _FakeServer:
    def __init__(self, socket_pool, debug=False):
        self.socket_pool = socket_pool
        self.debug = debug
        self.headers = {}
        self.request_buffer_size = 0
        self.socket_timeout = 0
        self.started = None
        self.routes = {}
        self.poll_count = 0

    def start(self, host, port):
        self.started = (host, port)

    def route(self, path, methods=None):
        def _decorator(fn):
            self.routes[(path, tuple(methods or ()))] = fn
            return fn

        return _decorator

    def poll(self):
        self.poll_count += 1


class _FakeServerModule:
    Server = _FakeServer
    Response = _FakeResponse
    JSONResponse = _FakeJSONResponse


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def test_ota_http_controller_registers_protocol_routes(tmp_path):
    controller = _controller(tmp_path)

    assert controller.phase == "ready"
    assert controller.server.started == ("0.0.0.0", 8000)
    assert ("/ota/status", ("GET",)) in controller.server.routes
    assert ("/ota/begin", ("POST",)) in controller.server.routes
    assert ("/ota/file", ("PUT",)) in controller.server.routes
    assert ("/ota/file/begin", ("POST",)) in controller.server.routes
    assert ("/ota/file/chunk", ("PUT",)) in controller.server.routes
    assert ("/ota/file/end", ("POST",)) in controller.server.routes
    assert ("/ota/commit", ("POST",)) in controller.server.routes
    assert ("/ota/abort", ("POST",)) in controller.server.routes


def test_ota_status_returns_ready_payload(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/status", ("GET",))]

    response = handler(_FakeRequest())

    assert response.body["schema"] == "nodus-ota-status/v1"
    assert response.body["phase"] == "ready"
    assert response.body["package_id"] == ""
    assert response.body["ota_protocol"] == "v2"
    assert response.body["network"]["ipv4addr"] == "10.0.0.213"


def test_ota_begin_validates_manifest_and_marks_staging(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/begin", ("POST",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "code.py",
                "size": 12,
                "sha256": "a" * 64,
            }
        ],
    }

    response = handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["phase"] == "staging"
    assert response.body["files"] == 1
    assert state.phase == "staging"


def test_ota_begin_rejects_package_mismatch(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/begin", ("POST",))]
    save_ota_state(controller.ota_state, str(tmp_path / "_ota" / "state.json"))
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-other",
        "files": [],
    }

    response = handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.status == (400, "Bad Request")
    assert response.body["accepted"] is False
    assert response.body["phase"] == "aborted"
    assert response.body["error"] == "package_id_mismatch"
    assert state is None
    assert controller.ota_state.phase == "aborted"


def test_ota_begin_invalid_json_clears_state(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/begin", ("POST",))]
    save_ota_state(controller.ota_state, str(tmp_path / "_ota" / "state.json"))

    response = handler(_FakeRequest(b"{bad json"))
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.status == (400, "Bad Request")
    assert response.body["accepted"] is False
    assert response.body["phase"] == "aborted"
    assert response.body["error"] == "invalid_json"
    assert state is None
    assert controller.ota_state.phase == "aborted"


def test_ota_abort_marks_state_aborted(tmp_path):
    rebooted = []
    fake_time = _FakeTime()
    controller = _controller(
        tmp_path,
        reboot_callback=lambda: rebooted.append(True),
        reboot_delay_s=5,
        time_module=fake_time,
    )
    handler = controller.server.routes[("/ota/abort", ("POST",))]
    save_ota_state(controller.ota_state, str(tmp_path / "_ota" / "state.json"))
    staged_path = tmp_path / "_ota" / "stage" / "ota_test.py"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_bytes(b"staged payload\n")

    response = handler(_FakeRequest())
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.body["accepted"] is True
    assert response.body["phase"] == "aborted"
    assert state is None
    assert controller.ota_state.phase == "aborted"
    assert not (tmp_path / "_ota" / "stage").exists()
    assert response.body["rebooting"] is True
    assert response.body["reboot_delay_s"] == 5
    controller.poll()
    assert rebooted == []

    fake_time.now = 5.0
    controller.poll()

    assert rebooted == [True]


def test_ota_abort_rejects_after_commit_applied_pending_boot(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/abort", ("POST",))]
    pending = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        session_id="s" * 32,
        phase="applied_pending_boot",
    )
    save_ota_state(pending, str(tmp_path / "_ota" / "state.json"))

    response = handler(_FakeRequest())
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.status == (409, "Conflict")
    assert response.body["accepted"] is False
    assert response.body["phase"] == "applied_pending_boot"
    assert response.body["error"] == "ota_apply_already_pending"
    assert state == pending


def test_ota_begin_clears_previous_workspace_files(tmp_path):
    controller = _controller(tmp_path)
    handler = controller.server.routes[("/ota/begin", ("POST",))]
    old_stage = tmp_path / "_ota" / "stage" / "old.py"
    old_backup = tmp_path / "_ota" / "backup" / "old.py"
    tmp_file = tmp_path / "_ota" / "manifest.json.tmp"
    old_stage.parent.mkdir(parents=True)
    old_backup.parent.mkdir(parents=True)
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    old_stage.write_bytes(b"old stage\n")
    old_backup.write_bytes(b"old backup\n")
    tmp_file.write_text("stale tmp")
    payload = b'print("ok")\n'
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    response = handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert not (tmp_path / "_ota" / "stage").exists()
    assert not (tmp_path / "_ota" / "backup").exists()
    assert not tmp_file.exists()
    assert (tmp_path / "_ota" / "manifest.json").exists()


def test_ota_file_stages_manifest_file_with_sha256_verification(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    payload = b'print("ok")\n'
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "cpynodus_ii/app.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = file_handler(
        _FakeRequest(
            payload,
            query_params={"path": "cpynodus_ii/app.py"},
        )
    )

    staged_path = tmp_path / "_ota" / "stage" / "cpynodus_ii" / "app.py"
    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["path"] == "cpynodus_ii/app.py"
    assert response.body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert staged_path.read_bytes() == payload


def test_ota_file_accepts_path_from_header_when_query_params_are_missing(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    payload = b'print("ok")\n'
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = file_handler(
        _FakeRequest(payload, headers={"X-Nodus-File-Path": "ota_test.py"})
    )

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["path"] == "ota_test.py"
    assert (tmp_path / "_ota" / "stage" / "ota_test.py").read_bytes() == payload


def test_ota_file_accepts_path_from_mapping_like_query_params(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    payload = b'print("ok")\n'
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    request = _FakeRequest(payload)
    request.query_params = _MappingLike({"path": "ota_test.py"})
    response = file_handler(request)

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["path"] == "ota_test.py"


def test_ota_file_accepts_path_from_mapping_like_headers(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    payload = b'print("ok")\n'
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    request = _FakeRequest(payload)
    request.headers = _MappingLike(
        {
            "x-nodus-file-path": "ota_test.py",
            "x-nodus-ota-session": "s" * 32,
        }
    )
    response = file_handler(request)

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["path"] == "ota_test.py"


def test_ota_file_route_returns_error_when_handler_raises(tmp_path, monkeypatch):
    logs = []
    controller = _controller(tmp_path, log_fn=lambda module, msg: logs.append(msg))
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]

    def _raise(_path, _body):
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "_handle_file", _raise)
    response = file_handler(
        _FakeRequest(b"payload", headers={"X-Nodus-File-Path": "ota_test.py"})
    )

    assert response.status == (500, "Internal Server Error")
    assert response.body["accepted"] is False
    assert response.body["error"] == "file_handler_exception"
    assert "file received path=ota_test.py bytes=7" in logs
    assert "file exception error=RuntimeError:boom" in logs


def test_ota_chunked_file_stages_manifest_file(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_begin = controller.server.routes[("/ota/file/begin", ("POST",))]
    file_chunk = controller.server.routes[("/ota/file/chunk", ("PUT",))]
    file_end = controller.server.routes[("/ota/file/end", ("POST",))]
    payload = (b"0123456789abcdef" * 128) + b"done\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "cpynodus_ii/features/payloads.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    begin = file_begin(
        _FakeRequest(query_params={"path": "cpynodus_ii/features/payloads.py"})
    )
    first = file_chunk(
        _FakeRequest(
            payload[:1024],
            query_params={
                "path": "cpynodus_ii/features/payloads.py",
                "offset": "0",
            },
        )
    )
    second = file_chunk(
        _FakeRequest(
            payload[1024:],
            query_params={
                "path": "cpynodus_ii/features/payloads.py",
                "offset": "1024",
            },
        )
    )
    end = file_end(
        _FakeRequest(query_params={"path": "cpynodus_ii/features/payloads.py"})
    )

    staged_path = tmp_path / "_ota" / "stage" / "cpynodus_ii" / "features"
    assert begin.status == (200, "OK")
    assert first.body["offset"] == 1024
    assert second.body["offset"] == len(payload)
    assert end.status == (200, "OK")
    assert end.body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (staged_path / "payloads.py").read_bytes() == payload


def test_ota_chunk_rejects_offset_mismatch(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_begin = controller.server.routes[("/ota/file/begin", ("POST",))]
    file_chunk = controller.server.routes[("/ota/file/chunk", ("PUT",))]
    payload = b"abcdef"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_begin(_FakeRequest(query_params={"path": "ota_test.py"}))
    response = file_chunk(
        _FakeRequest(b"abc", query_params={"path": "ota_test.py", "offset": "2"})
    )

    assert response.status == (409, "Conflict")
    assert response.body["error"] == "chunk_offset_mismatch"
    assert response.body["offset"] == 0


def test_ota_file_rejects_sha256_mismatch(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "code.py",
                "size": 4,
                "sha256": "a" * 64,
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = file_handler(_FakeRequest(b"nope", query_params={"path": "code.py"}))

    assert response.status == (400, "Bad Request")
    assert response.body["accepted"] is False
    assert response.body["error"] == "sha256_mismatch"
    assert not (tmp_path / "_ota" / "stage" / "code.py").exists()


def test_ota_sha256_helper_uses_hashlib_new_when_sha256_attr_missing(monkeypatch):
    class _FakeHasher:
        def __init__(self):
            self.payload = b""

        def update(self, payload):
            self.payload += payload

        def hexdigest(self):
            return hashlib.sha256(self.payload).hexdigest()

    class _FakeHashlib:
        @staticmethod
        def new(name):
            assert name == "sha256"
            return _FakeHasher()

    monkeypatch.setattr(ota_http, "hashlib", _FakeHashlib)

    assert ota_http._sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_ota_sha256_helper_accepts_digest_only_hash_object(monkeypatch, tmp_path):
    class _DigestOnlyHasher:
        def __init__(self):
            self.payload = b""

        def update(self, payload):
            self.payload += payload

        def digest(self):
            return hashlib.sha256(self.payload).digest()

    class _FakeHashlib:
        @staticmethod
        def sha256():
            return _DigestOnlyHasher()

    staged = tmp_path / "staged.mpy"
    staged.write_bytes(b"abc")
    monkeypatch.setattr(ota_http, "hashlib", _FakeHashlib)
    monkeypatch.setattr(
        ota_http,
        "_read_binary_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("full-file read")),
    )

    assert ota_http._sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert ota_http._file_size_sha256(str(staged)) == (
        3,
        hashlib.sha256(b"abc").hexdigest(),
    )


def test_ota_sha256_helper_streams_when_hashlib_sha256_is_unavailable(
    monkeypatch,
    tmp_path,
):
    class _FakeHashlib:
        pass

    staged = tmp_path / "staged.mpy"
    staged.write_bytes(b"abc" * 2048)
    monkeypatch.setattr(ota_http, "hashlib", _FakeHashlib)
    monkeypatch.setattr(
        ota_http,
        "_read_binary_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("full-file read")),
    )

    assert ota_http._sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert ota_http._file_size_sha256("staged.mpy") == (
        -1,
        "",
    )
    assert ota_http._file_size_sha256(str(staged)) == (
        6144,
        hashlib.sha256(b"abc" * 2048).hexdigest(),
    )


def test_ota_file_end_uses_streaming_fallback_when_hashlib_sha256_is_unavailable(
    tmp_path,
    monkeypatch,
):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_end = controller.server.routes[("/ota/file/end", ("POST",))]
    payload = b"large staged payload"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "cpynodus_ii/app.mpy",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    staged_path = tmp_path / "_ota" / "stage" / "cpynodus_ii" / "app.mpy"

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    staged_path.parent.mkdir(parents=True)
    staged_path.write_bytes(payload)
    monkeypatch.setattr(ota_http, "hashlib", type("_FakeHashlib", (), {}))
    monkeypatch.setattr(
        ota_http,
        "_read_binary_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("full-file read")),
    )
    response = file_end(
        _FakeRequest(query_params={"path": "cpynodus_ii/app.mpy"})
    )

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["sha256"] == hashlib.sha256(payload).hexdigest()


def test_ota_file_end_route_returns_error_when_handler_raises(tmp_path, monkeypatch):
    logs = []
    controller = _controller(tmp_path, log_fn=lambda module, msg: logs.append(msg))
    file_end = controller.server.routes[("/ota/file/end", ("POST",))]

    def _raise(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "_handle_file_end", _raise)
    response = file_end(_FakeRequest(query_params={"path": "cpynodus_ii/app.mpy"}))

    assert response.status == (500, "Internal Server Error")
    assert response.body["accepted"] is False
    assert response.body["error"] == "file_end_handler_exception"
    assert "file end exception error=RuntimeError:boom" in logs


def test_ota_file_rejects_path_not_in_manifest(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = file_handler(_FakeRequest(b"x", query_params={"path": "../code.py"}))

    assert response.status == (400, "Bad Request")
    assert response.body["error"] == "file_path_invalid"


def test_ota_commit_verifies_backs_up_and_applies_staged_files(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    live_path = tmp_path / "ota_test.py"
    live_path.write_bytes(b"old payload\n")
    payload = b"OTA transfer check\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    response = commit_handler(_FakeRequest())
    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["phase"] == "applied_pending_boot"
    assert response.body["files"] == 1
    assert state.phase == "applied_pending_boot"
    assert live_path.read_bytes() == payload
    assert not (tmp_path / "_ota" / "stage").exists()
    assert (tmp_path / "_ota" / "backup" / "ota_test.py").read_bytes() == (
        b"old payload\n"
    )
    assert (tmp_path / "_ota" / "manifest.json").exists()


def test_ota_commit_schedules_single_reboot_after_delay(tmp_path):
    reboot_calls = []
    fake_time = _FakeTime()
    controller = _controller(
        tmp_path,
        reboot_callback=lambda: reboot_calls.append("reboot"),
        reboot_delay_s=5,
        time_module=fake_time,
    )
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"OTA transfer check\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    response = commit_handler(_FakeRequest())

    assert response.status == (200, "OK")
    assert response.body["rebooting"] is True
    assert response.body["reboot_delay_s"] == 5
    controller.poll()
    assert reboot_calls == []

    fake_time.now = 5.0
    controller.poll()
    controller.poll()

    assert reboot_calls == ["reboot"]


def test_ota_poll_failure_still_runs_due_reboot(tmp_path):
    reboot_calls = []
    logs = []
    fake_time = _FakeTime()
    controller = _controller(
        tmp_path,
        reboot_callback=lambda: reboot_calls.append("reboot"),
        reboot_delay_s=5,
        time_module=fake_time,
        log_fn=lambda module, message: logs.append(message),
    )
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"OTA transfer check\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    commit_handler(_FakeRequest())

    def _raise():
        raise BrokenPipeError(32)

    fake_time.now = 5.0
    controller.server.poll = _raise
    controller.poll()

    assert "poll failed error=BrokenPipeError:32" in logs
    assert reboot_calls == ["reboot"]


def test_ota_commit_applies_new_file_without_backup(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"new file\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    response = commit_handler(_FakeRequest())

    assert response.status == (200, "OK")
    assert (tmp_path / "ota_test.py").read_bytes() == payload
    assert not (tmp_path / "_ota" / "backup" / "ota_test.py").exists()


def test_ota_commit_rejects_missing_staged_file(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": 4,
                "sha256": hashlib.sha256(b"test").hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = commit_handler(_FakeRequest())

    assert response.status == (400, "Bad Request")
    assert response.body["accepted"] is False
    assert response.body["error"] == "staged_file_missing"


def test_ota_commit_rejects_tampered_staged_file(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"test"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    (tmp_path / "_ota" / "stage" / "ota_test.py").write_bytes(b"nope")
    response = commit_handler(_FakeRequest())

    assert response.status == (400, "Bad Request")
    assert response.body["accepted"] is False
    assert response.body["error"] == "staged_file_sha256_mismatch"


def test_ota_commit_uses_streaming_fallback_when_hashlib_sha256_is_unavailable(
    tmp_path,
    monkeypatch,
):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"large staged payload"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "cpynodus_ii/app.mpy",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    staged_path = tmp_path / "_ota" / "stage" / "cpynodus_ii" / "app.mpy"

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    staged_path.parent.mkdir(parents=True)
    staged_path.write_bytes(payload)
    monkeypatch.setattr(ota_http, "hashlib", type("_FakeHashlib", (), {}))
    monkeypatch.setattr(
        ota_http,
        "_read_binary_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("full-file read")),
    )
    response = commit_handler(_FakeRequest())

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True
    assert response.body["phase"] == "applied_pending_boot"


def test_ota_commit_rejects_when_not_staging(tmp_path):
    controller = _controller(tmp_path)
    commit_response = controller.server.routes[("/ota/commit", ("POST",))](
        _FakeRequest()
    )

    assert commit_response.status == (400, "Bad Request")
    assert commit_response.body["error"] == "ota_not_staging"


def test_ota_file_end_logs_friendly_verification_messages(tmp_path):
    logs = []
    controller = _controller(
        tmp_path,
        log_fn=lambda module, message: logs.append(message),
    )
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    file_end = controller.server.routes[("/ota/file/end", ("POST",))]
    payload = b"verified payload\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "ota_test.py"}))
    response = file_end(_FakeRequest(query_params={"path": "ota_test.py"}))

    assert response.status == (200, "OK")
    assert "Verifying file... path=ota_test.py" in logs
    assert "Verified... path=ota_test.py" in logs


def test_ota_ready_inactivity_aborts_and_reboots(tmp_path):
    rebooted = []
    fake_time = _FakeTime()
    controller = _controller(
        tmp_path,
        reboot_callback=lambda: rebooted.append(True),
        reboot_delay_s=5,
        time_module=fake_time,
    )

    fake_time.now = ota_http.OTA_WAIT_FOR_BEGIN_TIMEOUT_S + 1
    controller.poll()

    assert load_ota_state(str(tmp_path / "_ota" / "state.json")) is None
    assert controller.ota_state.phase == "aborted"
    assert controller.ota_state.error == "ota_begin_timeout"

    fake_time.now += 5
    controller.poll()
    assert rebooted == [True]


def test_ota_poll_failure_runs_delayed_reboot_after_entering_error(tmp_path):
    rebooted = []
    fake_time = _FakeTime()
    controller = _controller(
        tmp_path,
        reboot_callback=lambda: rebooted.append(True),
        reboot_delay_s=5,
        time_module=fake_time,
    )

    def _fail_poll():
        raise RuntimeError("poll failed")

    controller.server.poll = _fail_poll
    controller.poll()

    assert controller.phase == "error"
    assert rebooted == []
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")) is None

    fake_time.now = 5
    controller.poll()

    assert rebooted == [True]


def test_legacy_ota_file_refreshes_staging_activity(tmp_path):
    fake_time = _FakeTime()
    controller = _controller(tmp_path, time_module=fake_time)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    payload = b"verified payload\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    fake_time.now = ota_http.OTA_STAGING_IDLE_TIMEOUT_S - 100
    response = file_handler(
        _FakeRequest(payload, query_params={"path": "ota_test.py"})
    )
    fake_time.now += 200
    controller.poll()

    assert response.status == (200, "OK")
    assert controller.ota_state.phase == "staging"
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")).phase == "staging"


def test_ota_commit_applies_manifest_deletions(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    obsolete = tmp_path / "cpynodus_ii" / "old.py"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_bytes(b"old source\n")
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [],
        "delete": ["cpynodus_ii/old.py"],
    }

    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    response = commit_handler(_FakeRequest())

    assert response.status == (200, "OK")
    assert not obsolete.exists()
    backup = tmp_path / "_ota" / "backup" / "cpynodus_ii" / "old.py"
    assert backup.read_bytes() == b"old source\n"


def test_ota_commit_preserves_recovery_artifacts_when_rollback_fails(
    tmp_path,
    monkeypatch,
):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    file_handler = controller.server.routes[("/ota/file", ("PUT",))]
    commit_handler = controller.server.routes[("/ota/commit", ("POST",))]
    payload = b"new payload\n"
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "existing.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))
    file_handler(_FakeRequest(payload, query_params={"path": "existing.py"}))
    backup = tmp_path / "_ota" / "backup" / "existing.py"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"old payload\n")
    monkeypatch.setattr(
        ota_http,
        "_apply_staged_manifest_files",
        lambda root, document, transaction: "file_apply_failed",
    )
    monkeypatch.setattr(
        ota_http,
        "_rollback_apply_transaction",
        lambda root, document, transaction: "apply_rollback_failed",
    )

    response = commit_handler(_FakeRequest())

    assert response.status == (503, "Service Unavailable")
    assert response.body["phase"] == "applying"
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")).phase == "applying"
    assert (tmp_path / "_ota" / "manifest.json").exists()
    assert (tmp_path / "_ota" / "transaction.json").exists()
    assert backup.read_bytes() == b"old payload\n"


def test_recover_interrupted_apply_restores_replaced_and_new_files(tmp_path):
    old_path = tmp_path / "existing.py"
    new_path = tmp_path / "new.py"
    old_path.write_bytes(b"new existing\n")
    new_path.write_bytes(b"new file\n")
    backup = tmp_path / "_ota" / "backup" / "existing.py"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"old existing\n")
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {"path": "existing.py", "size": 1, "sha256": "a" * 64},
            {"path": "new.py", "size": 1, "sha256": "b" * 64},
        ],
        "delete": [],
    }
    transaction = {
        "paths": ["existing.py", "new.py"],
        "existing": ["existing.py"],
    }
    ota_http._write_json_file(str(tmp_path / "_ota" / "manifest.json"), manifest)
    ota_http._write_json_file(
        str(tmp_path / "_ota" / "transaction.json"),
        transaction,
    )
    save_ota_state(
        FwUpdateState(package_id="ota-tagA-to-tagB", phase="applying"),
        str(tmp_path / "_ota" / "state.json"),
    )

    error = ota_http.recover_interrupted_ota_apply(tmp_path)

    assert error == ""
    assert old_path.read_bytes() == b"old existing\n"
    assert not new_path.exists()
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")) is None


def test_ota_begin_rejects_insufficient_storage(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {"path": "large.mpy", "size": 8192, "sha256": "a" * 64},
        ],
    }
    monkeypatch.setattr(ota_http, "_filesystem_free_bytes", lambda root: 1024)

    response = begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))

    assert response.status == (400, "Bad Request")
    assert response.body["error"] == "insufficient_storage"
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")) is None


def test_ota_begin_rejects_preserve_conflict(tmp_path):
    controller = _controller(tmp_path)
    begin_handler = controller.server.routes[("/ota/begin", ("POST",))]
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {"path": "code.py", "size": 12, "sha256": "a" * 64},
        ],
        "preserve": ["code.py"],
    }

    response = begin_handler(_FakeRequest(json.dumps(manifest).encode("utf-8")))

    assert response.status == (400, "Bad Request")
    assert response.body["error"] == "manifest_preserve_conflict"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl unavailable")
def test_ota_begin_authenticates_exact_v2_manifest(tmp_path):
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "ota-public-key.json"
    key_document = generate_signing_key(private_key, public_key)
    manifest = {
        "schema": "nodus-ota/v2",
        "package_id": "ota-tagA-to-tagB",
        "target": {"platform": "pico2w", "circuitpython": "9.2.8"},
        "requires": {"version": "v0.26.123.6"},
        "files": [
            {
                "path": "code.py",
                "size": 4,
                "sha256": "a" * 64,
            }
        ],
        "delete": [],
        "preserve": ["settings.toml", "ota-public-key.json"],
    }
    manifest_path = tmp_path / "signed-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signature = sign_manifest(manifest_path, private_key)
    raw_manifest = manifest_path.read_bytes()
    state = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        session_id="s" * 32,
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        key_id=key_document["key_id"],
        phase="ready",
    )
    controller = _controller(tmp_path)
    controller.ota_state = state
    controller._authorize_begin = OtaHttpController._authorize_begin.__get__(
        controller, OtaHttpController
    )
    controller.manifest_validator = ota_http._validate_manifest_for_begin
    handler = controller.server.routes[("/ota/begin", ("POST",))]

    response = handler(
        _FakeRequest(
            raw_manifest,
            headers={
                "X-Nodus-OTA-Key-Id": key_document["key_id"],
                "X-Nodus-OTA-Signature": signature["signature"],
            },
        )
    )

    assert response.status == (200, "OK")
    assert response.body["accepted"] is True


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl unavailable")
def test_ota_begin_rejects_bad_signature_and_preserves_prior_backup(tmp_path):
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "ota-public-key.json"
    key_document = generate_signing_key(private_key, public_key)
    raw_manifest = json.dumps(
        {
            "schema": "nodus-ota/v2",
            "package_id": "ota-tagA-to-tagB",
            "target": {"platform": "pico2w", "circuitpython": "9.2.8"},
            "requires": {"version": "v0.26.123.6"},
            "files": [],
            "delete": [],
            "preserve": ["settings.toml", "ota-public-key.json"],
        },
        sort_keys=True,
    ).encode("utf-8")
    state = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        session_id="s" * 32,
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        key_id=key_document["key_id"],
        phase="ready",
    )
    controller = _controller(tmp_path)
    controller.ota_state = state
    controller._authorize_begin = OtaHttpController._authorize_begin.__get__(
        controller, OtaHttpController
    )
    backup = tmp_path / "_ota" / "backup" / "cpynodus_ii" / "app.mpy"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"prior firmware")

    response = controller.server.routes[("/ota/begin", ("POST",))](
        _FakeRequest(
            raw_manifest,
            headers={
                "X-Nodus-OTA-Key-Id": key_document["key_id"],
                "X-Nodus-OTA-Signature": "invalid",
            },
        )
    )

    assert response.status == (401, "Unauthorized")
    assert response.body["error"] == "ota_signature_invalid"
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")) is None
    assert backup.read_bytes() == b"prior firmware"


def test_ota_mutating_route_rejects_wrong_session(tmp_path):
    controller = _controller(tmp_path)
    response = controller.server.routes[("/ota/commit", ("POST",))](
        _FakeRequest(headers={"X-Nodus-OTA-Session": "wrong"})
    )

    assert response.status == (401, "Unauthorized")
    assert response.body["error"] == "ota_session_mismatch"


def _controller(tmp_path, **kwargs):
    state = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        session_id="s" * 32,
        manifest_sha256="a" * 64,
        key_id="test-key",
        phase="ready",
    )
    controller = OtaHttpController(
        RuntimeConfig(),
        SimpleNamespace(
            phase="ready",
            ssid="PeaceHill",
            ip_address="10.0.0.213",
            socket_pool=object(),
            errors=(),
        ),
        state,
        settings_root=tmp_path,
        version="v0.26.123.6",
        server_module=_FakeServerModule,
        **kwargs,
    ).start()
    controller._authorize_begin = lambda request, raw_manifest: ""
    controller.manifest_validator = _legacy_manifest_validator
    return controller


def _legacy_manifest_validator(manifest, state, **_kwargs):
    if manifest.get("schema") != "nodus-ota/v1":
        return "manifest_schema_invalid"
    package_id = str(manifest.get("package_id", "") or "").strip()
    if not package_id:
        return "package_id_missing"
    if package_id != str(getattr(state, "package_id", "") or ""):
        return "package_id_mismatch"
    files = manifest.get("files", ())
    if not isinstance(files, list):
        return "manifest_files_invalid"
    preserve = set(manifest.get("preserve", ()) or ())
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            return "manifest_file_invalid"
        path = ota_http._normalize_package_path(entry.get("path", ""))
        if not path:
            return "manifest_file_path_invalid"
        if path in preserve:
            return "manifest_preserve_conflict"
        if path in seen:
            return "manifest_file_duplicate"
        seen.add(path)
    for raw_path in manifest.get("delete", ()) or ():
        path = ota_http._normalize_package_path(raw_path)
        if not path:
            return "manifest_delete_path_invalid"
        if path in preserve:
            return "manifest_preserve_conflict"
        if path in seen:
            return "manifest_path_conflict"
        seen.add(path)
    return ""
