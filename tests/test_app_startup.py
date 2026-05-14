"""Tests for startup planning, AP fallback, and settings boot behavior."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from cpynodus_ii.app import (
    _learned_broker_candidate,
    _load_settings_for_startup,
    _load_startup_ota_state,
    _mark_ota_applied_after_boot,
    _ota_state_path,
    _persist_learned_broker_ip,
    _resolve_startup_plan,
    _should_enter_ota_mode,
    _should_fallback_to_ap,
    _should_log_wifi_failure_signature,
    _should_verify_mqtt_before_rebuild,
    _should_wait_for_broker_hostname_resolution,
    _startup_ap_fallback_reason,
    _with_learned_broker_target,
)
from cpynodus_ii.core import RecoveryState, build_mqtt_client_adapter
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


def test_broker_hostname_resolution_uses_broker_ip_fallback_on_mdns_failure():
    class _ResolveFailPool:
        def getaddrinfo(self, host, port):
            raise OSError(-2)

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", broker_ip="10.0.0.248"),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveFailPool(),
        modules={"mqtt_cls": lambda **_kwargs: object()},
    )

    should_wait, detail = _should_wait_for_broker_hostname_resolution(
        runtime_config,
        adapter,
        now_monotonic=15.0,
        recovery_state=RecoveryState(phase="mqtt", phase_started_at=0.0),
        settle_s=30.0,
    )

    assert should_wait is False
    assert "samhain.local" in detail
    assert "mqtt_resolve_failed" in detail
    assert "fallback_ip=10.0.0.248" in detail


def test_broker_hostname_resolution_allows_fallback_after_settle_window():
    class _ResolveFailPool:
        def getaddrinfo(self, host, port):
            raise OSError(-2)

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", broker_ip="10.0.0.248"),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveFailPool(),
        modules={"mqtt_cls": lambda **_kwargs: object()},
    )

    should_wait, detail = _should_wait_for_broker_hostname_resolution(
        runtime_config,
        adapter,
        now_monotonic=31.0,
        recovery_state=RecoveryState(phase="mqtt", phase_started_at=0.0),
        settle_s=30.0,
    )

    assert should_wait is False
    assert detail == ""


def test_broker_hostname_resolution_does_not_wait_when_host_resolves():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            return [(None, None, None, None, ("10.0.0.248", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", broker_ip="10.0.0.248"),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        modules={"mqtt_cls": lambda **_kwargs: object()},
    )

    should_wait, detail = _should_wait_for_broker_hostname_resolution(
        runtime_config,
        adapter,
        now_monotonic=15.0,
        recovery_state=RecoveryState(phase="mqtt", phase_started_at=0.0),
        settle_s=30.0,
    )

    assert should_wait is False
    assert detail == "resolved_ip=10.0.0.248"


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


def test_learned_broker_target_appends_after_configured_targets():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": lambda **_kwargs: object()},
    )

    updated = _with_learned_broker_target(
        adapter,
        "10.0.0.220",
        runtime_config,
    )

    assert updated.broker_targets == ("samhain.local", "10.0.0.248", "10.0.0.220")


def test_learned_broker_candidate_keeps_hostname_resolution_runtime_only():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )
    adapter = SimpleNamespace(
        active_broker="samhain.local",
        resolved_broker_ip="10.0.0.220",
    )

    assert (
        _learned_broker_candidate("", runtime_config, adapter)
        == "10.0.0.220"
    )


def test_wifi_failure_signature_logging_is_sparse():
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 1, False)
    assert not _should_log_wifi_failure_signature(
        "station_scan_miss_after_ready", 2, False
    )
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 3, False)
    assert _should_log_wifi_failure_signature("station_scan_miss_after_ready", 5, True)
    assert not _should_log_wifi_failure_signature("none", 1, True)


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


def test_persist_learned_broker_ip_skips_hostname_success(tmp_path):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="plain-wifi",
            hostname="co2-29j39c",
        ),
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )
    mqtt_adapter = SimpleNamespace(
        active_broker="samhain.local",
        resolved_broker_ip="10.0.0.248",
    )

    updated_runtime, phase, errors = _persist_learned_broker_ip(
        runtime_config,
        mqtt_adapter,
        settings_root=tmp_path,
    )

    assert phase == "skipped"
    assert errors == ()
    assert updated_runtime is runtime_config
    assert not (tmp_path / "settings.toml").exists()


def test_persist_learned_broker_ip_promotes_direct_learned_success(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("settings.toml.def",):
        (tmp_path / name).write_text((root / name).read_text(), encoding="utf-8")
    (tmp_path / "settings.toml").write_text(
        "[Network]\n"
        'SSID = "PeaceHill"\n'
        'PASSWORD = "plain-wifi"\n'
        'AP_SSID = "Nodus_Setup"\n'
        'AP_PASSWORD = "plain-ap"\n'
        'HOSTNAME = "co2-29j39c"\n'
        "HTTPPORT = 8000\n"
        "[Profile]\n"
        'ACTIVE_PROFILE = "sensorius"\n'
        "[MQTT]\n"
        'BROKER = "samhain.local"\n'
        'BROKER_IP = "10.0.0.248"\n'
        "PORT = 1883\n"
        'PASSWORD = "plain-mqtt"\n',
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
    mqtt_adapter = SimpleNamespace(
        active_broker="10.0.0.220",
        resolved_broker_ip="10.0.0.220",
    )

    updated_runtime, phase, errors = _persist_learned_broker_ip(
        runtime_config,
        mqtt_adapter,
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "persisted"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.220"
    assert 'BROKER_IP = "10.0.0.220"' in text


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
