"""Tests for recovery diagnostic writes emitted by managed recovery."""

from cpynodus_ii.app import _log_recovery_event, _log_recovery_soft_reboot


def test_log_recovery_soft_reboot_skips_when_filesystem_not_writable(monkeypatch):
    calls = []

    def _fake_append(reason, *, header, device_id=None, path="/_reboot.log"):
        calls.append((reason, header, device_id, path))
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

    def _fake_append(reason, *, header, device_id=None, path="/_reboot.log"):
        calls.append((reason, header, device_id, path))
        return True

    monkeypatch.setattr(
        "cpynodus_ii.app.append_reboot_reason_traceback",
        _fake_append,
    )

    written = _log_recovery_soft_reboot(
        "mqtt_recovery_timeout",
        fs_writable=True,
        device_id="co2-ph244",
    )

    assert written is True
    assert calls == [
        (
            "mqtt_recovery_timeout",
            "recovery soft reboot: mqtt_recovery_timeout",
            "co2-ph244",
            "/_reboot.log",
        )
    ]


def test_log_recovery_event_skips_when_filesystem_not_writable(monkeypatch):
    calls = []

    def _fake_append(event, detail="", *, device_id=None, path="/_recovery.log"):
        calls.append((event, detail, device_id, path))
        return True

    monkeypatch.setattr(
        "cpynodus_ii.app.append_recovery_event",
        _fake_append,
    )

    written = _log_recovery_event(
        "phase_change",
        "previous=idle phase=mqtt",
        fs_writable=False,
    )

    assert written is False
    assert calls == []


def test_log_recovery_event_writes_when_filesystem_is_writable(monkeypatch):
    calls = []

    def _fake_append(event, detail="", *, device_id=None, path="/_recovery.log"):
        calls.append((event, detail, device_id, path))
        return True

    monkeypatch.setattr(
        "cpynodus_ii.app.append_recovery_event",
        _fake_append,
    )

    written = _log_recovery_event(
        "phase_change",
        "previous=idle phase=mqtt",
        fs_writable=True,
        device_id="soil-bd1234",
    )

    assert written is True
    assert calls == [
        (
            "phase_change",
            "previous=idle phase=mqtt",
            "soil-bd1234",
            "/_recovery.log",
        )
    ]
