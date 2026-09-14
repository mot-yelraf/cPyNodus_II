# Lesson 9: Network failures and recovery

[Previous](08-switches-and-local-automation.md) · [Index](README.md) · [Next](10-memory-and-cooperative-io.md)

## What you will learn

Model a recovery timeline, separate policy from execution, and explain why
Wi-Fi loss takes priority over MQTT reconnection. Allow 120 minutes.

## Preparation

Use a host checkout and pytest. Read [architecture](../architecture.md),
[MQTT](../mqtt.md), `advance_recovery_state` in
[recovery.py](../../cpynodus_ii/core/recovery.py), and
[recovery tests](../../tests/test_recovery_policy.py). Hardware investigation is
an optional instructor-supervised extension on an isolated classroom network.
Review [wifi_network_probe.py](../../testApparatus/wifi_network_probe.py) before
choosing a diagnostic; read its implementation and setup requirements first.

## Walkthrough

The policy consumes observed state and monotonic time and returns a decision.
It does not itself toggle the radio or reset the MCU. The app interprets that
decision along with failure classification and board state. A field named
`request_soft_reboot` is therefore not proof that the final action is a soft
reload. Follow the reason into [app.py](../../cpynodus_ii/app.py), including its
hard-reset reasons and cleanup functions.

Soft reload calls `supervisor.reload()`; hard reset calls
`microcontroller.reset()`. Persistent native radio/socket faults can require
the latter. Startup MQTT failures have additional bounded escalation and warm
attempt tracking; a successful CONNECT does not clear recovery before startup
publishes and subscriptions drain. Retry cadence is bounded and can change
from a short to a longer interval; do not describe it as exponential backoff
without finding that algorithm in the path you are studying.

## Lab: advance time without sleeping

Save this host-only starter as `/tmp/test_nodus_recovery_lesson.py` and replace
TODO expectations after reading the policy.

```python
"""Exercise recovery gating on a host clock.

The policy receives synthetic observations and never touches the radio.
"""
from cpynodus_ii.core.recovery import RecoveryState, advance_recovery_state


def test_wifi_loss_gates_mqtt():
    """Check Wi-Fi recovery despite a stale MQTT-connected observation."""
    decision = advance_recovery_state(
        RecoveryState(), now_monotonic=10.0,
        wifi_link_ready=False, transport_connected=True,
    )
    assert decision.state.phase == "TODO"
    assert decision.allow_mqtt_connect is None  # TODO: choose a boolean
    assert decision.attempt_wifi_reconnect is None  # TODO: choose a boolean
```

Run `PYTHONPATH=. pytest /tmp/test_nodus_recovery_lesson.py`. Add a second
observation at 11 seconds using the returned state; then one at 15 seconds.
Predict retry eligibility with the default five-second interval. Run:

```sh
pytest tests/test_network_stack.py tests/test_recovery_policy.py tests/test_app_startup.py
```

Choose the Wi-Fi timeout test. Trace its reboot reason into the app and write
the final intended reset depth. For optional hardware work, have the instructor
briefly interrupt only the node's dedicated test access point or broker, record
the agreed outage duration, restore it, and correlate serial and broker logs.
Do not create a fault on a shared operational network.

## Acceptance, hints, and reference solution

Submit the passing exercise, retry table, source-supported reset trace, and
hardware timeline only if performed. The initial answers are `"wifi"`, `False`,
and `True`. Reusing returned state gives no retry at 11 seconds and a retry at
15 seconds. Wi-Fi failure overrides stale MQTT-connected state. The app treats
`wifi_recovery_timeout` as a hard-reset reason despite the policy field name.

If predictions differ, check whether you reused the returned state or reset
its timestamps. Avoid wall-clock sleeps in host policy tests. Historical debug
notes are useful evidence of failed approaches, not instructions to restore them.

Why should an NTP clock adjustment not alter retry timing? What evidence would
justify changing reset depth rather than just adding another retry?
