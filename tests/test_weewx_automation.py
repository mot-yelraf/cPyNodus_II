"""Tests for the host-side WeeWX Nodus automation service."""

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

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

    assert extension.nodus_automation == expected
