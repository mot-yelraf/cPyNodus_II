"""Bounded runtime recovery policy for Wi-Fi, MQTT, and AP idle mode."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryPolicy:
    """Describe bounded recovery windows and retry cadence."""

    wifi_timeout_s: float = 900.0
    wifi_retry_interval_s: float = 5.0
    mqtt_timeout_s: float = 180.0
    mqtt_rebuild_interval_s: float = 30.0
    ap_timeout_s: float = 600.0


@dataclass(frozen=True)
class RecoveryState:
    """Track the active recovery phase across loop iterations."""

    phase: str = "idle"
    phase_started_at: float = -1.0
    last_wifi_attempt_at: float = -1.0
    last_mqtt_rebuild_at: float = -1.0


@dataclass(frozen=True)
class RecoveryDecision:
    """Describe what recovery action the runtime should take next."""

    state: RecoveryState
    allow_mqtt_connect: bool
    attempt_wifi_reconnect: bool = False
    attempt_mqtt_rebuild: bool = False
    request_soft_reboot: bool = False
    reboot_reason: str = ""


def advance_recovery_state(
    state,
    *,
    now_monotonic,
    policy=None,
    ap_mode=False,
    wifi_link_ready=True,
    transport_connected=False,
):
    """Advance recovery state and return the next action decision."""
    policy = policy or RecoveryPolicy()
    state = state or RecoveryState()
    now_value = float(now_monotonic or 0.0)

    if ap_mode:
        ap_state = _ensure_phase(state, "ap", now_value)
        if _phase_elapsed(ap_state, now_value) >= float(policy.ap_timeout_s):
            return RecoveryDecision(
                state=ap_state,
                allow_mqtt_connect=False,
                request_soft_reboot=True,
                reboot_reason="ap_idle_timeout",
            )
        return RecoveryDecision(state=ap_state, allow_mqtt_connect=False)

    if transport_connected:
        return RecoveryDecision(
            state=RecoveryState(),
            allow_mqtt_connect=True,
        )

    if not wifi_link_ready:
        wifi_state = _ensure_phase(state, "wifi", now_value)
        if _phase_elapsed(wifi_state, now_value) >= float(policy.wifi_timeout_s):
            return RecoveryDecision(
                state=wifi_state,
                allow_mqtt_connect=False,
                request_soft_reboot=True,
                reboot_reason="wifi_recovery_timeout",
            )
        attempt_wifi = _interval_elapsed(
            wifi_state.last_wifi_attempt_at,
            now_value,
            float(policy.wifi_retry_interval_s),
        )
        next_state = wifi_state
        if attempt_wifi:
            next_state = RecoveryState(
                phase=wifi_state.phase,
                phase_started_at=wifi_state.phase_started_at,
                last_wifi_attempt_at=now_value,
                last_mqtt_rebuild_at=wifi_state.last_mqtt_rebuild_at,
            )
        return RecoveryDecision(
            state=next_state,
            allow_mqtt_connect=False,
            attempt_wifi_reconnect=attempt_wifi,
        )

    mqtt_state = _ensure_phase(state, "mqtt", now_value)
    if _phase_elapsed(mqtt_state, now_value) >= float(policy.mqtt_timeout_s):
        return RecoveryDecision(
            state=mqtt_state,
            allow_mqtt_connect=True,
            request_soft_reboot=True,
            reboot_reason="mqtt_recovery_timeout",
        )
    attempt_rebuild = _interval_elapsed(
        mqtt_state.last_mqtt_rebuild_at,
        now_value,
        float(policy.mqtt_rebuild_interval_s),
    )
    next_state = mqtt_state
    if attempt_rebuild:
        next_state = RecoveryState(
            phase=mqtt_state.phase,
            phase_started_at=mqtt_state.phase_started_at,
            last_wifi_attempt_at=mqtt_state.last_wifi_attempt_at,
            last_mqtt_rebuild_at=now_value,
        )
    return RecoveryDecision(
        state=next_state,
        allow_mqtt_connect=True,
        attempt_mqtt_rebuild=attempt_rebuild,
    )


def _ensure_phase(state, phase, now_monotonic):
    if state.phase == phase:
        return state
    return RecoveryState(phase=phase, phase_started_at=now_monotonic)


def _phase_elapsed(state, now_monotonic):
    started_at = float(state.phase_started_at)
    if started_at < 0.0:
        return 0.0
    return max(0.0, float(now_monotonic) - started_at)


def _interval_elapsed(last_at, now_monotonic, interval_s):
    if interval_s <= 0.0:
        return True
    if float(last_at) < 0.0:
        return True
    return (float(now_monotonic) - float(last_at)) >= float(interval_s)
