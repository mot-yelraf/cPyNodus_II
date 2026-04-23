from types import SimpleNamespace

import pytest

from cpynodus_ii.app import _should_log_command_result, _soft_reboot


def test_should_log_command_result_skips_switch_commands():
    result = SimpleNamespace(phase="published", command_type="switch")

    assert _should_log_command_result(result) is False


def test_should_log_command_result_keeps_non_switch_commands():
    result = SimpleNamespace(phase="published", command_type="config")

    assert _should_log_command_result(result) is True


def test_soft_reboot_logs_reload_reason_before_reload(monkeypatch, capsys):
    class _Supervisor:
        @staticmethod
        def reload():
            return None

    monkeypatch.setitem(pytest.importorskip("sys").modules, "supervisor", _Supervisor)

    _soft_reboot(reason="recovery:mqtt_recovery_timeout", start_monotonic=0.0)

    captured = capsys.readouterr()
    assert "runtime action=reload reason=recovery:mqtt_recovery_timeout" in captured.out
