"""Tests for startup planning, AP fallback, and settings boot behavior."""

from dataclasses import replace
from types import SimpleNamespace

from cpynodus_ii.app import (
    _broker_ip_refresh_needed,
    _is_mqtt_subscription_failure,
    _load_settings_for_startup,
    _load_startup_ota_state,
    _mark_ota_applied_after_boot,
    _mqtt_connect_errors_are_repeated_failures,
    _ota_state_path,
    _refresh_broker_ip_from_hostname,
    _resolve_startup_plan,
    _should_enter_ota_mode,
    _should_fallback_to_ap,
    _should_fast_reboot_mqtt_connect_failures,
    _should_fast_reboot_wifi_after_ready,
    _should_fast_reset_wifi_station,
    _should_log_wifi_failure_signature,
    _should_verify_mqtt_before_rebuild,
    _startup_ap_fallback_reason,
    _startup_subscription_recovery_drained,
)
from cpynodus_ii.core import RecoveryState
from cpynodus_ii.core.config import MQTTConfig, NetworkConfig, RuntimeConfig
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


def test_broker_ip_refresh_needed_for_writable_startup_even_with_ip(tmp_path):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=tmp_path) is True


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


def test_repeated_mqtt_connect_failure_detection_matches_minimqtt_error():
    errors = (
        "mqtt_connect_failed:10.0.0.248:('Repeated connect failures', None)",
    )

    assert _mqtt_connect_errors_are_repeated_failures(errors) is True
    assert (
        _mqtt_connect_errors_are_repeated_failures(("mqtt_connect_failed:5",))
        is False
    )


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
    assert _should_fast_reboot_mqtt_connect_failures(3, 100.0, 279.0) is False
    assert _should_fast_reboot_mqtt_connect_failures(3, 100.0, 280.0) is True


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
