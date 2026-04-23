"""Tests for bounded Wi-Fi and MQTT recovery policy decisions."""

from cpynodus_ii.core.recovery import RecoveryPolicy, RecoveryState, advance_recovery_state


def test_wifi_recovery_blocks_mqtt_and_requests_reconnect():
    decision = advance_recovery_state(
        RecoveryState(),
        now_monotonic=10.0,
        policy=RecoveryPolicy(wifi_retry_interval_s=5.0),
        ap_mode=False,
        wifi_link_ready=False,
        transport_connected=False,
    )

    assert decision.state.phase == "wifi"
    assert decision.allow_mqtt_connect is False
    assert decision.attempt_wifi_reconnect is True
    assert decision.request_soft_reboot is False


def test_wifi_recovery_requests_soft_reboot_after_timeout():
    state = RecoveryState(phase="wifi", phase_started_at=0.0, last_wifi_attempt_at=10.0)

    decision = advance_recovery_state(
        state,
        now_monotonic=901.0,
        policy=RecoveryPolicy(wifi_timeout_s=900.0),
        ap_mode=False,
        wifi_link_ready=False,
        transport_connected=False,
    )

    assert decision.request_soft_reboot is True
    assert decision.reboot_reason == "wifi_recovery_timeout"


def test_mqtt_recovery_rebuilds_before_timeout():
    state = RecoveryState(phase="mqtt", phase_started_at=0.0, last_mqtt_rebuild_at=0.0)

    decision = advance_recovery_state(
        state,
        now_monotonic=31.0,
        policy=RecoveryPolicy(mqtt_timeout_s=180.0, mqtt_rebuild_interval_s=30.0),
        ap_mode=False,
        wifi_link_ready=True,
        transport_connected=False,
    )

    assert decision.state.phase == "mqtt"
    assert decision.allow_mqtt_connect is True
    assert decision.attempt_mqtt_rebuild is True
    assert decision.request_soft_reboot is False


def test_ap_mode_requests_soft_reboot_after_timeout():
    state = RecoveryState(phase="ap", phase_started_at=0.0)

    decision = advance_recovery_state(
        state,
        now_monotonic=601.0,
        policy=RecoveryPolicy(ap_timeout_s=600.0),
        ap_mode=True,
        wifi_link_ready=False,
        transport_connected=False,
    )

    assert decision.allow_mqtt_connect is False
    assert decision.request_soft_reboot is True
    assert decision.reboot_reason == "ap_idle_timeout"


def test_mqtt_disabled_profile_stays_out_of_mqtt_recovery():
    decision = advance_recovery_state(
        RecoveryState(phase="mqtt", phase_started_at=0.0, last_mqtt_rebuild_at=0.0),
        now_monotonic=181.0,
        policy=RecoveryPolicy(mqtt_timeout_s=180.0, mqtt_rebuild_interval_s=30.0),
        ap_mode=False,
        mqtt_enabled=False,
        wifi_link_ready=True,
        transport_connected=False,
    )

    assert decision.state.phase == "idle"
    assert decision.allow_mqtt_connect is False
    assert decision.attempt_mqtt_rebuild is False
    assert decision.request_soft_reboot is False
