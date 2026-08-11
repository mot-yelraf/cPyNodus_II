"""Tests for startup planning, AP fallback, and settings boot behavior.

The cases exercise host-side startup orchestration so profile resolution,
network fallback, and service selection remain deterministic.
"""

from dataclasses import replace
from types import SimpleNamespace

import cpynodus_ii.app as app_module
from cpynodus_ii.app import (
    _broker_ip_refresh_needed,
    _clear_hard_fault_recovery_marker,
    _clear_mqtt_subscription_recovery_marker,
    _clear_terminal_startup_ota_state,
    _defer_switch_subscriptions_until_after_startup_publish,
    _handle_mqtt_subscription_failure,
    _hard_fault_recovery_marker_value,
    _increase_mqtt_preflight_connect_delay,
    _is_mqtt_preoperational_sync_failure,
    _is_mqtt_subscription_failure,
    _load_settings_for_startup,
    _load_startup_ota_state,
    _mark_mqtt_subscription_recovery_requested,
    _maybe_reboot_for_mqtt_failure_budget,
    _mqtt_client_init_memory_failed,
    _mqtt_connect_attempt_is_connack_timeout_pattern,
    _mqtt_connect_errors_are_repeated_failures,
    _mqtt_connect_retry_interval_s,
    _mqtt_error_indicates_allocation_failure,
    _mqtt_error_indicates_socket_progress,
    _mqtt_generation_is_operational,
    _mqtt_native_socket_poison_detected,
    _mqtt_preconnect_has_socket_progress,
    _mqtt_preconnect_probe,
    _mqtt_preoperational_connect_has_allocation_failure,
    _mqtt_preoperational_reboot_plan,
    _mqtt_repeated_failure_budget_exhausted,
    _mqtt_repeated_failure_elapsed_s,
    _mqtt_repeated_failure_reboot_gate_tripped,
    _mqtt_startup_publish_pacing_s,
    _mqtt_subscription_failure_action,
    _mqtt_subscription_recovery_marker_is_set,
    _mqtt_subscription_recovery_state,
    _mqtt_sync_should_log_success,
    _ota_state_path,
    _persist_broker_ip_after_mqtt_connect,
    _prepare_ota_boot_health_check,
    _recover_mqtt_subscription_failure,
    _recovery_reconnect_attempts,
    _recovery_reconnect_delay_s,
    _refresh_broker_ip_from_hostname,
    _reset_mqtt_preflight_connect_delay,
    _resolve_broker_ip_from_hostname,
    _resolve_startup_plan,
    _restart_sensor_stack,
    _runtime_device_id,
    _sample_nodusweb_sensor,
    _sensor_driver_start_deferred,
    _sensor_errors_indicate_not_found,
    _should_cycle_mqtt_radio_for_socket_progress,
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
    _steady_state_for_mqtt_connect,
    _update_mqtt_memory_failure_window,
    _update_mqtt_subscription_failure_window,
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
from cpynodus_ii.features.steady_state import SteadyState
from cpynodus_ii.ota.state import FwUpdateState, save_ota_state


class _BrokerProbeSocket:
    def __init__(self, socket_pool):
        self._socket_pool = socket_pool
        self.timeout = None
        self.connected_to = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.connected_to = address
        self._socket_pool.connects.append(address)
        host = address[0]
        if host in self._socket_pool.failing_targets:
            raise OSError("unreachable")

    def close(self):
        self.closed = True


class _BrokerResolvePool:
    def __init__(self, resolved_ips, *, failing_targets=()):
        self.resolved_ips = tuple(resolved_ips)
        self.failing_targets = tuple(failing_targets)
        self.getaddrinfo_calls = []
        self.connects = []
        self.sockets = []

    def getaddrinfo(self, host, port):
        self.getaddrinfo_calls.append((host, port))
        return [
            (None, None, None, None, (resolved_ip, port))
            for resolved_ip in self.resolved_ips
        ]

    def socket(self):
        sock = _BrokerProbeSocket(self)
        self.sockets.append(sock)
        return sock


def test_nodusweb_sensor_sampling_retains_last_successful_snapshot(monkeypatch):
    previous = SimpleNamespace(phase="ready", metrics={"Temperature": 21.0})
    failed = SimpleNamespace(
        phase="error",
        metrics={},
        errors=("sensor_read_failed", "pystack_exhausted"),
    )
    monkeypatch.setattr(app_module, "read_sensor_snapshot", lambda *args: failed)

    snapshot, errors = _sample_nodusweb_sensor(object(), object(), previous)

    assert snapshot is previous
    assert errors == ("sensor_read_failed", "pystack_exhausted")


def test_nodusweb_sensor_sampling_replaces_cache_on_success(monkeypatch):
    previous = SimpleNamespace(phase="ready", metrics={"Temperature": 21.0})
    current = SimpleNamespace(phase="ready", metrics={"Temperature": 22.0}, errors=())
    monkeypatch.setattr(app_module, "read_sensor_snapshot", lambda *args: current)

    snapshot, errors = _sample_nodusweb_sensor(object(), object(), previous)

    assert snapshot is current
    assert errors == ()

def _ready_network_stack(socket_pool):
    return SimpleNamespace(
        phase="ready",
        ip_address="10.0.0.210",
        socket_pool=socket_pool,
    )


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


def test_mqtt_subscription_failure_window_preserves_first_failure_epoch():
    count, started_at = _update_mqtt_subscription_failure_window(0, -1.0, 100.0)
    assert (count, started_at) == (1, 100.0)

    count, started_at = _update_mqtt_subscription_failure_window(
        count,
        started_at,
        114.0,
    )
    assert (count, started_at) == (2, 100.0)

    state = _mqtt_subscription_recovery_state(
        RecoveryState(
            phase="mqtt",
            phase_started_at=90.0,
            last_mqtt_rebuild_at=95.0,
        ),
        120.0,
    )
    assert state.phase_started_at == 90.0
    assert state.last_mqtt_rebuild_at == 120.0


def test_mqtt_subscription_failure_actions_are_bounded_and_nvm_guarded():
    nvm = bytearray(4)

    assert _mqtt_subscription_failure_action(1, False, nvm) == "mqtt_rebuild"
    assert _mqtt_subscription_failure_action(2, False, nvm) == "station_reset"
    assert _mqtt_subscription_failure_action(2, True, nvm) == "mqtt_rebuild"
    assert _mqtt_subscription_failure_action(3, True, nvm) == "warm_reload"

    assert _mark_mqtt_subscription_recovery_requested(nvm) is True
    assert _mqtt_subscription_recovery_marker_is_set(nvm) is False
    assert _mqtt_subscription_failure_action(3, True, nvm) == "warm_reload"

    assert _mark_mqtt_subscription_recovery_requested(nvm) is True
    assert _mqtt_subscription_recovery_marker_is_set(nvm) is True
    assert _mark_mqtt_subscription_recovery_requested(nvm) is False
    assert _mqtt_subscription_failure_action(3, True, nvm) == "hard_reset"

    assert _clear_mqtt_subscription_recovery_marker(nvm) is True
    assert _mqtt_subscription_recovery_marker_is_set(nvm) is False


def test_preoperational_reboot_plan_allows_two_warm_attempts():
    nvm = bytearray(4)

    assert _mqtt_preoperational_reboot_plan("mqtt_recovery_timeout", False, nvm) == (
        "soft",
        1,
    )
    assert _mqtt_preoperational_reboot_plan("mqtt_recovery_timeout", False, nvm) == (
        "soft",
        2,
    )
    assert _mqtt_preoperational_reboot_plan("mqtt_recovery_timeout", False, nvm) == (
        "hard",
        2,
    )


def test_slow_startup_publish_joins_preoperational_failure_episode():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connected(now_monotonic=10.0)
    result = SimpleNamespace(
        phase="error",
        operation="publish",
        errors=("mqtt_publish_slow:nodus/device/meta/switch:elapsed_ms=29895",),
    )

    assert _is_mqtt_preoperational_sync_failure(result, transport, 0) is True
    assert (
        _is_mqtt_preoperational_sync_failure(
            result,
            transport,
            transport.connection_generation,
        )
        is False
    )


def test_hard_fault_recovery_marker_clears_after_stable_runtime_checkpoint():
    nvm = bytearray((0, 0, 0, 0, 2))

    assert _hard_fault_recovery_marker_value(nvm) == 2
    assert _clear_hard_fault_recovery_marker(nvm) is True
    assert _hard_fault_recovery_marker_value(nvm) == 0


def test_native_socket_poison_requires_slow_publish_and_invalid_close():
    slow_publish = SimpleNamespace(
        operation="publish",
        errors=("mqtt_publish_slow:nodus/device/status/heartbeat",),
    )
    normal_publish = SimpleNamespace(
        operation="publish",
        errors=("mqtt_publish_failed:nodus/device/data:32",),
    )
    poisoned_close = SimpleNamespace(
        errors=("mqtt_close_failed:Socket not managed",),
    )
    clean_close = SimpleNamespace(errors=())

    assert _mqtt_native_socket_poison_detected(slow_publish, poisoned_close)
    assert not _mqtt_native_socket_poison_detected(normal_publish, poisoned_close)
    assert not _mqtt_native_socket_poison_detected(slow_publish, clean_close)


def test_native_socket_poison_handler_hard_resets_before_rebuild(monkeypatch):
    reboots = []
    network_stack = SimpleNamespace()
    mqtt_adapter = SimpleNamespace()
    transport = MQTTTransport("broker.local", 1883)
    sync_result = SimpleNamespace(
        operation="publish",
        topic="nodus/device/status/heartbeat",
        errors=("mqtt_publish_slow:nodus/device/status/heartbeat",),
    )

    monkeypatch.setattr(
        app_module,
        "close_mqtt_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            errors=("mqtt_close_failed:Socket not managed",)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_perform_recovery_reboot",
        lambda *args, **kwargs: reboots.append((args, kwargs)),
    )
    monkeypatch.setattr(
        app_module,
        "_recover_mqtt_subscription_failure",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("poisoned socket must not rebuild in-process")
        ),
    )

    result = _handle_mqtt_subscription_failure(
        failure_count=1,
        first_failure_at=100.0,
        station_reset_done=False,
        now_monotonic=100.0,
        runtime_config=RuntimeConfig(active_profile="sensorius"),
        network_stack=network_stack,
        mqtt_adapter=mqtt_adapter,
        transport=transport,
        sync_result=sync_result,
        fs_writable=False,
        start_monotonic=0.0,
    )

    assert result == (network_stack, mqtt_adapter, False)
    assert reboots[0][0][:2] == ("mqtt_native_socket_poison", "hard")


def test_slow_publish_with_clean_close_rebuilds_without_second_close(monkeypatch):
    closes = []
    rebuilds = []
    network_stack = SimpleNamespace()
    rebuilt_stack = SimpleNamespace()
    mqtt_adapter = SimpleNamespace()
    rebuilt_adapter = SimpleNamespace()
    close_result = SimpleNamespace(errors=())
    transport = MQTTTransport("broker.local", 1883)
    sync_result = SimpleNamespace(
        operation="publish",
        topic="nodus/device/status/heartbeat",
        errors=("mqtt_publish_slow:nodus/device/status/heartbeat",),
    )

    def fake_close(*_args, **_kwargs):
        closes.append(True)
        return close_result

    def fake_recover(**kwargs):
        rebuilds.append(kwargs)
        return rebuilt_stack, rebuilt_adapter

    monkeypatch.setattr(app_module, "close_mqtt_client", fake_close)
    monkeypatch.setattr(
        app_module,
        "_recover_mqtt_subscription_failure",
        fake_recover,
    )

    result = _handle_mqtt_subscription_failure(
        failure_count=1,
        first_failure_at=100.0,
        station_reset_done=False,
        now_monotonic=100.0,
        runtime_config=RuntimeConfig(active_profile="sensorius"),
        network_stack=network_stack,
        mqtt_adapter=mqtt_adapter,
        transport=transport,
        sync_result=sync_result,
        fs_writable=False,
        start_monotonic=0.0,
    )

    assert result == (rebuilt_stack, rebuilt_adapter, False)
    assert len(closes) == 1
    assert rebuilds[0]["close_result"] is close_result


def test_puback_timeout_joins_preoperational_failure_episode():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connected(now_monotonic=10.0)
    result = SimpleNamespace(
        phase="error",
        operation="publish",
        errors=(
            "mqtt_publish_failed:nodus/device/meta:bytes=1438:"
            "raw_publish_diag qos=1 pkt_id=1 "
            "error=mqtt_puback_stage=header:[Errno 116] ETIMEDOUT",
        ),
    )

    assert _is_mqtt_preoperational_sync_failure(result, transport, 0) is True


def test_errno_12_preflight_and_connect_are_allocation_failures():
    assert _mqtt_error_indicates_allocation_failure(
        "mqtt_tcp_preflight_failed:10.0.0.246:OSError:[Errno 12] ENOMEM"
    )
    assert _mqtt_preoperational_connect_has_allocation_failure(
        tcp_preflight_error=(
            "mqtt_tcp_preflight_failed:10.0.0.246:OSError:[Errno 12] ENOMEM"
        ),
        connect_probe_error="mqtt_connect_probe_skipped:tcp_preflight_error",
        connect_errors=(
            "mqtt_connect_failed:10.0.0.246:raw:OSError:[Errno 12] ENOMEM",
        ),
    )
    assert not _mqtt_preoperational_connect_has_allocation_failure(
        tcp_preflight_error="",
        connect_probe_error="",
        connect_errors=("mqtt_connect_failed:10.0.0.246:raw:OSError:[Errno 5]",),
    )


def test_retained_startup_publish_pacing_stops_after_operational_checkpoint():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connected(now_monotonic=10.0)
    result = SimpleNamespace(
        phase="synced",
        operation="publish",
        retain=True,
        published_count=1,
    )

    assert _mqtt_startup_publish_pacing_s(result, transport, 0) == 0.35
    assert (
        _mqtt_startup_publish_pacing_s(
            result,
            transport,
            transport.connection_generation,
        )
        == 0.0
    )


def test_mqtt_generation_is_operational_only_after_checkpoint():
    transport = MQTTTransport("broker.local", 1883)
    assert _mqtt_generation_is_operational(transport, 0) is False

    generation = transport.mark_connected(now_monotonic=10.0)
    assert _mqtt_generation_is_operational(transport, 0) is False
    assert _mqtt_generation_is_operational(transport, generation) is True

    transport.mark_disconnected(now_monotonic=11.0, reason="test")
    assert _mqtt_generation_is_operational(transport, generation) is False


def test_subscription_reconnect_reuses_pending_startup_generation():
    transport = MQTTTransport("broker.local", 1883)
    generation = transport.mark_connected(now_monotonic=10.0)
    state = SteadyState(connection_generation=0)

    unchanged = _steady_state_for_mqtt_connect(state, transport, False)
    reused = _steady_state_for_mqtt_connect(state, transport, True)

    assert unchanged is state
    assert reused.connection_generation == generation


def test_subscription_failure_handler_uses_guarded_warm_reload(monkeypatch):
    reboots = []
    network_stack = SimpleNamespace()
    mqtt_adapter = SimpleNamespace()

    monkeypatch.setattr(
        app_module,
        "_mark_mqtt_subscription_recovery_requested",
        lambda: True,
    )
    monkeypatch.setattr(
        app_module,
        "_perform_recovery_reboot",
        lambda *args, **kwargs: reboots.append((args, kwargs)),
    )
    monkeypatch.setattr(
        app_module,
        "_mqtt_subscription_recovery_marker_is_set",
        lambda _nvm=None: False,
    )

    result = _handle_mqtt_subscription_failure(
        failure_count=3,
        first_failure_at=100.0,
        station_reset_done=True,
        now_monotonic=130.0,
        runtime_config=RuntimeConfig(active_profile="sensorius"),
        network_stack=network_stack,
        mqtt_adapter=mqtt_adapter,
        transport=MQTTTransport("broker.local", 1883),
        sync_result=SimpleNamespace(topic="nodus/device/config/set"),
        fs_writable=False,
        start_monotonic=0.0,
    )

    assert result == (network_stack, mqtt_adapter, True)
    assert reboots[0][0][:2] == ("mqtt_startup_not_operational", "soft")


def test_subscription_station_reset_rebuilds_network_socket_artifacts(monkeypatch):
    reconnects = []
    rebuilt_stack = SimpleNamespace(socket_pool="new-pool", ssl_context="new-ssl")
    rebuilt_adapter = SimpleNamespace(
        active_broker="10.0.0.246",
        broker="broker.local",
    )

    monkeypatch.setattr(
        app_module,
        "close_mqtt_client",
        lambda *_args, **_kwargs: SimpleNamespace(errors=()),
    )

    def fake_reconnect(runtime_config, network_stack, **kwargs):
        reconnects.append((runtime_config, network_stack, kwargs))
        return rebuilt_stack

    monkeypatch.setattr(app_module, "reconnect_network_stack", fake_reconnect)
    monkeypatch.setattr(
        app_module,
        "build_mqtt_client_adapter",
        lambda *_args, **_kwargs: rebuilt_adapter,
    )
    monkeypatch.setattr(app_module, "_collect_garbage", lambda: None)

    original_stack = SimpleNamespace(socket_pool="old-pool", ssl_context="old-ssl")
    result = _recover_mqtt_subscription_failure(
        runtime_config=RuntimeConfig(active_profile="sensorius"),
        network_stack=original_stack,
        mqtt_adapter=SimpleNamespace(),
        transport=MQTTTransport("broker.local", 1883),
        sync_result=SimpleNamespace(topic="nodus/device/config/set"),
        reset_station=True,
        fs_writable=False,
        start_monotonic=0.0,
    )

    assert result == (rebuilt_stack, rebuilt_adapter)
    assert reconnects[0][1] is original_stack
    assert reconnects[0][2]["rebuild_socket_artifacts"] is True
    assert reconnects[0][2]["reset_station"] is True
    assert reconnects[0][2]["cycle_radio"] is True
    assert reconnects[0][2]["force_station_reset"] is True


def test_refresh_broker_ip_uses_runtime_resolution_on_startup(tmp_path):
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
    socket_pool = _BrokerResolvePool(("10.0.0.248",))
    network_stack = _ready_network_stack(socket_pool)

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        network_stack,
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "resolved_volatile"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"
    assert 'BROKER_IP = ""' in text
    assert "BROKER_IP_ALT" not in text
    assert socket_pool.connects == []


def test_refresh_broker_ip_updates_changed_hostname_resolution(tmp_path):
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
        _ready_network_stack(_BrokerResolvePool(("10.0.0.220",))),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "resolved_volatile"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.220"
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert "BROKER_IP_ALT" not in text


def test_refresh_broker_ip_skips_probe_for_unchanged_primary(tmp_path):
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
    socket_pool = _BrokerResolvePool(
        ("10.0.0.248",),
        failing_targets=("10.0.0.248",),
    )

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        _ready_network_stack(socket_pool),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "unchanged"
    assert errors == ()
    assert updated_runtime is runtime_config
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"
    assert socket_pool.connects == []
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert "BROKER_IP_ALT" not in text


def test_refresh_broker_ip_updates_changed_hostname_without_tcp_verification(tmp_path):
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
    socket_pool = _BrokerResolvePool(("10.0.0.220",))

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        _ready_network_stack(socket_pool),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "resolved_volatile"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.220"
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert 'BROKER_IP = "10.0.0.220"' not in text
    assert socket_pool.connects == []


def test_resolve_broker_ip_from_hostname_uses_first_unique_address():
    class _ResolveDuplicatePool:
        def getaddrinfo(self, host, port):
            assert host == "samhain.local"
            return [
                (None, None, None, None, ("10.0.0.248", port)),
                (None, None, None, None, ("10.0.0.220", port)),
                (None, None, None, None, ("10.0.0.248", port)),
                (None, None, None, None, ("10.0.0.205", port)),
            ]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )

    resolved_ip, errors = _resolve_broker_ip_from_hostname(
        runtime_config,
        SimpleNamespace(socket_pool=_ResolveDuplicatePool()),
    )

    assert errors == ()
    assert resolved_ip == "10.0.0.248"


def test_refresh_broker_ip_uses_first_hostname_resolution_at_runtime(tmp_path):
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

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        _ready_network_stack(_BrokerResolvePool(("10.0.0.248", "10.0.0.220"))),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert phase == "resolved_volatile"
    assert errors == ()
    assert updated_runtime.mqtt.broker_ip == "10.0.0.248"
    assert updated_runtime.mqtt.connection_targets == ("10.0.0.248",)
    assert 'BROKER_IP = ""' in text
    assert "BROKER_IP_ALT" not in text


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
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )

    updated_runtime, phase, errors = _refresh_broker_ip_from_hostname(
        runtime_config,
        _ready_network_stack(_BrokerResolvePool(("10.0.0.248",))),
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


def test_broker_ip_refresh_needed_when_host_and_ip_are_configured(tmp_path):
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=tmp_path) is True


def test_broker_ip_refresh_needed_for_rofs_when_host_and_ip_are_configured():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=None) is True


def test_broker_ip_refresh_needed_for_rofs_without_configured_ip():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker="samhain.local", port=1883),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=None) is True


def test_broker_ip_refresh_skips_when_broker_host_is_missing():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(broker_ip="10.0.0.248", port=1883),
    )

    assert _broker_ip_refresh_needed(runtime_config, settings_root=None) is False


def test_refresh_broker_ip_for_mqtt_reports_changed_target(tmp_path):
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

    updated_runtime, changed = app_module._refresh_broker_ip_for_mqtt(
        runtime_config,
        _ready_network_stack(_BrokerResolvePool(("10.0.0.220",))),
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert changed is True
    assert updated_runtime.mqtt.broker_ip == "10.0.0.220"
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert "BROKER_IP_ALT" not in text


def test_persist_broker_ip_after_mqtt_connect_writes_primary(tmp_path):
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
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    persisted = _persist_broker_ip_after_mqtt_connect(
        runtime_config,
        settings_root=tmp_path,
    )

    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert persisted is True
    assert 'BROKER_IP = "10.0.0.248"' in text
    assert "BROKER_IP_ALT" not in text


def test_persist_broker_ip_after_mqtt_connect_skips_without_settings_root():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="samhain.local",
            broker_ip="10.0.0.248",
            port=1883,
        ),
    )

    persisted = _persist_broker_ip_after_mqtt_connect(
        runtime_config,
        settings_root=None,
    )

    assert persisted is False


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


def test_mqtt_socket_progress_can_force_one_radio_cycle():
    station_verify = SimpleNamespace(
        reset_needed=False,
        station_ready=True,
        status="tcp_failed",
        errors=(
            "tcp_failed:10.0.0.248:OSError:[Errno 119] EINPROGRESS",
        ),
    )

    assert (
        _should_cycle_mqtt_radio_for_socket_progress(station_verify, -1.0)
        is True
    )
    assert (
        _should_cycle_mqtt_radio_for_socket_progress(station_verify, 105.0)
        is False
    )


def test_mqtt_socket_progress_radio_cycle_requires_ready_stuck_tcp():
    station_verify = SimpleNamespace(
        reset_needed=False,
        station_ready=False,
        status="tcp_failed",
        errors=(
            "tcp_failed:10.0.0.248:OSError:[Errno 119] EINPROGRESS",
        ),
    )

    assert (
        _should_cycle_mqtt_radio_for_socket_progress(station_verify, -1.0)
        is False
    )

    station_verify.station_ready = True
    station_verify.errors = (
        "tcp_failed:10.0.0.248:OSError:[Errno 116] ETIMEDOUT",
    )

    assert (
        _should_cycle_mqtt_radio_for_socket_progress(station_verify, -1.0)
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


def test_deferred_sensor_driver_marker():
    assert (
        _sensor_driver_start_deferred(
            SimpleNamespace(errors=("sensor_driver_start_deferred",))
        )
        is True
    )
    assert _sensor_driver_start_deferred(SimpleNamespace(errors=())) is False



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


def test_prepare_ota_boot_health_check_persists_pending_state(tmp_path):
    pending = FwUpdateState(
        prior_profile="homeassistant",
        package_id="ota-tagA-to-tagB",
        phase="applied_pending_boot",
    )

    boot_pending = _prepare_ota_boot_health_check(pending, tmp_path, True)
    loaded = _load_startup_ota_state(tmp_path)

    assert boot_pending.phase == "boot_pending"
    assert boot_pending.prior_profile == "homeassistant"
    assert boot_pending.package_id == "ota-tagA-to-tagB"
    assert loaded == boot_pending


def test_prepare_ota_boot_health_check_leaves_non_pending_state_unchanged(tmp_path):
    ready = FwUpdateState(package_id="ota-tagA-to-tagB", phase="ready")

    result = _prepare_ota_boot_health_check(ready, tmp_path, True)

    assert result is ready
    assert _load_startup_ota_state(tmp_path) is None


def test_prepare_ota_boot_health_check_skips_write_on_rofs(tmp_path):
    pending = FwUpdateState(package_id="ota-tagA-to-tagB", phase="applied_pending_boot")

    result = _prepare_ota_boot_health_check(pending, tmp_path, False)

    assert result is pending
    assert _load_startup_ota_state(tmp_path) is None


def test_prepare_ota_boot_health_check_rolls_back_unhealthy_second_boot(
    tmp_path,
    monkeypatch,
):
    from cpynodus_ii.ota import http as ota_http

    boot_pending = FwUpdateState(
        package_id="ota-tagA-to-tagB",
        phase="boot_pending",
    )
    recovered = []
    monkeypatch.setattr(
        ota_http,
        "recover_interrupted_ota_apply",
        lambda root: recovered.append(root) or "",
    )

    result = _prepare_ota_boot_health_check(boot_pending, tmp_path, True)

    assert result is None
    assert recovered == [tmp_path]


def test_prepare_ota_boot_health_check_accepts_entrypoint_armed_first_boot(tmp_path):
    boot_pending = FwUpdateState(
        package_id="ota-tagA-to-tagB",
        phase="boot_pending",
    )

    result = _prepare_ota_boot_health_check(
        boot_pending,
        tmp_path,
        True,
        first_boot_armed=True,
    )

    assert result is boot_pending


def test_clear_terminal_startup_ota_state_removes_invalid_state(tmp_path):
    save_ota_state(
        FwUpdateState(package_id="ota-tagA-to-tagB", phase="invalid"),
        _ota_state_path(tmp_path),
    )
    state = _load_startup_ota_state(tmp_path)

    result = _clear_terminal_startup_ota_state(state, tmp_path, True)

    assert result is None
    assert _load_startup_ota_state(tmp_path) is None


def test_clear_terminal_startup_ota_state_removes_interrupted_staging(tmp_path):
    for relative_path in (
        "_ota/stage/partial.mpy",
        "_ota/backup/original.mpy",
        "_ota/manifest.json",
        "_ota/transaction.json",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("leftover")
    save_ota_state(
        FwUpdateState(package_id="ota-tagA-to-tagB", phase="staging"),
        _ota_state_path(tmp_path),
    )
    state = _load_startup_ota_state(tmp_path)

    result = _clear_terminal_startup_ota_state(state, tmp_path, True)

    assert result is None
    assert _load_startup_ota_state(tmp_path) is None
    assert not (tmp_path / "_ota/stage").exists()
    assert not (tmp_path / "_ota/backup").exists()
    assert not (tmp_path / "_ota/manifest.json").exists()
    assert not (tmp_path / "_ota/transaction.json").exists()


def test_clear_terminal_startup_ota_state_preserves_requested_state(tmp_path):
    requested = FwUpdateState(package_id="ota-tagA-to-tagB", phase="requested")
    save_ota_state(requested, _ota_state_path(tmp_path))
    state = _load_startup_ota_state(tmp_path)

    result = _clear_terminal_startup_ota_state(state, tmp_path, True)

    assert result == requested
    assert _load_startup_ota_state(tmp_path) == requested


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
