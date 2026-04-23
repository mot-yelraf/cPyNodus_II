"""Tests for reboot-log writes emitted by managed recovery reboots."""

from cpynodus_ii.app import _log_recovery_soft_reboot


def test_log_recovery_soft_reboot_skips_when_filesystem_not_writable(monkeypatch):
    calls = []

    def _fake_append(reason, *, header, path="/_reboot.log"):
        calls.append((reason, header, path))
        return True

    monkeypatch.setattr(
        "cpynodus_ii.app.append_reboot_reason_traceback",
        _fake_append,
    )

    written = _log_recovery_soft_reboot("mqtt_recovery_timeout", fs_writable=False)

    assert written is False
    assert calls == []


def test_log_recovery_soft_reboot_writes_when_filesystem_is_writable(monkeypatch):
    calls = []

    def _fake_append(reason, *, header, path="/_reboot.log"):
        calls.append((reason, header, path))
        return True

    monkeypatch.setattr(
        "cpynodus_ii.app.append_reboot_reason_traceback",
        _fake_append,
    )

    written = _log_recovery_soft_reboot("mqtt_recovery_timeout", fs_writable=True)

    assert written is True
    assert calls == [
        (
            "mqtt_recovery_timeout",
            "recovery soft reboot: mqtt_recovery_timeout",
            "/_reboot.log",
        )
    ]
