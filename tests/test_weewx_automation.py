"""Test the host-side WeeWX Nodus automation service.

The suite covers rule parsing, evaluation, MQTT commands, status persistence,
enablement, and report-facing automation state.
"""

import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_automation.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_automation", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rule(module, **overrides):
    source = {
        "enabled": "true",
        "channel_id": "S1-ykdvea",
        "condition_1": "inTemp, above, 27.0, 25.0",
        "start_time": "00:00",
        "end_time": "00:00",
        "days": "mon, tue, wed, thu, fri, sat, sun",
        "outside_window": "off",
        "stale_action": "off",
        "stale_after": "180",
        "minimum_on_seconds": "0",
        "minimum_off_seconds": "0",
    }
    source.update(overrides)
    return module._parse_rules(
        {"rules": {"greenhouse_fan": source}, "stale_after": "180"}
    )[0]


def _meta_payload(state=False):
    return json.dumps(
        {
            "schema": "nodus-meta-switch/v1",
            "device_id": "aht-ykdvea",
            "switch_device_id": "switch-ykdvea",
            "channels": [
                {
                    "index": 1,
                    "label": "Fan",
                    "channel_id": "S1-ykdvea",
                    "state": state,
                    "set_topic": "nodus/S1-ykdvea/config/set",
                    "state_topic": "nodus/S1-ykdvea/state",
                    "ack_topic": "nodus/S1-ykdvea/config/ack",
                    "result_topic": "nodus/S1-ykdvea/config/result",
                }
            ],
        }
    )


def _advanced_rule(module, conditions, actions=None, **overrides):
    source = {
        "enabled": True,
        "conditions": conditions,
        "actions": actions
        or [
            {
                "channel_id": "S1-ykdvea",
                "state": "on",
                "false_action": "opposite",
            }
        ],
        "stale_action": "off",
        "stale_after": 180,
        "minimum_on_seconds": 0,
        "minimum_off_seconds": 0,
        "retry_seconds": 5,
    }
    source.update(overrides)
    return module._parse_rules({"rules": {"advanced": source}})[0]


def _two_channel_meta(first=False, second=False):
    data = json.loads(_meta_payload(first))
    data["channels"].append(
        {
            "index": 2,
            "label": "Pump",
            "channel_id": "S2-ykdvea",
            "state": second,
            "set_topic": "nodus/S2-ykdvea/config/set",
            "state_topic": "nodus/S2-ykdvea/state",
            "ack_topic": "nodus/S2-ykdvea/config/ack",
            "result_topic": "nodus/S2-ykdvea/config/result",
        }
    )
    return json.dumps(data)


def test_rule_parser_supports_anded_conditions_and_hysteresis():
    module = _load_module()
    rule = _rule(
        module,
        condition_2="inHumidity, below, 45.0, 50.0",
    )

    assert [item["observation"] for item in rule["conditions"]] == [
        "inTemp",
        "inHumidity",
    ]
    assert module._condition_active(rule["conditions"][0], 27.0) is True
    assert module._condition_active(rule["conditions"][0], 26.0, True) is True
    assert module._condition_active(rule["conditions"][0], 25.0, True) is False
    assert module._condition_active(rule["conditions"][1], 45.0) is True
    assert module._condition_active(rule["conditions"][1], 48.0, True) is True
    assert module._condition_active(rule["conditions"][1], 50.0, True) is False


def test_rule_parser_rejects_competing_rules_for_one_channel():
    module = _load_module()
    source = {
        "enabled": "true",
        "channel_id": "S1-ykdvea",
        "condition_1": "inTemp, above, 27.0, 25.0",
    }

    with pytest.raises(ValueError, match="more than one enabled rule"):
        module._parse_rules(
            {"rules": {"fan_one": dict(source), "fan_two": dict(source)}}
        )


def test_disabled_rules_are_display_only_and_preserve_actions():
    module = _load_module()
    source = {
        "rules": {
            "lights": {
                "enabled": False,
                "conditions": [{"type": "timer"}],
                "actions": [{"channel_id": "S1-ykdvea"}],
            }
        }
    }

    assert module._parse_rules(source) == []
    assert module._disabled_rules(source) == [
        {"name": "lights", "channel_ids": ["S1-ykdvea"]}
    ]


def test_time_windows_support_daytime_overnight_and_all_day():
    module = _load_module()
    morning = time.mktime((2026, 7, 17, 9, 0, 0, 0, 0, -1))
    evening = time.mktime((2026, 7, 17, 21, 0, 0, 0, 0, -1))
    early = time.mktime((2026, 7, 17, 5, 0, 0, 0, 0, -1))

    assert module._time_window_active(morning, 8 * 60, 20 * 60)
    assert not module._time_window_active(evening, 8 * 60, 20 * 60)
    assert module._time_window_active(evening, 20 * 60, 6 * 60)
    assert module._time_window_active(early, 20 * 60, 6 * 60)
    assert module._time_window_active(morning, 0, 0)


def test_controller_requires_all_conditions_and_publishes_canonical_command():
    module = _load_module()
    published = []
    rule = _rule(
        module,
        condition_2="inHumidity, above, 70.0, 65.0",
    )
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    subscriptions = controller.update_meta(_meta_payload(), now=100)

    controller.evaluate(
        {"dateTime": 200, "inTemp": 28.0, "inHumidity": 60.0}, now=200
    )
    assert published == []

    controller.evaluate(
        {"dateTime": 201, "inTemp": 28.0, "inHumidity": 72.0}, now=201
    )

    assert subscriptions == [
        "nodus/S1-ykdvea/state",
        "nodus/S1-ykdvea/config/ack",
        "nodus/S1-ykdvea/config/result",
    ]
    assert published[0][0] == "nodus/S1-ykdvea/config/set"
    assert published[0][2] is False
    command = json.loads(published[0][1])
    assert command["restart"] is False
    assert command["payload"]["updates"] == [
        {
            "section": "Switch",
            "key": "SWITCH_1_LAST_STATE",
            "value": True,
            "name": "switch.toml",
        }
    ]


def test_weewx_automation_contract_topics_and_status_payload():
    module = _load_module()
    rule = _advanced_rule(
        module,
        [{"type": "timer", "duration_minutes": 30, "period_minutes": 60}],
        actions=[
            {"channel_id": "S1-ykdvea", "state": "on"},
            {"channel_id": "S2-ykdvea", "state": "off"},
        ],
    )

    assert module._automation_contract_topics(
        "nodus/aht-ykdvea/meta/switch"
    ) == (
        "nodus/aht-ykdvea/automation/weewx/status",
        "nodus/aht-ykdvea/automation/weewx/availability",
    )
    assert module._automation_contract_status([rule], now=123) == {
        "schema": "nodus-automation-status/v1",
        "controller": "weewx",
        "controller_id": "weewx-nodus-automation",
        "updated_at": 123,
        "channels": [
            {
                "channel_id": "S1-ykdvea",
                "automations": ["advanced"],
                "enabled": True,
            },
            {
                "channel_id": "S2-ykdvea",
                "automations": ["advanced"],
                "enabled": True,
            },
        ],
    }


def test_weewx_service_publishes_retained_contract_documents():
    module = _load_module()

    class Client:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload, qos=0, retain=False):
            self.published.append((topic, json.loads(payload), qos, retain))
            return SimpleNamespace(rc=0)

    service = object.__new__(module.NodusAutomation)
    service.enabled = True
    service.client = Client()
    service.controller = SimpleNamespace(rules=[_rule(module)])
    service.automation_status_topic = "nodus/aht/automation/weewx/status"
    service.automation_availability_topic = "nodus/aht/automation/weewx/availability"
    service.last_contract_status = ""
    service.last_availability_publish = 0

    assert service._publish_contract_status(force=True) is True
    assert service._publish_contract_availability("online") is True
    assert [item[0] for item in service.client.published] == [
        service.automation_status_topic,
        service.automation_availability_topic,
    ]
    assert all(item[3] is True for item in service.client.published)


def test_controller_confirms_only_after_ack_result_and_matching_state():
    module = _load_module()
    published = []
    rule = _rule(module)
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    controller.update_meta(_meta_payload(), now=100)
    controller.evaluate({"dateTime": 200, "inTemp": 28.0}, now=200)
    message_id = json.loads(published[0][1])["message_id"]

    controller.update_response(
        "nodus/S1-ykdvea/config/ack",
        json.dumps({"message_id": message_id, "accepted": True}),
        now=201,
    )
    controller.update_response(
        "nodus/S1-ykdvea/config/result",
        json.dumps({"message_id": message_id, "applied": True}),
        now=202,
    )
    assert message_id in controller.pending

    controller.update_state("nodus/S1-ykdvea/state", "ON", now=203)

    assert controller.pending == {}
    assert rule["last_action"].startswith("confirmed ON at ")
    assert controller.channels["S1-ykdvea"]["state"] is True


def test_stale_data_and_time_window_can_fail_safe_off():
    module = _load_module()
    published = []
    rule = _rule(module, stale_after="10")
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    controller.update_meta(_meta_payload(state=True), now=100)
    controller.evaluate({"dateTime": 100, "inTemp": 28.0}, now=100)
    assert published == []

    controller.evaluate(now=111)

    assert json.loads(published[0][1])["payload"]["updates"][0]["value"] is False
    assert rule["last_decision"] == "sensor data stale"


def test_command_timeout_observes_retry_cooldown():
    module = _load_module()
    published = []
    rule = _rule(module, retry_seconds="60", stale_after="300")
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
        command_timeout=5,
    )
    controller.update_meta(_meta_payload(), now=100)
    controller.evaluate({"dateTime": 200, "inTemp": 28.0}, now=200)

    controller.evaluate(now=206)
    assert len(published) == 1
    assert rule["last_error"] == "command confirmation timed out"
    assert "retry cooldown" in rule["last_decision"]

    controller.evaluate(now=260)
    assert len(published) == 2


def test_replayed_retained_meta_does_not_duplicate_pending_command():
    module = _load_module()
    published = []
    rule = _rule(module, retry_seconds="60", stale_after="300")
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
        command_timeout=15,
    )
    metadata = _meta_payload(state=False)
    controller.update_meta(metadata, now=100)
    controller.evaluate({"dateTime": 200, "inTemp": 28.0}, now=200)

    controller.update_meta(metadata, now=201)
    controller.evaluate(now=205)
    controller.evaluate(now=216)

    assert len(published) == 1
    assert controller.pending == {}
    assert rule["last_error"] == "command confirmation timed out"
    assert "retry cooldown" in rule["last_decision"]


def test_failed_publish_observes_retry_cooldown():
    module = _load_module()
    attempted = []
    rule = _rule(module, retry_seconds="60", stale_after="300")

    def publisher(topic, payload, retain):
        attempted.append((topic, payload, retain))
        return False

    controller = module.AutomationController(
        [rule],
        publisher,
    )
    controller.update_meta(_meta_payload(), now=100)

    controller.evaluate({"dateTime": 200, "inTemp": 28.0}, now=200)
    controller.evaluate(now=205)

    assert len(attempted) == 1
    assert rule["last_error"] == "MQTT publish failed"
    assert "retry cooldown" in rule["last_decision"]

    controller.evaluate(now=260)
    assert len(attempted) == 2


def test_switch_state_parser_accepts_startup_json_and_runtime_scalar():
    module = _load_module()

    assert module._normalize_switch_state("ON") is True
    assert module._normalize_switch_state(b"OFF") is False
    assert (
        module._normalize_switch_state(
            json.dumps({"schema": "nodus-switch-state/v1", "state": "ON"})
        )
        is True
    )


def test_skin_status_extension_reads_automation_status(tmp_path):
    module = _load_module()
    path = tmp_path / "status.json"
    expected = {
        "enabled": True,
        "updated": 123,
        "rules": [{"name": "fan", "state": "ON"}],
    }
    path.write_text(json.dumps(expected), encoding="utf-8")
    generator = SimpleNamespace(
        skin_dict={"NodusAutomationStatus": {"status_file": str(path)}}
    )

    extension = module.NodusAutomationStatus(generator)

    assert extension.nodus_automation["enabled"] is True
    assert extension.nodus_automation["updated"] == 123
    assert extension.nodus_automation["rules"] == [
        {
            "name": "fan",
            "state": "ON",
            "enabled": True,
            "enable_state": "Enabled",
        }
    ]


def test_service_status_includes_disabled_rules_without_evaluation(tmp_path):
    module = _load_module()
    service = object.__new__(module.NodusAutomation)
    service.enabled = True
    service.status_file = str(tmp_path / "status.json")
    service.last_status = ""
    service.rule_order = ["enabled_rule", "disabled_rule"]
    service.disabled_rules = [
        {"name": "disabled_rule", "channel_ids": ["S1-ykdvea"]}
    ]
    enabled_rule = _rule(module)
    enabled_rule["name"] = "enabled_rule"
    service.controller = module.AutomationController(
        [enabled_rule], lambda *_args: True
    )
    service.controller.update_meta(_meta_payload(True), now=100)

    service._save_status()

    status = json.loads(Path(service.status_file).read_text(encoding="utf-8"))
    assert [rule["enable_state"] for rule in status["rules"]] == [
        "Enabled",
        "Disabled",
    ]
    assert status["rules"][1]["decision"] == "disabled"


def test_ui_managed_rules_file_replaces_static_rules(tmp_path):
    module = _load_module()
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "rules": {
                    "ui_rule": {
                        "enabled": True,
                        "channel_id": "S1-ykdvea",
                        "condition_1": "inTemp, above, 28, 26",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    rules = module._parse_rules(module._rules_source({"rules_file": str(path)}))

    assert [rule["name"] for rule in rules] == ["ui_rule"]


def test_advanced_groups_and_within_groups_and_or_across_groups():
    module = _load_module()
    published = []
    rule = _advanced_rule(
        module,
        [
            {
                "type": "metric",
                "metric": "inTemp",
                "direction": "above",
                "on_threshold": 28,
                "off_threshold": 26,
            },
            {"type": "or"},
            {
                "type": "time",
                "start": "00:00",
                "end": "00:00",
                "days": "mon,tue,wed,thu,fri,sat,sun",
            },
        ],
    )
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    controller.update_meta(_meta_payload(), now=100)

    controller.evaluate(now=200)

    assert json.loads(published[0][1])["payload"]["updates"][0]["value"] is True
    assert rule["last_decision"] == "condition groups active"


def test_advanced_timer_produces_thirty_minute_on_off_cycle():
    module = _load_module()
    condition = module._parse_advanced_condition(
        {
            "type": "timer",
            "duration_minutes": 30,
            "period_minutes": 60,
            "anchor_epoch": 0,
        },
        "timer",
    )
    rule = {"stale_after": 180}
    controller = module.AutomationController([], lambda *_args: True)
    on_time = time.mktime((2026, 7, 17, 12, 15, 0, 0, 0, -1))
    off_time = time.mktime((2026, 7, 17, 12, 45, 0, 0, 0, -1))

    assert controller._condition_result(condition, rule, on_time) is True
    assert controller._condition_result(condition, rule, off_time) is False


def test_advanced_switch_state_condition_controls_another_channel():
    module = _load_module()
    published = []
    rule = _advanced_rule(
        module,
        [{"type": "switch", "channel_id": "S2-ykdvea", "state": "on"}],
    )
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    controller.update_meta(_two_channel_meta(second=True), now=100)

    controller.evaluate(now=200)

    assert published[0][0] == "nodus/S1-ykdvea/config/set"
    assert json.loads(published[0][1])["payload"]["updates"][0]["value"] is True


def test_advanced_rule_can_apply_multiple_switch_actions():
    module = _load_module()
    published = []
    rule = _advanced_rule(
        module,
        [{"type": "timer", "duration_minutes": 30, "period_minutes": 60}],
        actions=[
            {"channel_id": "S1-ykdvea", "state": "on"},
            {"channel_id": "S2-ykdvea", "state": "on"},
        ],
    )
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
    )
    controller.update_meta(_two_channel_meta(), now=100)
    active_time = time.mktime((2026, 7, 17, 12, 15, 0, 0, 0, -1))

    controller.evaluate(now=active_time)

    assert {item[0] for item in published} == {
        "nodus/S1-ykdvea/config/set",
        "nodus/S2-ykdvea/config/set",
    }


def test_astral_condition_uses_station_coordinates(monkeypatch):
    module = _load_module()
    astral = ModuleType("astral")
    astral.Observer = lambda latitude, longitude: (latitude, longitude)
    astral_sun = ModuleType("astral.sun")

    def fake_sun(_observer, date, tzinfo):
        base = datetime(date.year, date.month, date.day, tzinfo=tzinfo)
        return {
            "sunrise": base.replace(hour=6),
            "sunset": base.replace(hour=20),
        }

    astral_sun.sun = fake_sun
    monkeypatch.setitem(sys.modules, "astral", astral)
    monkeypatch.setitem(sys.modules, "astral.sun", astral_sun)
    controller = module.AutomationController(
        [], lambda *_args: True, latitude=32.7, longitude=-108.2
    )
    condition = {
        "event": "sunrise_to_sunset",
        "offset_minutes": 0,
        "days": list(module._DAY_NAMES),
    }
    noon = time.mktime((2026, 7, 17, 12, 0, 0, 0, 0, -1))

    assert controller._astral_result(condition, noon) is True


def test_previous_state_ownership_is_persisted(tmp_path):
    module = _load_module()
    path = tmp_path / "runtime.json"
    published = []
    rule = _advanced_rule(
        module,
        [{"type": "timer", "duration_minutes": 30, "period_minutes": 60}],
        actions=[
            {
                "channel_id": "S1-ykdvea",
                "state": "on",
                "false_action": "previous_state",
            }
        ],
    )
    controller = module.AutomationController(
        [rule],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
        runtime_file=str(path),
    )
    controller.update_meta(_meta_payload(state=False), now=100)
    active_time = time.mktime((2026, 7, 17, 12, 15, 0, 0, 0, -1))

    controller.evaluate(now=active_time)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["active_actions"]["advanced|S1-ykdvea"]["previous"] is False


def test_removed_previous_state_rule_restores_owned_switch(tmp_path):
    module = _load_module()
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "active_actions": {
                    "removed|S1-ykdvea": {
                        "channel_id": "S1-ykdvea",
                        "previous": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    published = []
    controller = module.AutomationController(
        [],
        lambda topic, payload, retain: published.append((topic, payload, retain)),
        runtime_file=str(path),
    )
    controller.update_meta(_meta_payload(state=True), now=100)

    controller.evaluate(now=200)

    command = json.loads(published[0][1])
    assert command["payload"]["updates"][0]["value"] is False
