"""Tests for startup planning, AP fallback, and settings boot behavior."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from cpynodus_ii.app import (
    _load_settings_for_startup,
    _load_startup_ota_state,
    _mark_ota_applied_after_boot,
    _mark_startup_warm_rebooted,
    _ota_state_path,
    _persist_learned_broker_ip,
    _read_startup_warm_reboot_marker,
    _resolve_startup_plan,
    _should_enter_ota_mode,
    _should_fallback_to_ap,
    _startup_ap_fallback_reason,
    _startup_warm_reboot_ready_reason,
)
from cpynodus_ii.core.config import MQTTConfig, NetworkConfig, RuntimeConfig
from cpynodus_ii.core.obfuscation import PASSWORD_OBF_PREFIX
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


def test_persist_learned_broker_ip_writes_settings_and_obfuscates_password(tmp_path):
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
        'BROKER_IP = ""\n'
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

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "persisted"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert 'PASSWORD = "plain-wifi"' not in text
    assert PASSWORD_OBF_PREFIX + ":" in text


def test_startup_warm_reboot_marker_uses_second_nvm_byte():
    nvm = bytearray(4)

    assert _read_startup_warm_reboot_marker(nvm) == 0
    assert _mark_startup_warm_rebooted(nvm) is True
    assert _read_startup_warm_reboot_marker(nvm) == 1


def test_startup_warm_reboot_ready_after_ntp_synced():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )
    network_stack = SimpleNamespace(phase="ready", socket_pool=object())
    mqtt_adapter = SimpleNamespace(
        active_broker="samhain.local", broker="samhain.local"
    )
    ntp_state = SimpleNamespace(phase="synced")
    plan = SimpleNamespace(mqtt_enabled=True)

    reason, errors = _startup_warm_reboot_ready_reason(
        runtime_config,
        network_stack,
        mqtt_adapter,
        ntp_state,
        plan,
    )

    assert reason == "ntp_synced"
    assert errors == ()


def test_startup_warm_reboot_waits_for_broker_dns(monkeypatch):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )
    network_stack = SimpleNamespace(phase="ready", socket_pool=object())
    mqtt_adapter = SimpleNamespace(
        active_broker="samhain.local", broker="samhain.local"
    )
    ntp_state = SimpleNamespace(phase="deferred")
    plan = SimpleNamespace(mqtt_enabled=True)

    monkeypatch.setattr(
        "cpynodus_ii.app.preflight_mqtt_broker",
        lambda adapter: ("mqtt_resolve_failed:samhain.local:-2", ""),
    )

    reason, errors = _startup_warm_reboot_ready_reason(
        runtime_config,
        network_stack,
        mqtt_adapter,
        ntp_state,
        plan,
    )

    assert reason == ""
    assert errors == ("mqtt_resolve_failed:samhain.local:-2",)


def test_startup_warm_reboot_ready_after_broker_dns(monkeypatch):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )
    network_stack = SimpleNamespace(phase="ready", socket_pool=object())
    mqtt_adapter = SimpleNamespace(
        active_broker="samhain.local", broker="samhain.local"
    )
    ntp_state = SimpleNamespace(phase="deferred")
    plan = SimpleNamespace(mqtt_enabled=True)

    monkeypatch.setattr(
        "cpynodus_ii.app.preflight_mqtt_broker",
        lambda adapter: ("", "10.0.0.248"),
    )

    reason, errors = _startup_warm_reboot_ready_reason(
        runtime_config,
        network_stack,
        mqtt_adapter,
        ntp_state,
        plan,
    )

    assert reason == "broker_resolved"
    assert errors == ()


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
