# Lesson 8: Switches and local automation

[Previous](07-mqtt-and-integrations.md) · [Index](README.md) · [Next](09-network-failures-and-recovery.md)

## What you will learn

Explain switch gating, automation ownership, and persisted state; distinguish
rule evaluation cadence from sensor sampling cadence. Allow 120 minutes.

## Preparation

Read [automations](../automations.md), [pinout](../pinout.md), and switch
sections of [configuration](../configuration.md). Hardware work requires an
operator-approved low-voltage indicator or unloaded relay fixture, one enabled
channel, a sensor, and `nodusweb`. Do not connect mains or operational equipment
for this exercise. Save the original configuration before instructor-led edits.
The host alternative needs only pytest.

## Walkthrough

`switch.toml` is the normal-runtime switch gate. A channel also needs its
configured enable pin grounded at boot. GPIO binding lives in
[switch_adapter.py](../../cpynodus_ii/hardware/switch_adapter.py); state application
is `apply_switch_state` in
[switch_service.py](../../cpynodus_ii/features/switch_service.py).

[NodusWebAutomationService](../../cpynodus_ii/features/nodusweb_automation.py)
consumes the latest successful sample at most once every five seconds. The
normal NodusWeb sensor cadence is 60 seconds. Faster evaluation therefore does
not mean fresher observations. Enabled rules own their target channels and block
manual changes until disabled. MQTT profiles do not load this evaluator.

## Lab

1. Record the configured channel ID, enable pin, output pin, and initial logical
   state. Have the operator verify physical output agrees with the status page.
2. In the automation editor, create one local sensor rule using a metric the
   node actually reports, such as Temperature. Choose a threshold near the
   current classroom reading, an On action, zero delay, and `previous_state`
   when false. Record the initial state before enabling it.
3. Observe a true and a false condition using a modest environmental change or
   an instructor-controlled threshold edit. Wait for sample/evaluation timing;
   do not use aggressive heating or rapid relay cycling.
4. While the enabled rule owns the channel, verify that manual control is
   unavailable. Disable the rule, wait for ownership release, and verify manual
   control using the five-second per-channel guard.
5. With the operator, restore the original rule configuration and safe output.
   Inspect persistence evidence in `automations.toml` and `switch.toml` where
   writable; do not equate a changed screen with a durable file update.
6. Run `pytest tests/test_nodusweb_automation.py tests/test_feature_services.py`.
   For host-only work, select the ownership and persistence-failure tests and
   explain their setup and assertions instead of claiming a physical transition.

## Acceptance and debugging

Submit a timeline of sample, evaluation, action, ownership release, and cleanup;
include test results and whether persistence was actually observed. Pass when
manual ownership restrictions and the two cadences are correctly explained.
Record unavailable hardware evidence explicitly.

If no channel appears, inspect `switch.toml` and enable-pin gating first. If a
rule is rejected, check local IDs, supported metrics, size limits, and action
format. Astral and remote targets are unsupported locally. If action is late,
check sample age and action delay before assuming a GPIO problem.

## Reference answer and reflection

With an initially Off channel, a matching On rule drives On; its
`previous_state` behavior can restore Off when the condition becomes false.
Ownership persists while the rule is enabled, even when the condition is false.
Rule replacement is persistence-first: a failed save preserves the active rule
and ownership. Other switch-state persistence paths must be assessed separately.

How does hysteresis reduce switching near a threshold? Why should a rule and a
manual button not compete for the same output?
