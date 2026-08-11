"""Tests for bounded local NodusWeb switch automations.

The cases cover rule normalization, evaluation, persistence, and switch
actions while respecting constrained runtime state.
"""

from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    DisplayConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.features import nodusweb_automation as automation_module
from cpynodus_ii.features.nodusweb_automation import (
    NodusWebAutomationService,
    nodusweb_automations_enabled,
)
from cpynodus_ii.features.switch_service import SwitchChannelService, SwitchService


def _runtime(profile="nodusweb"):
    channel = SwitchChannelConfig(
        key="SWITCH_1",
        label="Fan",
        channel_id="S1-abc123",
        last_state=False,
    )
    return RuntimeConfig(
        active_profile=profile,
        sensor=DetectedSensor(
            family="i2c",
            device="aht",
            sensor_id="aht-abc123",
            display=DisplayConfig(metrics=("Temperature", "Rel-Humidity")),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-abc123",
            channel_count=1,
            channels=(channel,),
        ),
    )


def _switch_service(state=False):
    pin = SimpleNamespace(value=bool(state))
    channel = SwitchChannelService(
        key="SWITCH_1",
        channel_id="S1-abc123",
        phase="ready",
        desired_state=bool(state),
        enable_handle=SimpleNamespace(value=True),
        control_handle=pin,
    )
    return SwitchService(
        phase="ready",
        device_id="switch-abc123",
        channel_count=1,
        channels=(channel,),
    )


def _sensor_rule(name="Cool when hot"):
    return {
        "name": name,
        "enabled": True,
        "conditions": [
            {
                "type": "sensor",
                "sensor": "aht-abc123",
                "metric": "Temperature",
                "op": ">",
                "value": 25,
                "hyst": 1,
            }
        ],
        "actions": [
            {
                "switch_key": "switch-abc123::S1-abc123",
                "set": True,
                "revert_action": "previous_state",
                "delay_s": 0,
            }
        ],
    }


def test_enablement_is_exactly_nodusweb_with_switch():
    assert nodusweb_automations_enabled(_runtime()) is True
    assert nodusweb_automations_enabled(_runtime("sensorius")) is False
    runtime = _runtime()
    runtime.switch.present = False
    assert nodusweb_automations_enabled(runtime) is False


def test_save_round_trip_uses_advanced_schema(tmp_path):
    service = NodusWebAutomationService(
        _runtime(), _switch_service(), settings_root=tmp_path
    )

    result = service.save_rule("cool_when_hot", True, _sensor_rule())

    assert result["success"] is True
    text = (tmp_path / "automations.toml").read_text(encoding="utf-8")
    assert "[Advanced]" in text
    assert "cool_when_hot = { enabled=true" in text
    restored = NodusWebAutomationService(
        _runtime(), _switch_service(), settings_root=tmp_path
    )
    assert restored.payload()["rules"][0]["script"]["name"] == "Cool when hot"


def test_sensor_rule_applies_reverts_and_survives_restart(tmp_path):
    switch = _switch_service(False)
    service = NodusWebAutomationService(_runtime(), switch, settings_root=tmp_path)
    assert service.save_rule("cool", True, _sensor_rule())["success"] is True

    service.tick(now_monotonic=0, now_epoch=1_700_000_000, metrics={"Temperature": 26})
    assert switch.channels[0].control_handle.value is True
    assert service.channel_controlled(channel_id="S1-abc123") == ("Cool when hot",)

    restored = NodusWebAutomationService(_runtime(), switch, settings_root=tmp_path)
    restored.tick(
        now_monotonic=0,
        now_epoch=1_700_000_100,
        metrics={"Temperature": 23.9},
    )
    assert switch.channels[0].control_handle.value is False


def test_astral_remote_targets_and_non_nodusweb_are_rejected(tmp_path):
    service = NodusWebAutomationService(
        _runtime(), _switch_service(), settings_root=tmp_path
    )
    script = _sensor_rule()
    script["conditions"] = [{"type": "astral"}]
    assert service.save_rule("astral", True, script)["error"] == (
        "automation_astral_unsupported"
    )
    script = _sensor_rule()
    script["actions"][0]["switch_key"] = "remote::channel"
    assert service.save_rule("remote", True, script)["error"] == (
        "automation_switch_not_local"
    )
    inactive = NodusWebAutomationService(
        _runtime("weewx"), _switch_service(), settings_root=tmp_path
    )
    assert inactive.save_rule("remote", True, _sensor_rule())["error"] == (
        "automation_profile_inactive"
    )


def test_timer_or_sensor_conditions_are_supported(tmp_path):
    service = NodusWebAutomationService(
        _runtime(), _switch_service(), settings_root=tmp_path
    )
    script = _sensor_rule("Timer or hot")
    script["conditions"] = [
        {
            "type": "sensor",
            "sensor": "aht-abc123",
            "metric": "Temperature",
            "op": ">",
            "value": 99,
        },
        {"type": "or"},
        {
            "type": "timer",
            "duration_min": 5,
            "period_min": 15,
            "anchor_epoch": 1_700_000_000,
        },
    ]
    assert service.save_rule("timer_or_hot", True, script)["success"] is True
    service.tick(
        now_monotonic=0,
        now_epoch=1_700_000_001,
        metrics={"Temperature": 20},
    )
    assert service.switch_service.channels[0].control_handle.value is True


def test_failed_edit_does_not_change_live_rule_or_output(tmp_path, monkeypatch):
    switch = _switch_service(False)
    service = NodusWebAutomationService(_runtime(), switch, settings_root=tmp_path)
    assert service.save_rule("cool", True, _sensor_rule())["success"] is True
    service.tick(now_monotonic=0, metrics={"Temperature": 30})
    assert switch.channels[0].control_handle.value is True

    monkeypatch.setattr(
        automation_module, "_replace_file", lambda *_args, **_kwargs: "disk_full"
    )
    result = service.save_rule("cool", False, _sensor_rule())

    assert result["error"] == "disk_full"
    assert service.rules[0]["enabled"] is True
    assert switch.channels[0].control_handle.value is True


def test_failed_delete_does_not_release_live_rule(tmp_path, monkeypatch):
    switch = _switch_service(False)
    service = NodusWebAutomationService(_runtime(), switch, settings_root=tmp_path)
    assert service.save_rule("cool", True, _sensor_rule())["success"] is True
    service.tick(now_monotonic=0, metrics={"Temperature": 30})
    monkeypatch.setattr(
        automation_module, "_replace_file", lambda *_args, **_kwargs: "disk_full"
    )

    result = service.delete_rule("cool")

    assert result["error"] == "disk_full"
    assert service.channel_controlled(channel_id="S1-abc123") == ("Cool when hot",)
    assert switch.channels[0].control_handle.value is True
