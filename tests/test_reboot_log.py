"""Tests for bounded reboot-log append and trim behavior.

The cases verify compact record formatting, size bounds, and resilient file
handling for reboot diagnostics.
"""

from cpynodus_ii import __version__
from cpynodus_ii.core.reboot_log import (
    append_reboot_reason_traceback,
    append_reboot_traceback,
)


def test_append_reboot_traceback_writes_exception_details(tmp_path):
    log_path = tmp_path / "_reboot.log"

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        written = append_reboot_traceback(
            exc,
            path=str(log_path),
            header="unit-test",
            device_id="unit-device",
        )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert "=== unit-test | ts=" in content
    assert " | version={} | device=unit-device ===".format(__version__) in content
    assert "RuntimeError: boom" in content


def test_append_reboot_traceback_appends_entries(tmp_path):
    log_path = tmp_path / "_reboot.log"

    for message in ("first", "second"):
        try:
            raise ValueError(message)
        except ValueError as exc:
            assert (
                append_reboot_traceback(
                    exc,
                    path=str(log_path),
                    header=message,
                    device_id="unit-device",
                )
                is True
            )

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
        device_id="co2-ph244",
    )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert "=== recovery soft reboot: mqtt_recovery_timeout | ts=" in content
    assert " | version={} | device=co2-ph244 ===".format(__version__) in content
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
            device_id="unit-device",
            max_bytes=360,
        )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert len(content.encode("utf-8")) <= 360
    assert "=== new-entry | ts=" in content
    assert "RuntimeError: trim-me" in content
    assert "=== old-entry ===" not in content


def test_append_reboot_traceback_trims_with_tuple_stat(tmp_path, monkeypatch):
    log_path = tmp_path / "_reboot.log"
    old_content = "=== old-entry ===\n" + ("x" * 400) + "\n"
    log_path.write_text(old_content, encoding="utf-8")

    from cpynodus_ii.core import reboot_log

    real_os = reboot_log.os
    real_stat = real_os.stat

    class _TupleStatOs:
        def stat(self, path):
            stat_result = real_stat(path)
            return (0, 0, 0, 0, 0, 0, stat_result.st_size, 0, 0, 0)

    monkeypatch.setattr(reboot_log, "os", _TupleStatOs())

    try:
        raise RuntimeError("tuple-stat")
    except RuntimeError as exc:
        written = append_reboot_traceback(
            exc,
            path=str(log_path),
            header="new-entry",
            device_id="unit-device",
            max_bytes=400,
        )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert len(content.encode("utf-8")) <= 400
    assert "=== new-entry | ts=" in content
    assert "RuntimeError: tuple-stat" in content
    assert "=== old-entry ===" not in content


def test_append_reboot_traceback_uses_live_config_device_id(tmp_path, monkeypatch):
    log_path = tmp_path / "_reboot.log"
    sensor_path = tmp_path / "sensor_i2c.toml"
    sensor_path.write_text(
        '[Sensor]\nSENSOR_ID = "co2-ph244"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    try:
        raise RuntimeError("configured-device")
    except RuntimeError as exc:
        written = append_reboot_traceback(
            exc,
            path=str(log_path),
            header="configured",
        )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert " | device=co2-ph244 ===" in content
