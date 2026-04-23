from pathlib import Path

from cpynodus_ii.core.reboot_log import (
    append_reboot_reason_traceback,
    append_reboot_traceback,
)


def test_append_reboot_traceback_writes_exception_details(tmp_path):
    log_path = tmp_path / "_reboot.log"

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        written = append_reboot_traceback(exc, path=str(log_path), header="unit-test")

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert "=== unit-test ===" in content
    assert "RuntimeError: boom" in content


def test_append_reboot_traceback_appends_entries(tmp_path):
    log_path = tmp_path / "_reboot.log"

    for message in ("first", "second"):
        try:
            raise ValueError(message)
        except ValueError as exc:
            assert append_reboot_traceback(exc, path=str(log_path), header=message) is True

    content = log_path.read_text(encoding="utf-8")
    assert content.count("=== ") == 2
    assert "ValueError: first" in content
    assert "ValueError: second" in content


def test_append_reboot_reason_traceback_writes_recovery_reason(tmp_path):
    log_path = tmp_path / "_reboot.log"

    written = append_reboot_reason_traceback(
        "mqtt_recovery_timeout",
        path=str(log_path),
        header="recovery soft reboot: mqtt_recovery_timeout",
    )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert "=== recovery soft reboot: mqtt_recovery_timeout ===" in content
    assert "RuntimeError: soft reboot requested: mqtt_recovery_timeout" in content


def test_append_reboot_traceback_trims_log_to_max_size(tmp_path):
    log_path = tmp_path / "_reboot.log"
    old_content = (
        "=== old-entry ===\n"
        + ("x" * 200)
        + "\n"
        + "=== recent-entry ===\n"
        + ("y" * 200)
        + "\n"
    )
    log_path.write_text(old_content, encoding="utf-8")

    try:
        raise RuntimeError("trim-me")
    except RuntimeError as exc:
        written = append_reboot_traceback(
            exc,
            path=str(log_path),
            header="new-entry",
            max_bytes=260,
        )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert len(content.encode("utf-8")) <= 260
    assert "=== new-entry ===" in content
    assert "RuntimeError: trim-me" in content
    assert "=== old-entry ===" not in content
