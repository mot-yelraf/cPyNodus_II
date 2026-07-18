"""Tests for the host-side WeeWX Nodus switch status service."""

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_switch.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_switch", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    return {
        "MQTTSubscribeDriver": {
            "host": "broker.local",
            "topics": {
                "message": {"type": "json"},
                "nodus/aht-yuk0nv/data": {"subscribe": "true"},
            },
        }
    }


def _meta():
    return json.dumps(
        {
            "schema": "nodus-meta-switch/v1",
            "device_id": "aht-yuk0nv",
            "switch_device_id": "switch-yuk0nv",
            "location": "DevDesk",
            "channels": [
                {
                    "index": 1,
                    "label": "LEDS",
                    "channel_id": "S1-yuk0nv",
                    "state": True,
                    "state_topic": "nodus/S1-yuk0nv/state",
                    "event_topic": "nodus/S1-yuk0nv/event",
                    "set_topic": "nodus/S1-yuk0nv/config/set",
                    "ack_topic": "nodus/S1-yuk0nv/config/ack",
                    "result_topic": "nodus/S1-yuk0nv/config/result",
                }
            ],
        }
    )


def test_switch_status_derives_retained_metadata_topic():
    module = _load_module()

    assert module._derive_meta_topic(_config()) == "nodus/aht-yuk0nv/meta/switch"


def test_switch_status_finds_channels_with_enabled_automation_rules():
    module = _load_module()
    config = _config()
    config["NodusAutomation"] = {
        "enabled": "true",
        "rules": {
            "led_timer": {"enabled": "true", "channel_id": "S1-yuk0nv"},
            "disabled": {"enabled": "false", "channel_id": "S2-yuk0nv"},
        },
    }

    assert module._automation_channels(config) == {"S1-yuk0nv"}


def test_switch_status_discovers_label_state_and_topics():
    module = _load_module()
    controller = module.SwitchStatusController(
        automation_channels={"S1-yuk0nv"},
        automation_owners={"S1-yuk0nv": "LED ONxOFF 60x30"},
    )

    subscriptions = controller.update_meta(_meta(), now=100)
    status = controller.status()

    assert subscriptions == [
        "nodus/S1-yuk0nv/state",
        "nodus/S1-yuk0nv/event",
        "nodus/S1-yuk0nv/config/ack",
        "nodus/S1-yuk0nv/config/result",
    ]
    assert status["switch_device_id"] == "switch-yuk0nv"
    assert status["location"] == "DevDesk"
    assert status["channels"][0]["label"] == "LEDS"
    assert status["channels"][0]["state"] == "ON"
    assert status["channels"][0]["automation"] == "Automation enabled"
    assert status["channels"][0]["automation_enabled"] is True
    assert status["channels"][0]["mode_class"] == "automated"


def test_switch_status_accepts_retained_state_shapes():
    module = _load_module()
    controller = module.SwitchStatusController()
    controller.update_meta(_meta(), now=100)

    assert controller.update_message("nodus/S1-yuk0nv/state", "OFF", now=101)
    assert controller.status()["channels"][0]["state"] == "OFF"

    payload = json.dumps({"schema": "nodus-switch-state/v1", "state": "ON"})
    assert controller.update_message("nodus/S1-yuk0nv/state", payload, now=102)
    assert controller.status()["channels"][0]["state"] == "ON"


def test_switch_status_records_bounded_recent_events():
    module = _load_module()
    controller = module.SwitchStatusController(max_events=2)
    controller.update_meta(_meta(), now=100)

    for timestamp, state in ((101, "OFF"), (102, "ON"), (103, "OFF")):
        event = json.dumps(
            {
                "schema": "nodus-switch-event/v1",
                "channel_id": "S1-yuk0nv",
                "state": state,
                "message_id": "test-{}".format(timestamp),
                "timestamp": timestamp,
            }
        )
        assert controller.update_message(
            "nodus/S1-yuk0nv/event", event, now=timestamp
        )

    channel = controller.status()["channels"][0]
    assert channel["state"] == "OFF"
    assert [item["timestamp"] for item in channel["events"]] == [103, 102]
    assert channel["events"][0]["display"].endswith("Manual : OFF")
    assert channel["events"][0]["received_at"] == 103
    assert channel["events"][0]["time"] == time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(103)
    )


def test_switch_skin_repairs_prior_rtc_wall_time_without_second_offset():
    module = _load_module()
    status = {
        "channels": [
            {
                "channel_id": "S1-yuk0nv",
                "automation": "Automation enabled",
                "events": [
                    {
                        "timestamp": 3600,
                        "time": "wrong",
                        "rule": "LED rule",
                        "state": "ON",
                    }
                ],
            }
        ]
    }

    event = module._skin_status(status)["channels"][0]["events"][0]

    assert event["time"] == "1970-01-01 01:00:00"
    assert event["display"] == "1970-01-01 01:00:00 LED rule : ON"


def test_switch_skin_limits_cached_event_display_to_twenty():
    module = _load_module()
    status = {
        "channels": [
            {
                "channel_id": "S1-yuk0nv",
                "events": [
                    {"time": str(index), "state": "ON"}
                    for index in range(25)
                ],
            }
        ]
    }

    events = module._skin_status(status)["channels"][0]["events"]

    assert len(events) == 20
    assert events[0]["display"] == "0 Manual : ON"
    assert events[-1]["display"] == "19 Manual : ON"


def test_switch_manual_mode_exposes_countdown_without_automation_background():
    module = _load_module()
    controller = module.SwitchStatusController(
        manual_countdowns={"S1-yuk0nv": 1800}
    )

    controller.update_meta(_meta(), now=100)
    channel = controller.status()["channels"][0]

    assert channel["automation_enabled"] is False
    assert channel["mode_class"] == "manual"
    assert channel["automation"] == "Manual mode · 1800s countdown"


def test_switch_control_file_round_trips_countdown_and_deadline(tmp_path):
    module = _load_module()
    path = tmp_path / "control.json"

    module._write_control(
        path,
        {"S1-yuk0nv": 1800},
        {"S1-yuk0nv": 1234567890},
    )

    assert module._read_control(path) == (
        {"S1-yuk0nv": 1800},
        {"S1-yuk0nv": 1234567890},
    )


def test_switch_manual_toggle_rejects_automation_and_guards_rapid_commands():
    module = _load_module()
    service = object.__new__(module.NodusSwitchStatus)
    service.lock = threading.RLock()
    service.manual_guard_until = {}
    service.controller = module.SwitchStatusController()
    service.controller.update_meta(_meta(), now=100)
    service._set_manual_state = lambda channel_id, desired: {
        "channel_id": channel_id,
        "state": "ON" if desired else "OFF",
    }

    result = service.admin_toggle_switch({"channel_id": "S1-yuk0nv"})

    assert result["channel"]["state"] == "OFF"
    with pytest.raises(ValueError, match="wait 5 seconds"):
        service.admin_toggle_switch({"channel_id": "S1-yuk0nv"})

    service.controller.update_control_state(
        automation_owners={"S1-yuk0nv": "LED rule"}
    )
    service.manual_guard_until.clear()
    with pytest.raises(ValueError, match="disable the switch automation"):
        service.admin_toggle_switch({"channel_id": "S1-yuk0nv"})


def test_switch_admin_updates_location_and_labels_with_paced_config_requests():
    module = _load_module()
    service = object.__new__(module.NodusSwitchStatus)
    service.lock = threading.RLock()
    service.controller = module.SwitchStatusController()
    service.controller.update_meta(_meta(), now=100)
    service.manual_countdowns = {}
    service.manual_deadlines = {}
    service.admin_snapshot = lambda: {
        "switch": service.controller.status()
    }
    service._message_id = lambda prefix: "{}-id".format(prefix)
    requests = []
    service._request = lambda kind, payload: requests.append((kind, payload))
    service._save_manual_control = lambda: None

    result = service.admin_update_switch(
        {
            "location": "Propagation Bench",
            "labels": [
                {"channel_id": "S1-yuk0nv", "label": "Grow Light"}
            ],
        }
    )

    assert result["ok"] is True
    assert [request[1]["payload"]["updates"][0]["key"] for request in requests] == [
        "SWITCH_LOCATION",
        "SWITCH_1_LABEL",
    ]
    assert requests[1][1]["payload"]["updates"][0]["value"] == "Grow Light"
    assert service.controller.location == "Propagation Bench"
    assert service.controller.channels["S1-yuk0nv"]["label"] == "Grow Light"


def test_switch_admin_rejects_unknown_or_blank_channel_labels():
    module = _load_module()
    service = object.__new__(module.NodusSwitchStatus)
    service.lock = threading.RLock()
    service.controller = module.SwitchStatusController()
    service.controller.update_meta(_meta(), now=100)
    service.admin_snapshot = lambda: {
        "switch": service.controller.status()
    }
    service._request = lambda _kind, _payload: None

    with pytest.raises(ValueError, match="channel is invalid"):
        service.admin_update_switch(
            {
                "location": "DevDesk",
                "labels": [{"channel_id": "missing", "label": "Light"}],
            }
        )
    with pytest.raises(ValueError, match="label is required"):
        service.admin_update_switch(
            {
                "location": "DevDesk",
                "labels": [{"channel_id": "S1-yuk0nv", "label": ""}],
            }
        )


def test_switch_admin_snapshot_exposes_retained_identity_and_service_statistics(
    monkeypatch, tmp_path
):
    module = _load_module()
    admin_path = MODULE_PATH.with_name("nodus_admin.py")
    spec = importlib.util.spec_from_file_location("user.nodus_admin", admin_path)
    admin_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin_module)
    user_package = ModuleType("user")
    user_package.nodus_admin = admin_module
    monkeypatch.setitem(sys.modules, "user", user_package)
    monkeypatch.setitem(sys.modules, "user.nodus_admin", admin_module)

    service = object.__new__(module.NodusSwitchStatus)
    service.lock = threading.RLock()
    service.controller = module.SwitchStatusController()
    service.controller.update_meta(_meta(), now=100)
    service.device_meta = {
        "device_id": "aht-yuk0nv",
        "mcu": "xesp32s3",
        "version": "v0.26.199.1",
        "network": {"ipv4addr": "10.0.0.233"},
        "mqtt": {"broker": "broker.local", "active_broker": "10.0.0.20"},
        "sensor": {
            "device": "aht",
            "hardware": "AHTx0",
            "location": "Propagation Bench",
        },
    }
    service.mqtt_host = "fallback.local"
    service.mqtt_connected = True
    service.service_started = 10
    service.mqtt_connected_at = 20
    service.last_disconnect_at = 30
    service.disconnect_count = 2
    service.last_message_at = 40
    service.messages_received = 50
    service.rules_file = str(tmp_path / "missing-rules.json")
    service.config_dict = _config()

    snapshot = service.admin_snapshot()

    assert snapshot["sensor"]["location"] == "Propagation Bench"
    assert snapshot["info"] == {
        "service_started": 10,
        "mqtt_connected_at": 20,
        "last_disconnect_at": 30,
        "disconnect_count": 2,
        "last_packet_at": 40,
        "packets_received": 50,
        "board_type": "xesp32s3",
        "firmware_version": "v0.26.199.1",
        "sensor_device": "aht",
        "sensor_hardware": "AHTx0",
        "ip_address": "10.0.0.233",
        "broker": "10.0.0.20",
        "broker_status": "Connected",
    }


def test_switch_status_preserves_events_across_metadata_refresh():
    module = _load_module()
    controller = module.SwitchStatusController()
    controller.update_meta(_meta(), now=100)
    event = json.dumps(
        {
            "schema": "nodus-switch-event/v1",
            "channel_id": "S1-yuk0nv",
            "state": "OFF",
            "timestamp": 101,
        }
    )
    controller.update_message("nodus/S1-yuk0nv/event", event, now=101)

    controller.update_meta(_meta(), now=102)

    assert len(controller.status()["channels"][0]["events"]) == 1


def test_switch_search_list_reads_cached_status(tmp_path):
    module = _load_module()
    path = tmp_path / "switch.json"
    expected = {
        "device_id": "aht-yuk0nv",
        "switch_device_id": "switch-yuk0nv",
        "location": "DevDesk",
        "updated": 123,
        "channels": [{"label": "LEDS", "state": "ON", "events": []}],
    }
    path.write_text(json.dumps(expected), encoding="utf-8")
    generator = SimpleNamespace(
        skin_dict={"NodusSwitchStatus": {"status_file": str(path)}}
    )

    extension = module.NodusSwitchStatusSearchList(generator)

    channel = extension.nodus_switch["channels"][0]
    assert extension.nodus_switch["device_id"] == expected["device_id"]
    assert channel["label"] == "LEDS"
    assert channel["automation_enabled"] is False
    assert channel["mode_class"] == "manual"
    assert channel["automation"] == "Manual mode"


def test_switch_search_list_normalizes_prior_timer_ownership(tmp_path):
    module = _load_module()
    path = tmp_path / "switch.json"
    path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "channel_id": "S1-yuk0nv",
                        "state": "OFF",
                        "automation": "Timer enabled",
                        "events": [
                            {
                                "time": "2026-07-17 11:54:50",
                                "state": "OFF",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    generator = SimpleNamespace(
        skin_dict={"NodusSwitchStatus": {"status_file": str(path)}}
    )

    channel = module.NodusSwitchStatusSearchList(generator).nodus_switch[
        "channels"
    ][0]

    assert channel["automation_enabled"] is True
    assert channel["mode_class"] == "automated"
    assert channel["automation"] == "Automation enabled"
    assert channel["events"][0]["display"] == (
        "2026-07-17 11:54:50 Manual : OFF"
    )


def test_switch_backend_requires_correlated_ack_and_result():
    module = _load_module()
    service = object.__new__(module.NodusSwitchStatus)
    service.lock = threading.RLock()
    service.pending = {}
    service.command_timeout = 1
    service.device_topic = "nodus/aht-yuk0nv"

    class Result:
        rc = 0

    class Client:
        def publish(self, _topic, payload, qos=0):
            message_id = json.loads(payload)["message_id"]
            service._update_pending(
                "nodus/aht-yuk0nv/config/ack",
                json.dumps({"message_id": message_id, "accepted": True}).encode(),
            )
            service._update_pending(
                "nodus/aht-yuk0nv/config/result",
                json.dumps({"message_id": message_id, "applied": True}).encode(),
            )
            return Result()

    service.client = Client()
    response = service._request(
        "config", {"message_id": "cfg-1", "payload": {"updates": []}}
    )

    assert response["applied"] is True
    assert service.pending == {}
