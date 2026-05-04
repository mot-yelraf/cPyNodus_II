"""Tests for host-side OTA package creation."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ota_package
from scripts.ota_package import (
    OTAPackageError,
    OTATransferError,
    build_ota_package,
    build_worktree_ota_package,
    normalize_manifest_path,
    prepare_fwupdate,
    push_ota_package,
    timestamp_logger,
)


def test_build_ota_package_from_git_tag_range(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "code.py", 'print("boot-a")\n')
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "cpynodus_ii" / "app.py", "APP = 1\n")
    _write(repo / "docs" / "note.md", "old doc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    app_payload = "APP = 2\n"
    _write(repo / "cpynodus_ii" / "app.py", app_payload)
    _write(repo / "docs" / "note.md", "new doc\n")
    _write(repo / "tests" / "test_skip.py", "def test_skip(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    out_dir = tmp_path / "package"
    manifest = build_ota_package(
        repo,
        "tagA",
        "tagB",
        out_dir,
        created_at="2026-05-03T00:00:00Z",
    )

    assert manifest["schema"] == "nodus-ota/v1"
    assert manifest["package_id"] == "ota-tagA-to-tagB"
    assert manifest["from_tag"] == "tagA"
    assert manifest["to_tag"] == "tagB"
    assert manifest["requires"]["version"] == "v0.26.123.3"
    assert manifest["target"] == {"platform": "pico2w", "circuitpython": "9.2.8"}
    assert manifest["delete"] == []
    assert manifest["preserve"] == [
        "settings.toml",
        "sensor_i2c.toml",
        "sensor_soil.toml",
        "switch.toml",
    ]
    assert manifest["post_apply"] == {
        "reboot": True,
        "resume_profile": "previous",
        "reboot_delay_s": 5,
    }

    assert manifest["files"] == [
        {
            "path": "cpynodus_ii/app.py",
            "size": len(app_payload.encode("utf-8")),
            "sha256": hashlib.sha256(app_payload.encode("utf-8")).hexdigest(),
        }
    ]
    assert (out_dir / "files" / "cpynodus_ii" / "app.py").read_text(
        encoding="utf-8"
    ) == app_payload

    manifest_from_disk = json.loads(
        (out_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_from_disk == manifest


def test_build_ota_package_records_deployable_deletes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "cpynodus_ii" / "obsolete.py", "OLD = True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    (repo / "cpynodus_ii" / "obsolete.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    manifest = build_ota_package(repo, "tagA", "tagB", tmp_path / "package")

    assert manifest["files"] == []
    assert manifest["delete"] == ["cpynodus_ii/obsolete.py"]


def test_build_ota_package_includes_ota_test_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.8"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    _write(repo / "ota_test.py", "VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    manifest = build_ota_package(repo, "tagA", "tagB", tmp_path / "package")

    assert manifest["files"][0]["path"] == "ota_test.py"
    assert (tmp_path / "package" / "files" / "ota_test.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_build_ota_package_rejects_missing_tag(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "tag", "tagA")

    with pytest.raises(OTAPackageError, match="missing_git_ref:missing"):
        build_ota_package(repo, "tagA", "missing", tmp_path / "package")


def test_nodus_ota_cli_package_command(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "code.py", 'print("a")\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    _write(repo / "code.py", 'print("b")\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    script = Path(__file__).resolve().parents[1] / "scripts" / "nodus_ota.py"
    out_dir = tmp_path / "package"
    result = subprocess.run(
        (
            sys.executable,
            str(script),
            "package",
            "--repo",
            str(repo),
            "--from",
            "tagA",
            "--to",
            "tagB",
            "--out",
            str(out_dir),
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert "created ota-tagA-to-tagB files=1 delete=0" in result.stdout
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "files" / "code.py").read_text(encoding="utf-8") == 'print("b")\n'


def test_nodus_ota_cli_package_command_supports_include_and_exclude(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "code.py", 'print("a")\n')
    _write(repo / "ota_test.py", "VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    _write(repo / "code.py", 'print("b")\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    script = Path(__file__).resolve().parents[1] / "scripts" / "nodus_ota.py"
    out_dir = tmp_path / "package"
    subprocess.run(
        (
            sys.executable,
            str(script),
            "package",
            "--repo",
            str(repo),
            "--from",
            "tagA",
            "--to",
            "tagB",
            "--out",
            str(out_dir),
            "--include",
            "ota_test.py",
            "--exclude",
            "code.py",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["files"]] == ["ota_test.py"]


def test_build_worktree_ota_package_includes_current_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.13"\n')
    _write(repo / "ota_test.py", "VALUE = 2\n")

    manifest = build_worktree_ota_package(
        repo,
        tmp_path / "package",
        package_id="ota-working-test",
        include=("ota_test.py",),
        created_at="2026-05-03T00:00:00Z",
    )

    assert manifest["package_id"] == "ota-working-test"
    assert manifest["source"] == "working-tree"
    assert manifest["to_tag"] == "working-tree"
    assert manifest["requires"]["version"] == "v0.26.123.13"
    assert manifest["files"][0]["path"] == "ota_test.py"
    assert (tmp_path / "package" / "files" / "ota_test.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"


def test_nodus_ota_cli_package_worktree_command(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.13"\n')
    _write(repo / "ota_test.py", "VALUE = 2\n")

    exit_code = ota_package.main(
        [
            "package-worktree",
            "--repo",
            str(repo),
            "--out",
            str(tmp_path / "package"),
            "--package-id",
            "ota-working-test",
            "--include",
            "ota_test.py",
        ]
    )

    assert exit_code == 0
    manifest = json.loads(
        (tmp_path / "package" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["package_id"] == "ota-working-test"
    assert [entry["path"] for entry in manifest["files"]] == ["ota_test.py"]


def test_prepare_fwupdate_publishes_mqtt_prepare_payload():
    client = _FakeMqttClient()
    logs = []

    result = prepare_fwupdate(
        "broker.local",
        "co2-ph244",
        "ota-working-test",
        mqtt_client_factory=lambda: client,
        message_id="fw-1",
        log_fn=logs.append,
    )

    assert result["topic"] == "nodus/co2-ph244/fwupdate"
    assert client.connected == ("broker.local", 1883, 60)
    assert client.disconnected is True
    assert client.published[0][0] == "nodus/co2-ph244/fwupdate"
    assert json.loads(client.published[0][1]) == {
        "schema": "nodus-fwupdate/v1",
        "message_id": "fw-1",
        "command": "prepare",
        "package_id": "ota-working-test",
    }
    assert client.published[0][2] == 1
    assert logs == [
        "mqtt connect broker=broker.local port=1883",
        "mqtt publish topic=nodus/co2-ph244/fwupdate package=ota-working-test",
        "mqtt published message_id=fw-1",
    ]


def test_timestamp_logger_prefixes_console_messages(capsys):
    class _Clock:
        @staticmethod
        def replace(microsecond=0):
            return _Clock()

        @staticmethod
        def isoformat():
            return "2026-05-03T12:00:00"

    logger = timestamp_logger(clock=lambda: _Clock())

    logger("hello")

    assert capsys.readouterr().out == "[2026-05-03T12:00:00] hello\n"


def test_push_ota_package_sends_begin_files_and_commit(tmp_path):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-tagA-to-tagB")
    logs = []

    result = push_ota_package(
        package,
        "10.0.0.213:8000",
        timeout_s=3,
        opener=opener,
        log_fn=logs.append,
    )

    assert result["package_id"] == "ota-tagA-to-tagB"
    assert result["files"] == 1
    assert result["commit"]["phase"] == "applied_pending_boot"
    assert [
        (request.get_method(), request.full_url)
        for request, _timeout in opener.requests
    ] == [
        ("GET", "http://10.0.0.213:8000/ota/status"),
        ("POST", "http://10.0.0.213:8000/ota/begin"),
        ("POST", "http://10.0.0.213:8000/ota/file/begin?path=ota_test.py"),
        ("PUT", "http://10.0.0.213:8000/ota/file/chunk?path=ota_test.py&offset=0"),
        ("POST", "http://10.0.0.213:8000/ota/file/end?path=ota_test.py"),
        ("POST", "http://10.0.0.213:8000/ota/commit"),
    ]
    begin_payload = json.loads(opener.requests[1][0].data.decode("utf-8"))
    assert begin_payload["package_id"] == "ota-tagA-to-tagB"
    assert opener.requests[3][0].data == (
        package / "files" / "ota_test.py"
    ).read_bytes()
    assert opener.requests[3][0].headers["X-nodus-file-path"] == "ota_test.py"
    assert logs == [
        "status http://10.0.0.213:8000",
        "begin package=ota-tagA-to-tagB files=1",
        "file ota_test.py bytes=22 chunk=1024",
        "chunk ota_test.py offset=22/22",
        "commit",
        "committed phase=applied_pending_boot rebooting=True delay_s=5",
    ]


def test_push_ota_package_rejects_device_package_mismatch(tmp_path):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-other")

    with pytest.raises(OTATransferError, match="device_package_mismatch"):
        push_ota_package(package, "http://10.0.0.213:8000", opener=opener)


def test_nodus_ota_cli_push_command(tmp_path, monkeypatch, capsys):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-tagA-to-tagB")
    monkeypatch.setattr(ota_package, "urlopen", opener)

    exit_code = ota_package.main(
        [
            "push",
            str(package),
            "--device",
            "http://10.0.0.213:8000",
            "--timeout",
            "3",
        ]
    )

    assert exit_code == 0
    assert "pushed ota-tagA-to-tagB files=1" in capsys.readouterr().out


def test_normalize_manifest_path_rejects_path_traversal():
    with pytest.raises(OTAPackageError, match="unsafe_package_path"):
        normalize_manifest_path("../settings.toml")


def _init_repo(path):
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test User")
    return path


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo, *args):
    subprocess.run(
        ("git",) + args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_test_package(path):
    payload = b"OTA transfer check\n42\n"
    files = path / "files"
    files.mkdir(parents=True)
    (files / "ota_test.py").write_bytes(payload)
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
    (path / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    return path


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakeOtaOpener:
    def __init__(self, *, package_id):
        self.package_id = package_id
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        url = request.full_url
        if url.endswith("/ota/status"):
            return _FakeResponse(
                {
                    "schema": "nodus-ota-status/v1",
                    "phase": "ready",
                    "package_id": self.package_id,
                }
            )
        if url.endswith("/ota/begin"):
            return _FakeResponse({"accepted": True, "phase": "staging"})
        if url.endswith("/ota/file/begin?path=ota_test.py"):
            return _FakeResponse({"accepted": True, "phase": "staging"})
        if "/ota/file/chunk?path=ota_test.py&offset=0" in url:
            return _FakeResponse(
                {"accepted": True, "phase": "staging", "offset": 22}
            )
        if url.endswith("/ota/file/end?path=ota_test.py"):
            return _FakeResponse({"accepted": True, "phase": "staging"})
        if url.endswith("/ota/commit"):
            return _FakeResponse(
                {
                    "accepted": True,
                    "phase": "applied_pending_boot",
                    "rebooting": True,
                    "reboot_delay_s": 5,
                }
            )
        return _FakeResponse({"accepted": False, "error": "unexpected_request"})


class _FakePublishInfo:
    def __init__(self):
        self.waited = False

    def wait_for_publish(self):
        self.waited = True


class _FakeMqttClient:
    def __init__(self):
        self.connected = None
        self.disconnected = False
        self.published = []
        self.publish_info = _FakePublishInfo()

    def connect(self, broker, port, keepalive):
        self.connected = (broker, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return self.publish_info

    def disconnect(self):
        self.disconnected = True
