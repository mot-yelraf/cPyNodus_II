"""Tests for host-side OTA package creation.

The cases validate manifests, compiled-module policy, signatures, archive
contents, and transfer helpers used by the packaging workflow.
"""

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

TEST_SESSION = "s" * 32


def test_safemode_hook_is_a_deployable_root_runtime_file():
    assert ota_package._is_deployable_path("safemode.py") is True


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
    compiled = tmp_path / "compiled"
    compiled_payload = b"compiled-app"
    (compiled / "cpynodus_ii").mkdir(parents=True)
    (compiled / "cpynodus_ii" / "app.mpy").write_bytes(compiled_payload)
    _write_build_info(compiled, project_version="v0.26.123.3")

    out_dir = tmp_path / "package"
    manifest = build_ota_package(
        repo,
        "tagA",
        "tagB",
        out_dir,
        created_at="2026-05-03T00:00:00Z",
        compiled_root=compiled,
    )

    assert manifest["schema"] == "nodus-ota/v2"
    assert manifest["package_id"] == "ota-tagA-to-tagB"
    assert manifest["from_tag"] == "tagA"
    assert manifest["to_tag"] == "tagB"
    assert manifest["requires"]["version"] == "v0.26.123.3"
    assert manifest["target"] == {"platform": "pico2w", "circuitpython": "9.2.8"}
    assert manifest["delete"] == ["cpynodus_ii/app.py"]
    assert manifest["preserve"] == [
        "settings.toml",
        "sensor_i2c.toml",
        "sensor_soil.toml",
        "switch.toml",
        "ota-public-key.json",
    ]
    assert manifest["post_apply"] == {
        "reboot": True,
        "resume_profile": "previous",
        "reboot_delay_s": 5,
    }

    assert manifest["files"] == [
        {
            "path": "cpynodus_ii/app.mpy",
            "size": len(compiled_payload),
            "sha256": hashlib.sha256(compiled_payload).hexdigest(),
        }
    ]
    assert (
        out_dir / "files" / "cpynodus_ii" / "app.mpy"
    ).read_bytes() == compiled_payload

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
    assert manifest["delete"] == [
        "cpynodus_ii/obsolete.mpy",
        "cpynodus_ii/obsolete.py",
    ]


def test_build_ota_package_includes_root_runtime_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.8"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    root_file = "code.py"
    _write(repo / root_file, "VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    manifest = build_ota_package(repo, "tagA", "tagB", tmp_path / "package")

    assert manifest["files"][0]["path"] == root_file
    assert (tmp_path / "package" / "files" / root_file).read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_build_ota_package_includes_board_templates_and_root_def_deletes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.8"\n')
    _write(repo / "settings.toml.def", "[Network]\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    (repo / "settings.toml.def").unlink()
    template_path = "boards/pico2w/templates/sensor_i2c.toml.def"
    template_payload = "[Sensor]\nDEVICE = \"\"\n"
    _write(repo / template_path, template_payload)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    manifest = build_ota_package(repo, "tagA", "tagB", tmp_path / "package")

    assert [entry["path"] for entry in manifest["files"]] == [template_path]
    assert manifest["delete"] == ["settings.toml.def"]
    assert (tmp_path / "package" / "files" / template_path).read_text(
        encoding="utf-8"
    ) == template_payload


def test_build_ota_package_uses_compiled_modules_and_deletes_sources(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "cpynodus_ii" / "app.py", "APP = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    _write(repo / "cpynodus_ii" / "app.py", "APP = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    compiled = tmp_path / "compiled"
    compiled_payload = b"compiled-app"
    (compiled / "cpynodus_ii").mkdir(parents=True)
    (compiled / "cpynodus_ii" / "app.mpy").write_bytes(compiled_payload)
    _write_build_info(compiled, project_version="v0.26.123.3")

    manifest = build_ota_package(
        repo,
        "tagA",
        "tagB",
        tmp_path / "package",
        target="pico2w",
        compiled_root=compiled,
    )

    assert manifest["target"] == {"platform": "pico2w", "circuitpython": "9.2.8"}
    assert manifest["delete"] == ["cpynodus_ii/app.py"]
    assert manifest["files"] == [
        {
            "path": "cpynodus_ii/app.mpy",
            "size": len(compiled_payload),
            "sha256": hashlib.sha256(compiled_payload).hexdigest(),
        }
    ]
    assert (
        tmp_path / "package" / "files" / "cpynodus_ii" / "app.mpy"
    ).read_bytes() == compiled_payload
    assert not (tmp_path / "package" / "files" / "cpynodus_ii" / "app.py").exists()


def test_build_compiled_ota_package_deletes_removed_source_and_mpy(tmp_path):
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

    compiled = tmp_path / "compiled"
    compiled.mkdir()
    _write_build_info(compiled, project_version="v0.26.123.3")
    manifest = build_ota_package(
        repo,
        "tagA",
        "tagB",
        tmp_path / "package",
        compiled_root=compiled,
    )

    assert manifest["files"] == []
    assert manifest["delete"] == [
        "cpynodus_ii/obsolete.mpy",
        "cpynodus_ii/obsolete.py",
    ]


def test_build_compiled_ota_package_rejects_missing_artifact(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "cpynodus_ii" / "app.py", "APP = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")
    _write(repo / "cpynodus_ii" / "app.py", "APP = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    compiled = tmp_path / "compiled"
    compiled.mkdir()
    _write_build_info(compiled, project_version="v0.26.123.3")

    with pytest.raises(OTAPackageError, match="compiled_artifact_missing"):
        build_ota_package(
            repo,
            "tagA",
            "tagB",
            tmp_path / "package",
            compiled_root=compiled,
        )


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
    signing_key = _write_signing_key(tmp_path)
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
            "--signing-key",
            str(signing_key),
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert "created ota-tagA-to-tagB files=1 delete=0" in result.stdout
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "manifest.sig").exists()
    assert (out_dir / "files" / "code.py").read_text(encoding="utf-8") == 'print("b")\n'


def test_nodus_ota_cli_package_command_supports_include_and_exclude(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.3"\n')
    _write(repo / "code.py", 'print("a")\n')
    _write(repo / "dataclasses.py", "VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag a")
    _git(repo, "tag", "tagA")

    _write(repo / "code.py", 'print("b")\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tag b")
    _git(repo, "tag", "tagB")

    script = Path(__file__).resolve().parents[1] / "scripts" / "nodus_ota.py"
    out_dir = tmp_path / "package"
    signing_key = _write_signing_key(tmp_path)
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
            "dataclasses.py",
            "--exclude",
            "code.py",
            "--signing-key",
            str(signing_key),
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["files"]] == ["dataclasses.py"]


def test_build_worktree_ota_package_includes_current_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.13"\n')
    _write(repo / "code.py", "VALUE = 2\n")

    manifest = build_worktree_ota_package(
        repo,
        tmp_path / "package",
        package_id="ota-working-test",
        include=("code.py",),
        created_at="2026-05-03T00:00:00Z",
    )

    assert manifest["package_id"] == "ota-working-test"
    assert manifest["source"] == "working-tree"
    assert manifest["to_tag"] == "working-tree"
    assert manifest["requires"]["version"] == "v0.26.123.13"
    assert manifest["files"][0]["path"] == "code.py"
    assert (tmp_path / "package" / "files" / "code.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"


def test_nodus_ota_cli_package_worktree_command(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "cpynodus_ii" / "__init__.py", '__version__ = "v0.26.123.13"\n')
    _write(repo / "code.py", "VALUE = 2\n")
    signing_key = _write_signing_key(tmp_path)

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
            "code.py",
            "--signing-key",
            str(signing_key),
        ]
    )

    assert exit_code == 0
    manifest = json.loads(
        (tmp_path / "package" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["package_id"] == "ota-working-test"
    assert [entry["path"] for entry in manifest["files"]] == ["code.py"]


def test_prepare_fwupdate_publishes_mqtt_prepare_payload():
    client = _FakeMqttClient()
    logs = []

    result = prepare_fwupdate(
        "broker.local",
        "co2-ph244",
        "ota-working-test",
        mqtt_client_factory=lambda: client,
        message_id="fw-1",
        session_id=TEST_SESSION,
        manifest_sha256="a" * 64,
        key_id="test-key",
        log_fn=logs.append,
    )

    assert result["topic"] == "nodus/co2-ph244/fwupdate"
    assert client.connected == ("broker.local", 1883, 60)
    assert client.disconnected is True
    assert client.published[0][0] == "nodus/co2-ph244/fwupdate"
    assert json.loads(client.published[0][1]) == {
        "schema": "nodus-fwupdate/v2",
        "message_id": "fw-1",
        "command": "prepare",
        "package_id": "ota-working-test",
        "session_id": TEST_SESSION,
        "manifest_sha256": "a" * 64,
        "key_id": "test-key",
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
        session_id=TEST_SESSION,
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
    assert (
        opener.requests[3][0].data == (package / "files" / "ota_test.py").read_bytes()
    )
    assert opener.requests[3][0].headers["X-nodus-file-path"] == "ota_test.py"
    assert logs[:3] == [
        "status http://10.0.0.213:8000 attempt=1",
        "begin package=ota-tagA-to-tagB files=1",
        "file ota_test.py bytes=22 chunk=1024",
    ]
    assert logs[3].startswith("chunk ota_test.py offset=22/22 elapsed_s=")
    assert logs[4].startswith("file accepted ota_test.py elapsed_s=")
    assert logs[5] == "commit"
    assert logs[6].startswith(
        "committed phase=applied_pending_boot rebooting=True delay_s=5 elapsed_s="
    )
    assert logs[7].startswith(
        "summary package=ota-tagA-to-tagB files=1 bytes=22 elapsed_s="
    )


def test_push_ota_package_rejects_device_package_mismatch(tmp_path):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-other")

    with pytest.raises(OTATransferError, match="device_package_mismatch"):
        push_ota_package(
            package,
            "http://10.0.0.213:8000",
            opener=opener,
            session_id=TEST_SESSION,
        )


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
            "--session-id",
            TEST_SESSION,
        ]
    )

    assert exit_code == 0
    assert "pushed ota-tagA-to-tagB files=1" in capsys.readouterr().out


def test_nodus_ota_cli_push_defaults_to_300_second_timeout(tmp_path, monkeypatch):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-tagA-to-tagB")
    monkeypatch.setattr(ota_package, "urlopen", opener)

    exit_code = ota_package.main(
        [
            "push",
            str(package),
            "--device",
            "http://10.0.0.213:8000",
            "--session-id",
            TEST_SESSION,
        ]
    )

    assert exit_code == 0
    assert [timeout for _request, timeout in opener.requests] == [
        5.0,
        300.0,
        300.0,
        300.0,
        300.0,
        300.0,
    ]


def test_nodus_ota_cli_prepare_push_defaults_to_ready_polling(tmp_path, monkeypatch):
    package = _write_test_package(tmp_path / "package")
    opener = _FakeOtaOpener(package_id="ota-tagA-to-tagB")
    mqtt_client = _FakeMqttClient()
    sleeps = []
    monkeypatch.setattr(ota_package, "urlopen", opener)
    monkeypatch.setattr(ota_package, "_paho_client_factory", lambda: mqtt_client)
    monkeypatch.setattr(ota_package.time, "sleep", lambda value: sleeps.append(value))

    exit_code = ota_package.main(
        [
            "push",
            str(package),
            "--prepare",
            "--broker",
            "10.0.0.220",
            "--device-id",
            "aht-yuk0nv",
            "--device",
            "http://10.0.0.213:8000",
        ]
    )

    assert exit_code == 0
    assert sleeps == []
    assert opener.requests[0][0].full_url == "http://10.0.0.213:8000/ota/status"


def test_push_ota_package_waits_for_status_reachable(tmp_path, monkeypatch):
    package = _write_test_package(tmp_path / "package")
    opener = _DelayedStatusOpener(package_id="ota-tagA-to-tagB", failures=2)
    sleeps = []
    logs = []
    monkeypatch.setattr(ota_package.time, "sleep", lambda value: sleeps.append(value))

    result = push_ota_package(
        package,
        "http://10.0.0.213:8000",
        timeout_s=300,
        opener=opener,
        log_fn=logs.append,
        ready_interval_s=1.5,
        session_id=TEST_SESSION,
    )

    assert result["package_id"] == "ota-tagA-to-tagB"
    assert sleeps == [1.5, 1.5]
    assert logs[:5] == [
        "status http://10.0.0.213:8000 attempt=1",
        "status retry reason=http_unreachable:[Errno 61] Connection refused",
        "status http://10.0.0.213:8000 attempt=2",
        "status retry reason=http_unreachable:[Errno 61] Connection refused",
        "status http://10.0.0.213:8000 attempt=3",
    ]


def test_push_ota_package_retries_lost_file_end_response(tmp_path, monkeypatch):
    package = _write_test_package(tmp_path / "package")
    opener = _FileEndResetOpener(package_id="ota-tagA-to-tagB")
    sleeps = []
    logs = []
    monkeypatch.setattr(ota_package.time, "sleep", lambda value: sleeps.append(value))

    result = push_ota_package(
        package,
        "http://10.0.0.213:8000",
        opener=opener,
        log_fn=logs.append,
        session_id=TEST_SESSION,
    )

    assert result["package_id"] == "ota-tagA-to-tagB"
    assert sleeps == [2.0]
    assert logs[4] == (
        "file end ota_test.py retry attempt=2 "
        "reason=http_failed:[Errno 54] Connection reset by peer"
    )
    file_end_requests = [
        request.full_url
        for request, _timeout in opener.requests
        if request.full_url.endswith("/ota/file/end?path=ota_test.py")
    ]
    assert file_end_requests == [
        "http://10.0.0.213:8000/ota/file/end?path=ota_test.py",
        "http://10.0.0.213:8000/ota/file/end?path=ota_test.py",
    ]


def test_push_ota_package_restarts_file_after_size_mismatch(tmp_path):
    package = _write_test_package(tmp_path / "package")
    opener = _FileEndMismatchOpener(package_id="ota-tagA-to-tagB")
    logs = []

    result = push_ota_package(
        package,
        "http://10.0.0.213:8000",
        opener=opener,
        log_fn=logs.append,
        session_id=TEST_SESSION,
    )

    assert result["package_id"] == "ota-tagA-to-tagB"
    assert "file retry ota_test.py reason=file_size_mismatch" in logs
    file_begin_requests = [
        request.full_url
        for request, _timeout in opener.requests
        if request.full_url.endswith("/ota/file/begin?path=ota_test.py")
    ]
    assert file_begin_requests == [
        "http://10.0.0.213:8000/ota/file/begin?path=ota_test.py",
        "http://10.0.0.213:8000/ota/file/begin?path=ota_test.py",
    ]


def test_push_ota_package_aborts_after_file_end_invalid_json_response(tmp_path):
    package = _write_test_package(tmp_path / "package")
    opener = _FileEndInvalidJsonOpener(package_id="ota-tagA-to-tagB")
    logs = []

    with pytest.raises(OTATransferError, match="http_invalid_json"):
        push_ota_package(
            package,
            "http://10.0.0.213:8000",
            opener=opener,
            log_fn=logs.append,
            session_id=TEST_SESSION,
        )

    abort_requests = [
        request.full_url
        for request, _timeout in opener.requests
        if request.full_url.endswith("/ota/abort")
    ]
    assert abort_requests == ["http://10.0.0.213:8000/ota/abort"]
    assert "abort reason=transfer_failed" in logs
    assert "abort complete phase=aborted" in logs


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


def _write_build_info(path, *, project_version):
    _write(
        path / "BUILD_INFO",
        "\n".join(
            (
                "format=cpynodus-mpy-build-v1",
                "target=pico2w",
                "circuitpython=9.2.8",
                "mpy_abi=mpy v6.3",
                "project_version={}".format(project_version),
                "",
            )
        ),
    )


def _write_signing_key(tmp_path):
    key = Path(tmp_path) / "ota-private.pem"
    subprocess.run(
        (
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ),
        check=True,
        capture_output=True,
    )
    return key


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
        "schema": "nodus-ota/v2",
        "package_id": "ota-tagA-to-tagB",
        "files": [
            {
                "path": "ota_test.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_path = path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (path / "manifest.sig").write_text(
        json.dumps(
            {
                "schema": "nodus-ota-signature/v1",
                "algorithm": "rsa-pkcs1v15-sha256",
                "key_id": "test-key",
                "manifest_sha256": manifest_sha256,
                "signature": "test-signature",
            }
        ),
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


class _RawResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


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
            return _FakeResponse({"accepted": True, "phase": "staging", "offset": 22})
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
        if url.endswith("/ota/abort"):
            return _FakeResponse({"accepted": True, "phase": "aborted"})
        return _FakeResponse({"accepted": False, "error": "unexpected_request"})


class _DelayedStatusOpener(_FakeOtaOpener):
    def __init__(self, *, package_id, failures):
        super().__init__(package_id=package_id)
        self.failures = int(failures)

    def __call__(self, request, timeout):
        if request.full_url.endswith("/ota/status") and self.failures > 0:
            self.failures -= 1
            self.requests.append((request, timeout))
            raise ota_package.URLError(OSError(61, "Connection refused"))
        return super().__call__(request, timeout)


class _FileEndResetOpener(_FakeOtaOpener):
    def __init__(self, *, package_id):
        super().__init__(package_id=package_id)
        self.failed_file_end = False

    def __call__(self, request, timeout):
        if (
            request.full_url.endswith("/ota/file/end?path=ota_test.py")
            and not self.failed_file_end
        ):
            self.failed_file_end = True
            self.requests.append((request, timeout))
            raise OSError(54, "Connection reset by peer")
        return super().__call__(request, timeout)


class _FileEndMismatchOpener(_FakeOtaOpener):
    def __init__(self, *, package_id):
        super().__init__(package_id=package_id)
        self.failed_file_end = False

    def __call__(self, request, timeout):
        if (
            request.full_url.endswith("/ota/file/end?path=ota_test.py")
            and not self.failed_file_end
        ):
            self.failed_file_end = True
            self.requests.append((request, timeout))
            return _FakeResponse(
                {
                    "accepted": False,
                    "phase": "staging",
                    "error": "file_size_mismatch",
                }
            )
        return super().__call__(request, timeout)


class _FileEndInvalidJsonOpener(_FakeOtaOpener):
    def __call__(self, request, timeout):
        if request.full_url.endswith("/ota/file/end?path=ota_test.py"):
            self.requests.append((request, timeout))
            return _RawResponse(b"not-json")
        return super().__call__(request, timeout)


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
