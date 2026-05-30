"""Tests for startup planning, AP fallback, and settings boot behavior."""

from dataclasses import replace
from types import SimpleNamespace

import cpynodus_ii.app as app_module
from cpynodus_ii.app import (
    _broker_ip_refresh_needed,
    _defer_switch_subscriptions_until_after_startup_publish,
    _increase_mqtt_preflight_connect_delay,
    _is_mqtt_subscription_failure,
    _load_settings_for_startup,
    _load_startup_ota_state,
    _mark_ota_applied_after_boot,
    _maybe_reboot_for_mqtt_failure_budget,
    _mqtt_client_init_memory_failed,
    _mqtt_connect_attempt_is_connack_timeout_pattern,
    _mqtt_connect_errors_are_repeated_failures,
    _mqtt_connect_retry_interval_s,
    _mqtt_error_indicates_socket_progress,
    _mqtt_preconnect_has_socket_progress,
    _mqtt_preconnect_probe,
    _mqtt_repeated_failure_budget_exhausted,
    _mqtt_repeated_failure_elapsed_s,
    _mqtt_repeated_failure_reboot_gate_tripped,
    _mqtt_startup_queues_clear,
    _mqtt_sync_should_log_success,
    _ota_state_path,
    _recovery_reconnect_attempts,
    _recovery_reconnect_delay_s,
    _refresh_broker_ip_from_hostname,
    _reset_mqtt_preflight_connect_delay,
    _resolve_startup_plan,
    _restart_sensor_stack,
    _runtime_device_id,
    _sensor_driver_start_deferred,
    _sensor_errors_indicate_not_found,
    _should_enter_ota_mode,
    _should_fallback_to_ap,
    _should_fast_reboot_mqtt_connect_failures,
    _should_fast_reboot_wifi_after_ready,
    _should_fast_reset_wifi_station,
    _should_keep_mqtt_station_reset_after_verify,
    _should_log_wifi_failure_signature,
    _should_reboot_long_mqtt_recovery,
    _should_reboot_mqtt_memory_failures,
    _should_reboot_sensor_not_found,
    _should_rebuild_mqtt_adapter_for_recovery,
    _should_reset_mqtt_station_for_plain_connect_failure,
    _should_reset_wifi_station_before_ready,
    _should_stage_mqtt_station_reset_before_reboot,
    _should_verify_mqtt_before_rebuild,
    _startup_ap_fallback_reason,
    _startup_conditioning_enabled_for_current_run,
    _startup_subscription_recovery_drained,
    _update_mqtt_memory_failure_window,
    _update_sensor_not_found_window,
    _wifi_signature_text,
    _wifi_station_reset_reason,
)
from cpynodus_ii.core import RecoveryState
from cpynodus_ii.core.config import (
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.ota.state import FwUpdateState, save_ota_state


def test_resolve_startup_plan_allows_test_override_for_sensorius_without_web():
    runtime_config = RuntimeConfig(active_profile="sensorius")

    plan = _resolve_startup_plan(
        runtime_config,
        startup_plan_override=lambda base_plan: replace(base_plan, web_enabled=False),
    )

    assert plan.profile == "sensorius"
    assert plan.mqtt_enabled is True
    assert plan.web_enabled is False
    assert plan.ntp_enabled is True


def test_resolve_startup_plan_keeps_default_behavior_without_override():
    runtime_config = RuntimeConfig(active_profile="sensorius")

    plan = _resolve_startup_plan(runtime_config)

    assert plan.profile == "sensorius"
    assert plan.web_enabled is False


def test_default_startup_defers_switch_subscriptions_until_after_publishes():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        switch=SwitchConfig(present=True),
    )

    assert (
        _defer_switch_subscriptions_until_after_startup_publish(runtime_config)
        is True
    )


def test_mqtt_sync_should_log_successful_subscription_batches():
    sync_result = SimpleNamespace(
        phase="synced",
        operation="subscribe",
        subscribed_count=5,
        diagnostic="",
    )

    assert _mqtt_sync_should_log_success(sync_result) is True


def test_detects_mqtt_subscription_sync_failure():
    subscribe_failure = SimpleNamespace(
        phase="error",
        errors=("mqtt_subscribe_failed:topic=nodus/device/config/set:index=0/3:",),
    )
    publish_failure = SimpleNamespace(
        phase="error",
        errors=("mqtt_publish_failed:nodus/device/data:[Errno 5]",),
    )

    assert _is_mqtt_subscription_failure(subscribe_failure) is True
    assert _is_mqtt_subscription_failure(publish_failure) is False


def test_subscription_recovery_drained_requires_empty_subscription_queue():
    drained = SimpleNamespace(phase="synced", subscribed_count=3)
    not_drained = SimpleNamespace(phase="synced", subscribed_count=1)

    assert (
        _startup_subscription_recovery_drained(
            drained,
            SimpleNamespace(subscriptions=[]),
        )
        is True
    )
    assert (
        _startup_subscription_recovery_drained(
            not_drained,
            SimpleNamespace(subscriptions=["nodus/device/config/set"]),
        )
        is False
    )


def test_refresh_broker_ip_persists_resolved_hostname_on_startup(tmp_path):
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "samhain.local"
            return [(None, None, None, None, ("10.0.0.248", port))]

    (tmp_path / "settings.toml").write_text(
        "[Network]\n"
        'SSID = "PeaceHill"\n'
        'PASSWORD = "plain-wifi"\n'
        'HOSTNAME = "co2-29j39c"\n'
        "[Profile]\n"
        'ACTIVE_PROFILE = "sensorius"\n'
        "[MQTT]\n"
        'BROKER = "samhain.local"\n'
        'BROKER_IP = ""\n'
        "PORT = 1883\n",
        encoding="utf-8",
    )
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="plain-wifi",
            hostname="co2-29j39c",
        ),
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )
    network_stack = SimpleNamespace(socket_pool=_ResolveOKPool())

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        network_stack,
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "persisted"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"
    assert 'BROKER_IP = "10.0.0.248"' in text


def test_refresh_broker_ip_updates_changed_hostname_resolution(tmp_path):
    class _ResolveChangedPool:
        def getaddrinfo(self, host, port):
            assert host == "samhain.local"
            return [(None, None, None, None, ("10.0.0.220", port))]

    (tmp_path / "settings.toml").write_text(
        "[Network]\n"
        'SSID = "PeaceHill"\n'
        'PASSWORD = "plain-wifi"\n'
        'HOSTNAME = "co2-29j39c"\n'
        "[Profile]\n"
        'ACTIVE_PROFILE = "sensorius"\n'
        "[MQTT]\n"
        'BROKER = "samhain.local"\n'
        'BROKER_IP = "10.0.0.248"\n'
        "PORT = 1883\n",
        encoding="utf-8",
    )
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="plain-wifi",
            hostname="co2-29j39c",
        ),
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        SimpleNamespace(socket_pool=_ResolveChangedPool()),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "persisted"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.220"
    assert 'BROKER_IP = "10.0.0.220"' in text


def test_mqtt_rebuild_verification_uses_recent_success_in_initial_recovery():
    transport = MQTTTransport("samhain.local", 1883)
    transport.mark_connected(now_monotonic=50.0)
    transport.mark_disconnected(now_monotonic=61.0)

    should_verify = _should_verify_mqtt_before_rebuild(
        transport,
        RecoveryState(phase="mqtt", phase_started_at=60.0),
        62.0,
        success_window_s=90.0,
        phase_window_s=10.0,
    )

    assert should_verify is True


def test_refresh_broker_ip_uses_volatile_resolution_without_settings_root():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "samhain.local"
            return [(None, None, None, None, ("10.0.0.248", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        SimpleNamespace(socket_pool=_ResolveOKPool()),
        settings_root=None,
    )

    assert phase == "resolved_volatile"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"


def test_refresh_broker_ip_keeps_configured_ip_when_hostname_resolution_fails():
    class _ResolveFailPool:
        def getaddrinfo(self, host, port):
            raise OSError(-2)

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        SimpleNamespace(socket_pool=_ResolveFailPool()),
        settings_root=None,
    )

    assert phase == "error"
    assert "mqtt_resolve_failed:samhain.local" in errors[0]
    assert updated_runtime is runtime_config
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"


def test_broker_ip_refresh_skips_writable_startup_when_ip_is_configured(tmp_path):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=tmp_path) is False


def test_broker_ip_refresh_skips_rofs_when_ip_is_configured():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=None) is False


def test_broker_ip_refresh_needed_for_rofs_without_configured_ip():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=None) is True


def test_wifi_failure_signature_logging_is_sparse():
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 1, False)
    assert not _should_log_wifi_failure_signature(
        "station_scan_miss_after_ready", 2, False
    )
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 3, False)
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 5, True)
    assert not _should_log_wifi_failure_signature("none", 1, True)


def test_wifi_signature_text_reports_ready_for_health_log():
    assert _wifi_signature_text(SimpleNamespace(phase="ready")) == "ready"
    assert (
        _wifi_signature_text(
            SimpleNamespace(
                phase="error",
                errors=("No network with that ssid",),
            ),
            had_ready_link=True,
        )
        == "station_scan_miss_after_ready"
    )


def test_runtime_device_id_prefers_sensor_then_switch_then_hostname():
    assert (
        _runtime_device_id(
            SimpleNamespace(
                sensor=SimpleNamespace(sensor_id="aqi-wfcp7p"),
                switch=SimpleNamespace(device_id="switch-wfcp7p"),
                network=SimpleNamespace(hostname="nodus-wfcp7p"),
            )
        )
        == "aqi-wfcp7p"
    )
    assert (
        _runtime_device_id(
            SimpleNamespace(
                sensor=SimpleNamespace(sensor_id=""),
                switch=SimpleNamespace(device_id="switch-wfcp7p"),
                network=SimpleNamespace(hostname="nodus-wfcp7p"),
            )
        )
        == "switch-wfcp7p"
    )
    assert _runtime_device_id(SimpleNamespace()) == "cPyNodus_II"


def test_after_ready_wifi_failures_reset_station_early():
    assert not _should_fast_reset_wifi_station("station_scan_miss_after_ready", 1)
    assert _should_fast_reset_wifi_station("station_scan_miss_after_ready", 2)
    assert _should_fast_reset_wifi_station("station_unknown_after_ready", 2)
    assert not _should_fast_reset_wifi_station("station_scan_miss", 5)


def test_after_ready_wifi_failures_reboot_after_bounded_window():
    assert not _should_fast_reboot_wifi_after_ready(
        "station_scan_miss_after_ready",
        2,
        90.0,
    )
    assert not _should_fast_reboot_wifi_after_ready(
        "station_scan_miss_after_ready",
        3,
        89.0,
    )
    assert _should_fast_reboot_wifi_after_ready(
        "station_unknown_after_ready",
        3,
        90.0,
    )
    assert not _should_fast_reboot_wifi_after_ready("station_scan_miss", 10, 120.0)


def test_mqtt_rebuild_verification_skips_socket_poll_failures():
    transport = MQTTTransport("samhain.local", 1883)
    transport.mark_connected(now_monotonic=50.0)
    transport.mark_disconnected(
        now_monotonic=61.0,
        reason=(
            "mqtt_poll_failed:minimqtt_socket:sock=wrapped backcompat=0 "
            "error=function takes 3 positional arguments but 2 were given"
        ),
    )

    should_verify = _should_verify_mqtt_before_rebuild(
        transport,
        RecoveryState(phase="mqtt", phase_started_at=60.0),
        62.0,
        success_window_s=90.0,
        phase_window_s=10.0,
    )

    assert should_verify is False


def test_mqtt_rebuild_verification_skips_publish_failures():
    transport = MQTTTransport("samhain.local", 1883)
    transport.mark_connected(now_monotonic=50.0)
    transport.mark_disconnected(
        now_monotonic=61.0,
        reason="mqtt_publish_failed:nodus/S2-ykdvea/availability:[Errno 5]",
    )

    should_verify = _should_verify_mqtt_before_rebuild(
        transport,
        RecoveryState(phase="mqtt", phase_started_at=60.0),
        62.0,
        success_window_s=90.0,
        phase_window_s=10.0,
    )

    assert should_verify is False


def test_repeated_mqtt_connect_failure_detection_matches_connect_errors():
    repeated_errors = (
        "mqtt_connect_failed:10.0.0.248:('Repeated connect failures', None)",
    )
    plain_errors = ("mqtt_connect_failed:10.0.0.248:('Connect failure', None)",)

    assert _mqtt_connect_errors_are_repeated_failures(repeated_errors) is True
    assert _mqtt_connect_errors_are_repeated_failures(plain_errors) is False
    assert (
        _mqtt_connect_errors_are_repeated_failures(
            plain_errors,
            had_mqtt_success=True,
        )
        is True
    )
    assert (
        _mqtt_connect_errors_are_repeated_failures(
            plain_errors,
            count_plain_connect_failures=True,
        )
        is True
    )
    assert _mqtt_connect_errors_are_repeated_failures(("mqtt_poll_failed:5",)) is False


def test_mqtt_connack_timeout_pattern_requires_tcp_ok_probe_timeout_and_connect_error():
    plain_errors = ("mqtt_connect_failed:10.0.0.248:('Connect failure', None)",)

    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="",
            connect_probe_error=(
                "mqtt_connect_probe_failed:10.0.0.248:OSError:[Errno 116] ETIMEDOUT"
            ),
            connect_probe_code=-1,
            connect_errors=plain_errors,
        )
        is True
    )
    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="mqtt_tcp_preflight_failed:10.0.0.248:timeout",
            connect_probe_error=(
                "mqtt_connect_probe_failed:10.0.0.248:OSError:[Errno 116] ETIMEDOUT"
            ),
            connect_probe_code=-1,
            connect_errors=plain_errors,
        )
        is False
    )
    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="",
            connect_probe_error="",
            connect_probe_code=0,
            connect_errors=plain_errors,
        )
        is False
    )
    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="",
            connect_probe_error="",
            connect_probe_code=-1,
            connect_errors=(
                "mqtt_connect_failed:10.0.0.248:raw:OSError:[Errno 116] ETIMEDOUT",
            ),
        )
        is True
    )
    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="",
            connect_probe_error=("mqtt_connect_probe_failed:10.0.0.248:connack_code=5"),
            connect_probe_code=5,
            connect_errors=plain_errors,
        )
        is False
    )
    assert (
        _mqtt_connect_attempt_is_connack_timeout_pattern(
            tcp_preflight_error="",
            connect_probe_error=(
                "mqtt_connect_probe_failed:10.0.0.248:OSError:[Errno 116] ETIMEDOUT"
            ),
            connect_probe_code=-1,
            connect_errors=("mqtt_poll_failed:[Errno 116]",),
        )
        is False
    )


def test_mqtt_socket_progress_pattern_matches_errno_119_only():
    assert (
        _mqtt_error_indicates_socket_progress(
            "mqtt_tcp_preflight_failed:10.0.0.248:OSError:[Errno 119] EINPROGRESS"
        )
        is True
    )
    assert (
        _mqtt_error_indicates_socket_progress(
            "mqtt_connect_probe_failed:10.0.0.248:OSError:Operation now in progress"
        )
        is True
    )
    assert (
        _mqtt_error_indicates_socket_progress(
            "mqtt_connect_probe_failed:10.0.0.248:OSError:[Errno 116] ETIMEDOUT"
        )
        is False
    )
    assert (
        _mqtt_preconnect_has_socket_progress(
            tcp_preflight_error="",
            connect_probe_error="mqtt_connect_probe_failed:host:OSError:EINPROGRESS",
        )
        is True
    )


def test_mqtt_preconnect_probe_retries_socket_progress_before_success(monkeypatch):
    tcp_calls = []
    connect_calls = []
    sleeps = []

    def fake_tcp(_adapter):
        tcp_calls.append(1)
        if len(tcp_calls) == 1:
            return (
                "mqtt_tcp_preflight_failed:10.0.0.4:OSError:[Errno 119] EINPROGRESS",
                "10.0.0.4",
            )
        return "", "10.0.0.4"

    def fake_connect(_adapter):
        connect_calls.append(1)
        return "", "10.0.0.4", "aqi-wfcp7p", 0

    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_RETRIES", 3)
    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_RETRY_DELAY_S", 0.5)
    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_CONNECT_DELAY_S", 0.5)
    monkeypatch.setattr(app_module, "preflight_mqtt_broker_tcp", fake_tcp)
    monkeypatch.setattr(app_module, "preflight_mqtt_broker_connect", fake_connect)
    monkeypatch.setattr(app_module.time, "sleep", lambda delay: sleeps.append(delay))

    result = _mqtt_preconnect_probe(
        SimpleNamespace(active_broker="10.0.0.4", broker="samhain.local", port=1883),
        SimpleNamespace(socket_artifact_source="direct"),
        start_monotonic=0.0,
    )

    assert result[0] == ""
    assert result[3] == ""
    assert len(tcp_calls) == 2
    assert len(connect_calls) == 1
    assert sleeps == [0.5, 0.5]


def test_mqtt_preconnect_probe_checks_connack_before_minimqtt_connect(monkeypatch):
    tcp_calls = []
    connect_calls = []
    sleeps = []

    class _SocketPool:
        def socket(self):
            return object()

    def fake_tcp(_adapter):
        tcp_calls.append(1)
        return "", "10.0.0.4"

    def fake_connect(_adapter):
        connect_calls.append(1)
        return "", "10.0.0.4", "aqi-wfcp7p", 0

    monkeypatch.setattr(app_module, "preflight_mqtt_broker_tcp", fake_tcp)
    monkeypatch.setattr(app_module, "preflight_mqtt_broker_connect", fake_connect)
    monkeypatch.setattr(app_module.time, "sleep", lambda delay: sleeps.append(delay))

    result = _mqtt_preconnect_probe(
        SimpleNamespace(
            active_broker="10.0.0.4",
            broker="samhain.local",
            port=1883,
            socket_compat_enabled=True,
            client_kwargs={"socket_pool": _SocketPool()},
        ),
        SimpleNamespace(socket_artifact_source="direct"),
        start_monotonic=0.0,
    )

    assert result[0] == ""
    assert result[3] == ""
    assert result[5] == "aqi-wfcp7p"
    assert result[6] == 0
    assert len(tcp_calls) == 1
    assert len(connect_calls) == 1
    assert sleeps == [5.0]


def test_mqtt_preconnect_delay_adapts_after_connect_failures(monkeypatch):
    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_CONNECT_DELAY_S", 5.0)
    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_CONNECT_DELAY_STEP_S", 3.0)
    monkeypatch.setattr(app_module, "MQTT_PREFLIGHT_CONNECT_DELAY_MAX_S", 11.0)

    assert (
        _increase_mqtt_preflight_connect_delay(
            5.0,
            start_monotonic=0.0,
        )
        == 8.0
    )
    assert (
        _increase_mqtt_preflight_connect_delay(
            10.0,
            start_monotonic=0.0,
        )
        == 11.0
    )
    assert (
        _reset_mqtt_preflight_connect_delay(
            11.0,
            start_monotonic=0.0,
        )
        == 5.0
    )


def test_recovery_reconnect_settings_only_expand_after_station_reset():
    assert _recovery_reconnect_attempts(False) == 1
    assert _recovery_reconnect_delay_s(False) == 0.0
    assert _recovery_reconnect_attempts(True) == 3
    assert _recovery_reconnect_delay_s(True) == 2.0


def test_wifi_station_reset_extends_to_repeated_startup_failures():
    assert (
        _should_reset_wifi_station_before_ready("station_scan_miss", 1, False) is False
    )
    assert (
        _should_reset_wifi_station_before_ready("station_scan_miss", 2, False) is True
    )
    assert _should_reset_wifi_station_before_ready("station_unknown", 2, False) is True
    assert (
        _should_reset_wifi_station_before_ready("station_scan_miss", 2, True) is False
    )

    assert _wifi_station_reset_reason(False, True) == "before_ready_failure"


def test_repeated_mqtt_connect_failure_fast_reboot_gate():
    assert (
        _should_fast_reboot_mqtt_connect_failures(
            12,
            100.0,
            159.0,
            timeout_s=60.0,
            min_count=6,
        )
        is False
    )
    assert (
        _should_fast_reboot_mqtt_connect_failures(
            5,
            100.0,
            170.0,
            timeout_s=60.0,
            min_count=6,
        )
        is False
    )
    assert (
        _should_fast_reboot_mqtt_connect_failures(
            6,
            100.0,
            160.0,
            timeout_s=60.0,
            min_count=6,
        )
        is True
    )


def test_repeated_mqtt_connect_failure_defaults_allow_blocking_connects():
    assert _should_fast_reboot_mqtt_connect_failures(2, 100.0, 400.0) is False
    assert _should_fast_reboot_mqtt_connect_failures(3, 100.0, 159.0) is False
    assert _should_fast_reboot_mqtt_connect_failures(3, 100.0, 160.0) is True


def test_repeated_mqtt_failure_budget_uses_first_failure_time():
    assert _mqtt_repeated_failure_elapsed_s(100.0, 159.9) == 59
    assert _mqtt_repeated_failure_elapsed_s(-1.0, 400.0) == 0
    assert _mqtt_repeated_failure_budget_exhausted(100.0, 159.0) is False
    assert _mqtt_repeated_failure_budget_exhausted(100.0, 160.0) is True


def test_startup_conditioning_runs_only_for_soft_reload(monkeypatch):
    monkeypatch.setattr(app_module, "MQTT_STARTUP_CONDITIONING_ENABLED", False)
    monkeypatch.setattr(
        app_module,
        "MQTT_STARTUP_CONDITIONING_SOFT_RELOAD_ENABLED",
        True,
    )
    monkeypatch.setattr(
        app_module,
        "_startup_run_reason_text",
        lambda: "RunReason.STARTUP",
    )

    assert _startup_conditioning_enabled_for_current_run(False) is False
    assert _startup_conditioning_enabled_for_current_run(True) is True

    monkeypatch.setattr(
        app_module,
        "_startup_run_reason_text",
        lambda: "RunReason.SUPERVISOR_RELOAD",
    )
    assert _startup_conditioning_enabled_for_current_run(False) is True

    monkeypatch.setattr(
        app_module,
        "MQTT_STARTUP_CONDITIONING_SOFT_RELOAD_ENABLED",
        False,
    )
    assert _startup_conditioning_enabled_for_current_run(False) is False

    monkeypatch.setattr(app_module, "MQTT_STARTUP_CONDITIONING_ENABLED", True)
    assert _startup_conditioning_enabled_for_current_run(False) is True


def test_mqtt_failure_budget_reboots_after_three_preconnect_failures(monkeypatch):
    calls = []

    monkeypatch.setattr(
        app_module,
        "_recovery_hard_reset_marker_is_set",
        lambda _reason: False,
    )
    monkeypatch.setattr(
        app_module,
        "_mark_recovery_hard_reset_requested",
        lambda _reason: True,
    )
    monkeypatch.setattr(
        app_module,
        "_perform_recovery_reboot",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        _maybe_reboot_for_mqtt_failure_budget(
            failure_count=3,
            first_failure_at=100.0,
            now_monotonic=112.0,
            fs_writable=False,
            start_monotonic=0.0,
        )
        == "reboot"
    )
    assert calls[0][0][:2] == ("mqtt_repeated_connect_failures", "hard")


def test_mqtt_failure_marker_defers_fast_reset_but_allows_budget(monkeypatch):
    calls = []

    monkeypatch.setattr(
        app_module,
        "_recovery_hard_reset_marker_is_set",
        lambda _reason: True,
    )
    monkeypatch.setattr(
        app_module,
        "_mark_recovery_hard_reset_requested",
        lambda _reason: True,
    )
    monkeypatch.setattr(
        app_module,
        "_perform_recovery_reboot",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        _maybe_reboot_for_mqtt_failure_budget(
            failure_count=3,
            first_failure_at=100.0,
            now_monotonic=112.0,
            fs_writable=False,
            start_monotonic=0.0,
        )
        == "none"
    )
    assert calls == []

    assert (
        _maybe_reboot_for_mqtt_failure_budget(
            failure_count=3,
            first_failure_at=100.0,
            now_monotonic=160.0,
            fs_writable=False,
            start_monotonic=0.0,
        )
        == "reboot"
    )
    assert calls[0][0][:2] == ("mqtt_repeated_connect_failures", "hard")


def test_repeated_mqtt_failures_stage_station_reset_before_reboot():
    assert _mqtt_repeated_failure_reboot_gate_tripped(2, 100.0, 112.0) is False
    assert _mqtt_repeated_failure_reboot_gate_tripped(3, 100.0, 112.0) is True
    assert _mqtt_repeated_failure_reboot_gate_tripped(1, 100.0, 160.0) is True
    assert _mqtt_repeated_failure_reboot_gate_tripped(3, -1.0, 112.0) is False

    assert (
        _should_stage_mqtt_station_reset_before_reboot(
            3,
            100.0,
            112.0,
            -1.0,
        )
        is True
    )
    assert (
        _should_stage_mqtt_station_reset_before_reboot(
            3,
            100.0,
            112.0,
            105.0,
        )
        is False
    )


def test_mqtt_socket_poison_keeps_station_reset_after_tcp_ok():
    tcp_ok = SimpleNamespace(reset_needed=False, station_ready=True, status="tcp_ok")
    tcp_failed = SimpleNamespace(
        reset_needed=True,
        station_ready=True,
        status="tcp_failed",
    )

    assert (
        _should_keep_mqtt_station_reset_after_verify(
            "mqtt_poll_failed:[Errno 9] EBADF",
            tcp_ok,
        )
        is True
    )
    assert (
        _should_keep_mqtt_station_reset_after_verify(
            "mqtt_poll_failed:bad file descriptor",
            tcp_ok,
        )
        is True
    )
    assert (
        _should_keep_mqtt_station_reset_after_verify(
            "mqtt_publish_failed:nodus/device/availability:[Errno 5]",
            tcp_ok,
        )
        is False
    )
    assert (
        _should_keep_mqtt_station_reset_after_verify(
            "mqtt_publish_failed:nodus/device/availability:[Errno 5]",
            tcp_failed,
        )
        is True
    )


def test_mqtt_memory_init_failure_window_and_reboot_gate():
    errors = ("mqtt_client_init_failed", "memory allocation failed, allocating 2344")

    assert _mqtt_client_init_memory_failed(errors) is True
    count, started_at = _update_mqtt_memory_failure_window(errors, 0, -1.0, 100.0)
    assert count == 1
    assert started_at == 100.0

    count, started_at = _update_mqtt_memory_failure_window(
        errors,
        count,
        started_at,
        110.0,
    )
    assert count == 2
    assert started_at == 100.0
    assert _should_reboot_mqtt_memory_failures(4, 100.0, 140.0) is False
    assert _should_reboot_mqtt_memory_failures(5, 100.0, 119.0) is False
    assert _should_reboot_mqtt_memory_failures(5, 100.0, 120.0) is True

    count, started_at = _update_mqtt_memory_failure_window(
        (),
        count,
        started_at,
        130.0,
    )
    assert count == 0
    assert started_at == -1.0


def test_sensor_not_found_window_and_reboot_gate():
    errors = (
        "sensor_not_found",
        "sensor_not_found:ValueError:No_I2C_device_at_address:_0x77",
    )

    assert _sensor_errors_indicate_not_found(errors) is True
    count, started_at = _update_sensor_not_found_window(errors, 0, -1.0, 100.0)
    assert count == 1
    assert started_at == 100.0
    count, started_at = _update_sensor_not_found_window(
        errors,
        count,
        started_at,
        120.0,
    )
    assert count == 2
    assert started_at == 100.0
    assert (
        _should_reboot_sensor_not_found(
            5,
            100.0,
            170.0,
            timeout_s=60.0,
            min_count=6,
        )
        is False
    )
    assert (
        _should_reboot_sensor_not_found(
            6,
            100.0,
            160.0,
            timeout_s=60.0,
            min_count=6,
        )
        is True
    )
    count, started_at = _update_sensor_not_found_window(
        ("sensor_metrics_empty",),
        count,
        started_at,
        180.0,
    )
    assert count == 0
    assert started_at == -1.0


def test_deferred_sensor_driver_marker_and_mqtt_queue_gate():
    assert (
        _sensor_driver_start_deferred(
            SimpleNamespace(errors=("sensor_driver_start_deferred",))
        )
        is True
    )
    assert _sensor_driver_start_deferred(SimpleNamespace(errors=())) is False
    assert _mqtt_startup_queues_clear(
        SimpleNamespace(published_messages=[], subscriptions=[])
    ) is True
    assert _mqtt_startup_queues_clear(
        SimpleNamespace(published_messages=[object()], subscriptions=[])
    ) is False
    assert _mqtt_startup_queues_clear(
        SimpleNamespace(published_messages=[], subscriptions=["topic"])
    ) is False


def test_restart_sensor_stack_stops_rebinds_and_reads_snapshot(monkeypatch):
    events = []
    sensor_runtime = SimpleNamespace(phase="ready")
    runtime_config = RuntimeConfig()
    old_service = SimpleNamespace(phase="error")
    new_adapter = SimpleNamespace(phase="bound")
    new_service = SimpleNamespace(phase="ready")
    new_snapshot = SimpleNamespace(phase="ready", metrics={"Temperature": 24.0})

    def fake_stop_sensor_service(service):
        events.append(("stop", service))

    def fake_bind_sensor_hardware(runtime, config):
        events.append(("bind", runtime, config))
        return new_adapter

    def fake_start_sensor_service(runtime, adapter, config):
        events.append(("start", runtime, adapter, config))
        return new_service

    def fake_read_sensor_snapshot(service, config):
        events.append(("read", service, config))
        return new_snapshot

    monkeypatch.setattr(app_module, "stop_sensor_service", fake_stop_sensor_service)
    monkeypatch.setattr(app_module, "bind_sensor_hardware", fake_bind_sensor_hardware)
    monkeypatch.setattr(app_module, "start_sensor_service", fake_start_sensor_service)
    monkeypatch.setattr(app_module, "read_sensor_snapshot", fake_read_sensor_snapshot)
    monkeypatch.setattr(app_module, "_collect_garbage", lambda: None)

    adapter, service, snapshot = _restart_sensor_stack(
        sensor_runtime,
        old_service,
        runtime_config,
    )

    assert adapter is new_adapter
    assert service is new_service
    assert snapshot is new_snapshot
    assert events == [
        ("stop", old_service),
        ("bind", sensor_runtime, runtime_config),
        ("start", sensor_runtime, new_adapter, runtime_config),
        ("read", new_service, runtime_config),
    ]


def test_long_mqtt_recovery_reboot_gate():
    state = RecoveryState(phase="mqtt", phase_started_at=100.0)

    assert _should_reboot_long_mqtt_recovery(state, 999.0) is False
    assert _should_reboot_long_mqtt_recovery(state, 1000.0) is True
    assert (
        _should_reboot_long_mqtt_recovery(
            RecoveryState(phase="wifi", phase_started_at=0.0),
            2000.0,
        )
        is False
    )


def test_plain_mqtt_broker_failures_back_off_connect_before_station_reset():
    state = RecoveryState(phase="mqtt", phase_started_at=100.0)
    plain_failure = "mqtt_connect_failed:10.0.0.248:('Connect failure', None)"
    repeated_failure = (
        "mqtt_connect_failed:10.0.0.248:('Repeated connect failures', None)"
    )

    assert _mqtt_connect_retry_interval_s(state, 399.0, plain_failure) == 5.0
    assert _mqtt_connect_retry_interval_s(state, 400.0, plain_failure) == 30.0
    assert _mqtt_connect_retry_interval_s(state, 400.0, repeated_failure) == 5.0
    assert (
        _should_rebuild_mqtt_adapter_for_recovery(
            SimpleNamespace(phase="ready"),
            plain_failure,
        )
        is False
    )
    assert (
        _should_rebuild_mqtt_adapter_for_recovery(
            SimpleNamespace(phase="ready"),
            repeated_failure,
        )
        is True
    )
    assert (
        _should_reset_mqtt_station_for_plain_connect_failure(
            state,
            279.0,
            plain_failure,
            -1.0,
        )
        is False
    )
    assert (
        _should_reset_mqtt_station_for_plain_connect_failure(
            state,
            280.0,
            plain_failure,
            -1.0,
        )
        is True
    )
    assert (
        _should_reset_mqtt_station_for_plain_connect_failure(
            state,
            400.0,
            plain_failure,
            250.0,
        )
        is False
    )
    assert (
        _should_reset_mqtt_station_for_plain_connect_failure(
            state,
            430.0,
            plain_failure,
            250.0,
        )
        is True
    )
    assert (
        _should_reset_mqtt_station_for_plain_connect_failure(
            state,
            430.0,
            repeated_failure,
            -1.0,
        )
        is False
    )
    assert (
        _should_rebuild_mqtt_adapter_for_recovery(
            SimpleNamespace(phase="error"),
            plain_failure,
        )
        is True
    )


def test_mqtt_rebuild_verification_expires_after_recovery_window():
    transport = MQTTTransport("samhain.local", 1883)
    transport.mark_connected(now_monotonic=50.0)
    transport.mark_disconnected(now_monotonic=61.0)

    should_verify = _should_verify_mqtt_before_rebuild(
        transport,
        RecoveryState(phase="mqtt", phase_started_at=60.0),
        75.0,
        success_window_s=90.0,
        phase_window_s=10.0,
    )

    assert should_verify is False


def test_load_settings_for_startup_skips_write_paths_on_rofs(monkeypatch):
    calls = []
    sentinel_settings = SimpleNamespace(runtime_config=lambda: RuntimeConfig())

    monkeypatch.setattr(
        "cpynodus_ii.app.Settings.filesystem_writable",
        lambda root: False,
    )
    monkeypatch.setattr(
        "cpynodus_ii.app.Settings.apply_factory_profile_reset_if_requested",
        lambda root: calls.append(("profile_reset", root)) or False,
    )
    monkeypatch.setattr(
        "cpynodus_ii.app.Settings.bootstrap_factory_defaults",
        lambda root: calls.append(("bootstrap", root)),
    )
    monkeypatch.setattr(
        "cpynodus_ii.app.Settings.from_working_directory",
        lambda: sentinel_settings,
    )

    settings, fs_writable, profile_reset_requested = _load_settings_for_startup(".")

    assert settings is sentinel_settings
    assert fs_writable is False
    assert profile_reset_requested is False
    assert calls == []


def test_startup_ota_state_loads_from_private_state_file(tmp_path):
    save_ota_state(
        FwUpdateState(
            prior_profile="homeassistant",
            package_id="ota-tagA-to-tagB",
            phase="requested",
        ),
        _ota_state_path(tmp_path),
    )

    state = _load_startup_ota_state(tmp_path)

    assert state.prior_profile == "homeassistant"
    assert state.package_id == "ota-tagA-to-tagB"
    assert state.phase == "requested"


def test_should_enter_ota_mode_requires_rwfs_and_requested_or_ready_state():
    requested = FwUpdateState(phase="requested")
    ready = FwUpdateState(phase="ready")
    invalid = FwUpdateState(phase="invalid")

    assert _should_enter_ota_mode(requested, True) is True
    assert _should_enter_ota_mode(ready, True) is True
    assert _should_enter_ota_mode(requested, False) is False
    assert _should_enter_ota_mode(invalid, True) is False
    assert _should_enter_ota_mode(None, True) is False


def test_mark_ota_applied_after_boot_persists_success_state(tmp_path):
    pending = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        phase="applied_pending_boot",
    )

    applied = _mark_ota_applied_after_boot(pending, tmp_path, True)
    loaded = _load_startup_ota_state(tmp_path)

    assert applied.phase == "applied"
    assert applied.prior_profile == "homeassistant"
    assert applied.package_id == "ota-tagA-to-tagB"
    assert loaded == applied


def test_mark_ota_applied_after_boot_leaves_non_pending_state_unchanged(tmp_path):
    ready = FwUpdateState(package_id="ota-tagA-to-tagB", phase="ready")

    result = _mark_ota_applied_after_boot(ready, tmp_path, True)

    assert result is ready
    assert _load_startup_ota_state(tmp_path) is None


def test_mark_ota_applied_after_boot_skips_write_on_rofs(tmp_path):
    pending = FwUpdateState(package_id="ota-tagA-to-tagB", phase="applied_pending_boot")

    result = _mark_ota_applied_after_boot(pending, tmp_path, False)

    assert result is pending
    assert _load_startup_ota_state(tmp_path) is None


def test_should_fallback_to_ap_when_nodusweb_has_no_ssid():
    runtime_config = RuntimeConfig(active_profile="nodusweb")

    assert (
        _should_fallback_to_ap(
            runtime_config,
            SimpleNamespace(phase="error"),
        )
        is True
    )


def test_should_fallback_to_ap_when_nodusweb_has_no_password():
    runtime_config = RuntimeConfig(
        active_profile="nodusweb",
        network=replace(RuntimeConfig().network, ssid="PeaceHill", password=""),
    )

    assert (
        _should_fallback_to_ap(
            runtime_config,
            SimpleNamespace(phase="ready"),
        )
        is True
    )


def test_should_not_fallback_to_ap_when_station_error_keeps_lan_ip():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=replace(RuntimeConfig().network, ssid="PeaceHill", password="secret"),
    )

    assert (
        _should_fallback_to_ap(
            runtime_config,
            SimpleNamespace(phase="error", ip_address="10.0.0.252"),
        )
        is False
    )


def test_should_not_fallback_to_ap_for_mqtt_profile_station_error_with_ap_ip():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=replace(
            RuntimeConfig().network,
            ssid="PeaceHill",
            password="secret",
            ap_ssid="Nodus_Setup",
        ),
    )

    assert (
        _should_fallback_to_ap(
            runtime_config,
            SimpleNamespace(phase="error", ip_address="192.168.4.16"),
        )
        is False
    )


def test_startup_ap_fallback_reason_uses_original_station_errors():
    assert (
        _startup_ap_fallback_reason(("network_wrong_ssid", "Nodus_Setup"))
        == "network_wrong_ssid,Nodus_Setup"
    )
    assert _startup_ap_fallback_reason(()) == "network_startup_failed"
