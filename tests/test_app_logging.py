"""Regression tests for application logging and reboot-side effects."""

from types import SimpleNamespace

import pytest

from cpynodus_ii.app import (
    _dns_health_text,
    _ntp_health_text,
    _sensor_error_text,
    _sensor_issue_text,
    _should_log_command_result,
    _soft_reboot,
)


def test_should_log_command_result_skips_switch_commands():
    result = SimpleNamespace(phase="published", command_type="switch")

    assert _should_log_command_result(result) is False


def test_should_log_command_result_keeps_non_switch_commands():
    result = SimpleNamespace(phase="published", command_type="config")

    assert _should_log_command_result(result) is True


def test_sensor_error_text_combines_startup_errors_without_duplicates():
    runtime = SimpleNamespace(errors=("sensor_not_found",))
    snapshot = SimpleNamespace(errors=("sensor_metrics_empty", "sensor_not_found"))

    assert (
        _sensor_error_text(runtime, snapshot) == "sensor_not_found,sensor_metrics_empty"
    )


def test_sensor_issue_text_omits_normal_poll_skip():
    assert _sensor_issue_text(("sensor_poll_interval_not_elapsed",)) == ""
    assert _sensor_issue_text(("sensor_metrics_empty",)) == "sensor_metrics_empty"


def test_dns_health_reports_existing_resolver_failures():
    network_stack = SimpleNamespace(phase="ready", socket_pool=object())
    mqtt_adapter = SimpleNamespace(
        errors=("mqtt_resolve_failed:homeassistant.local:-2",)
    )
    ntp_state = SimpleNamespace(errors=())

    assert _dns_health_text(network_stack, mqtt_adapter, ntp_state) == "error"


def test_dns_health_reports_ok_when_network_ready_without_dns_errors():
    network_stack = SimpleNamespace(phase="ready", socket_pool=object())
    mqtt_adapter = SimpleNamespace(errors=())
    ntp_state = SimpleNamespace(errors=())

    assert _dns_health_text(network_stack, mqtt_adapter, ntp_state) == "ok"


def test_ntp_health_uses_state_phase_or_idle():
    assert _ntp_health_text(SimpleNamespace(phase="synced")) == "synced"
    assert _ntp_health_text(SimpleNamespace(phase="")) == "idle"


def test_soft_reboot_logs_reload_reason_before_reload(monkeypatch, capsys):
    class _Supervisor:
        @staticmethod
        def reload():
            return None

    monkeypatch.setitem(pytest.importorskip("sys").modules, "supervisor", _Supervisor)

    _soft_reboot(reason="recovery:mqtt_recovery_timeout", start_monotonic=0.0)

    captured = capsys.readouterr()
    assert "runtime action=reload reason=recovery:mqtt_recovery_timeout" in captured.out
