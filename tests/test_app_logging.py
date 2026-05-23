"""Regression tests for application logging and reboot-side effects."""

import sys
from types import SimpleNamespace

import pytest

import cpynodus_ii.app as app_module
from cpynodus_ii.app import (
    _clear_recovery_hard_reset_marker,
    _consume_soft_reload_cleanup_marker,
    _consume_soft_reload_prepared,
    _cycle_wifi_radio_for_warm_start,
    _dns_health_text,
    _hard_reboot,
    _mark_recovery_hard_reset_requested,
    _mark_soft_reload_cleanup_requested,
    _mark_soft_reload_prepared,
    _mark_unprepared_shutdown_cleanup,
    _mqtt_disconnect_reason_requires_rebuild,
    _ntp_health_text,
    _recovery_hard_reset_marker_is_set,
    _recovery_reboot_kind,
    _runtime_restart_kind,
    _sensor_error_text,
    _sensor_issue_text,
    _should_log_command_result,
    _soft_reboot,
    _teardown_network_for_shutdown,
)
from cpynodus_ii.core.config import RuntimeConfig


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


def test_soft_reboot_can_wait_before_reload(monkeypatch, capsys):
    sleeps = []

    class _Supervisor:
        @staticmethod
        def reload():
            return None

    monkeypatch.setitem(pytest.importorskip("sys").modules, "supervisor", _Supervisor)
    monkeypatch.setattr(app_module.time, "sleep", lambda delay: sleeps.append(delay))

    _soft_reboot(
        reason="recovery:mqtt_repeated_connect_failures",
        start_monotonic=0.0,
        settle_s=1.0,
    )

    captured = capsys.readouterr()
    assert sleeps == [1.0]
    assert (
        "runtime action=reload_wait "
        "reason=recovery:mqtt_repeated_connect_failures delay_s=1.0"
    ) in captured.out
    assert (
        "runtime action=reload reason=recovery:mqtt_repeated_connect_failures"
        in captured.out
    )
    assert captured.out.index("reload_wait") < captured.out.index("action=reload ")


def test_soft_reboot_marks_next_boot_cleanup(monkeypatch, capsys):
    nvm = bytearray(4)

    class _Supervisor:
        @staticmethod
        def reload():
            return None

    monkeypatch.setitem(sys.modules, "microcontroller", SimpleNamespace(nvm=nvm))
    monkeypatch.setitem(sys.modules, "supervisor", _Supervisor)

    _soft_reboot(reason="web:restart", start_monotonic=0.0)

    captured = capsys.readouterr()
    assert nvm[app_module.SOFT_RELOAD_CLEANUP_NVM_INDEX] == (
        app_module.SOFT_RELOAD_CLEANUP_MARKER
    )
    assert "runtime action=reload_prepare reason=web:restart marker=set" in captured.out


def test_soft_reload_cleanup_marker_consumed_once():
    nvm = bytearray(4)

    assert _mark_soft_reload_cleanup_requested(nvm) is True
    assert _consume_soft_reload_cleanup_marker(nvm) is True
    assert nvm[app_module.SOFT_RELOAD_CLEANUP_NVM_INDEX] == 0
    assert _consume_soft_reload_cleanup_marker(nvm) is False


def test_unprepared_shutdown_marks_next_boot_cleanup(monkeypatch, capsys):
    nvm = bytearray(4)

    monkeypatch.setitem(sys.modules, "microcontroller", SimpleNamespace(nvm=nvm))

    assert _mark_unprepared_shutdown_cleanup(0.0, reason="finally") is True

    captured = capsys.readouterr()
    assert nvm[app_module.SOFT_RELOAD_CLEANUP_NVM_INDEX] == (
        app_module.SOFT_RELOAD_CLEANUP_MARKER
    )
    assert "runtime shutdown_prepare reason=finally marker=set" in captured.out


def test_warm_start_cleanup_resets_station_without_power_cycle(monkeypatch, capsys):
    sleeps = []

    class _Radio:
        def __init__(self):
            self.calls = []
            self.enabled_changes = []

        def disconnect(self):
            self.calls.append("disconnect")

        def stop_station(self):
            self.calls.append("stop_station")

        def stop_ap(self):
            self.calls.append("stop_ap")

        def start_station(self):
            self.calls.append("start_station")

        @property
        def enabled(self):
            return True

        @enabled.setter
        def enabled(self, value):
            self.enabled_changes.append(bool(value))

    radio = _Radio()

    monkeypatch.setitem(sys.modules, "wifi", SimpleNamespace(radio=radio))
    monkeypatch.setattr(app_module.time, "sleep", lambda delay: sleeps.append(delay))

    assert _cycle_wifi_radio_for_warm_start(0.0) is True

    captured = capsys.readouterr()
    assert "runtime warm_start_cleanup phase=start" in captured.out
    assert "runtime warm_start_cleanup phase=done" in captured.out
    assert radio.calls == ["stop_ap", "disconnect", "stop_station", "start_station"]
    assert "cycle_radio=0" in captured.out
    assert radio.enabled_changes == []
    assert sleeps == [app_module.WARM_START_RADIO_SETTLE_S]


def test_soft_reload_prepared_flag_is_consumed_once():
    assert _consume_soft_reload_prepared() is False

    _mark_soft_reload_prepared()

    assert _consume_soft_reload_prepared() is True
    assert _consume_soft_reload_prepared() is False


def test_hard_reboot_logs_reset_reason_before_reset(monkeypatch, capsys):
    class _Microcontroller:
        @staticmethod
        def reset():
            return None

    monkeypatch.setitem(
        pytest.importorskip("sys").modules,
        "microcontroller",
        _Microcontroller,
    )

    _hard_reboot(reason="recovery:mqtt_recovery_timeout", start_monotonic=0.0)

    captured = capsys.readouterr()
    assert "runtime action=reset reason=recovery:mqtt_recovery_timeout" in captured.out


def test_shutdown_network_teardown_logs_and_stops_radio(capsys):
    radio = SimpleNamespace(
        disconnect=lambda: None,
        stop_station=lambda: None,
        stop_ap=lambda: None,
    )
    network_stack = SimpleNamespace(wifi_radio=radio)

    assert (
        _teardown_network_for_shutdown(network_stack, start_monotonic=0.0)
        is True
    )

    captured = capsys.readouterr()
    assert "runtime network action=teardown result=1" in captured.out


def test_shutdown_network_teardown_can_cycle_radio(monkeypatch, capsys):
    from cpynodus_ii.core import network as network_module

    monkeypatch.setattr(network_module.time, "sleep", lambda _seconds: None)

    class _Radio:
        def __init__(self):
            self.enabled_changes = []

        def disconnect(self):
            return None

        def stop_station(self):
            return None

        def stop_ap(self):
            return None

        @property
        def enabled(self):
            return True

        @enabled.setter
        def enabled(self, value):
            self.enabled_changes.append(bool(value))

    radio = _Radio()
    network_stack = SimpleNamespace(wifi_radio=radio)

    assert (
        _teardown_network_for_shutdown(
            network_stack,
            start_monotonic=0.0,
            cycle_radio=True,
        )
        is True
    )

    captured = capsys.readouterr()
    assert "runtime network action=teardown result=1 cycle_radio=1" in captured.out
    assert radio.enabled_changes == [False, True]


def test_recovery_escalations_use_hard_reset_when_filesystem_is_read_only():
    assert _recovery_reboot_kind("ap_idle_timeout") == "hard"
    assert _recovery_reboot_kind("mqtt_recovery_timeout") == "hard"
    assert _recovery_reboot_kind("sensor_not_found") == "hard"
    assert _recovery_reboot_kind("wifi_after_ready_failure") == "hard"
    assert _recovery_reboot_kind("wifi_recovery_timeout") == "hard"


def test_persistent_mqtt_connect_failures_hard_reset_even_on_read_only_fs():
    assert _recovery_reboot_kind("mqtt_repeated_connect_failures") == "hard"
    assert (
        _recovery_reboot_kind("mqtt_repeated_connect_failures", fs_writable=False)
        == "hard"
    )


def test_recovery_hard_reset_is_not_gated_by_writable_filesystem():
    assert (
        _recovery_reboot_kind(
            "mqtt_repeated_connect_failures",
            fs_writable=True,
        )
        == "hard"
    )
    assert _recovery_reboot_kind("mqtt_recovery_timeout", fs_writable=True) == "hard"
    assert (
        _recovery_reboot_kind(
            "mqtt_memory_allocation_failures",
            fs_writable=True,
        )
        == "hard"
    )
    assert (
        _recovery_reboot_kind("wifi_after_ready_failure", fs_writable=True)
        == "hard"
    )
    assert _recovery_reboot_kind("wifi_recovery_timeout", fs_writable=True) == "hard"


def test_runtime_restart_promotes_soft_reload_for_mqtt_profiles():
    assert _runtime_restart_kind(RuntimeConfig(active_profile="sensorius")) == "hard"
    assert _runtime_restart_kind(RuntimeConfig(active_profile="weewx")) == "hard"
    assert (
        _runtime_restart_kind(RuntimeConfig(active_profile="homeassistant"))
        == "hard"
    )
    assert _runtime_restart_kind(RuntimeConfig(active_profile="nodusweb")) == "soft"
    assert (
        _runtime_restart_kind(
            RuntimeConfig(active_profile="nodusweb"),
            requested_kind="hard",
        )
        == "hard"
    )


def test_recovery_hard_reset_marker_is_one_shot_for_mqtt_connect_failures():
    nvm = bytearray(4)

    assert _recovery_hard_reset_marker_is_set(
        "mqtt_repeated_connect_failures",
        nvm,
    ) is False
    assert _mark_recovery_hard_reset_requested(
        "mqtt_repeated_connect_failures",
        nvm,
    ) is True
    assert _recovery_hard_reset_marker_is_set(
        "mqtt_repeated_connect_failures",
        nvm,
    ) is True
    assert _clear_recovery_hard_reset_marker(
        "mqtt_repeated_connect_failures",
        nvm,
    ) is True
    assert _recovery_hard_reset_marker_is_set(
        "mqtt_repeated_connect_failures",
        nvm,
    ) is False


def test_mqtt_connect_repeated_failures_force_station_reset_rebuild():
    assert (
        _mqtt_disconnect_reason_requires_rebuild(
            "mqtt_connect_failed:10.0.0.248:('Repeated connect failures', None)"
        )
        is True
    )
    assert _mqtt_disconnect_reason_requires_rebuild("mqtt_connect_failed:5") is False
