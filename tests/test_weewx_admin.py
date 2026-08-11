"""Tests for the limited authenticated Nodus WeeWX setup service.

The cases verify authentication, permitted manager actions, rendered status,
and rejection of unsupported administrative requests.
"""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_admin.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_admin", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _automation(**overrides):
    item = {
        "name": "desk_leds",
        "enabled": True,
        "channel_id": "S1-yuk0nv",
        "metric": "inTemp",
        "direction": "above",
        "on_threshold": 28,
        "off_threshold": 26,
        "start_time": "08:00",
        "end_time": "20:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "outside_window": "off",
        "stale_action": "off",
        "minimum_on_seconds": 300,
        "minimum_off_seconds": 300,
        "retry_seconds": 60,
    }
    item.update(overrides)
    return item


def test_admin_exposes_family_specific_change_only_calibrations():
    module = _load_module()

    aht = {item["key"] for item in module.calibration_fields("aht-yuk0nv")}
    co2 = {item["key"] for item in module.calibration_fields("co2-test")}
    soil = {item["key"] for item in module.calibration_fields("soil-test")}

    assert "Calibration.Device.TEMP_OFFSET" in aht
    assert "Calibration.Device.ALTITUDE_METERS" not in aht
    assert "Calibration.Device.CO2_OFFSET" in co2
    assert "Calibration.Device.SOIL_PH_CAL_VAL" in soil


def test_admin_exposes_labels_and_units_for_automation_metrics():
    module = _load_module()

    fields = module.metric_fields(
        ["inTemp", "inHumidity", "vpd", "absoluteHumidity", "unknownMetric"]
    )

    assert fields[:4] == [
        {"name": "inTemp", "label": "Inside temperature", "unit": "°C"},
        {"name": "inHumidity", "label": "Inside humidity", "unit": "%"},
        {"name": "vpd", "label": "VPD", "unit": "kPa"},
        {
            "name": "absoluteHumidity",
            "label": "Absolute humidity",
            "unit": "g/m³",
        },
    ]
    assert fields[-1] == {
        "name": "unknownMetric",
        "label": "unknownMetric",
        "unit": "",
    }


def test_admin_converts_and_round_trips_original_automation_rules():
    module = _load_module()

    document = module.validate_rules(
        [_automation()], ["S1-yuk0nv"], ["inTemp", "inHumidity"]
    )
    stored = document["rules"]["desk_leds"]

    assert document["schema"] == "nodus-weewx-automation/v2"
    assert stored["conditions"][0] == {
        "type": "metric",
        "metric": "inTemp",
        "direction": "above",
        "on_threshold": 28.0,
        "off_threshold": 26.0,
    }
    assert stored["conditions"][1]["start"] == "08:00"
    assert stored["actions"][0]["channel_id"] == "S1-yuk0nv"
    assert module.rules_for_ui(document)[0]["conditions"][0]["metric"] == "inTemp"


def test_admin_rejects_invalid_hysteresis_and_unknown_channels():
    module = _load_module()

    with pytest.raises(ValueError, match="OFF threshold"):
        module.validate_rules(
            [_automation(off_threshold=29)], ["S1-yuk0nv"], ["inTemp"]
        )
    with pytest.raises(ValueError, match="channel"):
        module.validate_rules(
            [_automation(channel_id="S2-other")], ["S1-yuk0nv"], ["inTemp"]
        )


def test_admin_rules_file_is_atomic_and_json_safe(tmp_path):
    module = _load_module()
    path = tmp_path / "rules.json"
    expected = {"rules": {"desk": {"enabled": True}}}

    module.write_rules(path, expected)

    assert module.read_rules(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected


def test_admin_routes_dashboard_and_protects_setup(tmp_path):
    module = _load_module()
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text("metrics dashboard", encoding="utf-8")

    assert module._http_route("/") == (
        "dashboard",
        "index.html",
        None,
        False,
    )
    assert module._http_route("/micro_vpd.png") == (
        "dashboard",
        "micro_vpd.png",
        None,
        False,
    )
    assert module._http_route("/setup/") == (
        "setup",
        "index.html",
        "text/html; charset=utf-8",
        True,
    )
    assert module._http_route("/api/status") == ("api", None, None, True)
    assert module._static_path(dashboard, "index.html").read_text(
        encoding="utf-8"
    ) == "metrics dashboard"
    assert module._static_path(dashboard, "../outside") is None


def test_admin_validates_advanced_and_or_timer_and_actions():
    module = _load_module()
    item = {
        "name": "hourly_lights",
        "enabled": True,
        "conditions": [
            {"type": "timer", "period_minutes": 60, "duration_minutes": 30},
            {"type": "or"},
            {
                "type": "metric",
                "metric": "inTemp",
                "direction": "above",
                "on_threshold": 30,
                "off_threshold": 28,
            },
        ],
        "actions": [
            {
                "channel_id": "S1-yuk0nv",
                "state": "on",
                "false_action": "opposite",
                "delay_seconds": 0,
            }
        ],
        "stale_action": "hold",
    }

    document = module.validate_rules(
        [item], ["S1-yuk0nv", "S2-yuk0nv"], ["inTemp"]
    )

    stored = document["rules"]["hourly_lights"]
    assert document["schema"] == "nodus-weewx-automation/v2"
    assert [condition["type"] for condition in stored["conditions"]] == [
        "timer",
        "or",
        "metric",
    ]
    assert stored["actions"][0]["false_action"] == "opposite"


def test_admin_accepts_readable_automation_names_with_spaces():
    module = _load_module()
    item = _automation(name="LED ONxOFF 60x30")

    document = module.validate_rules([item], ["S1-yuk0nv"], ["inTemp"])

    assert "LED ONxOFF 60x30" in document["rules"]


def test_admin_rejects_switch_dependency_cycles():
    module = _load_module()
    rules = [
        {
            "name": "first",
            "conditions": [
                {"type": "switch", "channel_id": "S2-yuk0nv", "state": "on"}
            ],
            "actions": [{"channel_id": "S1-yuk0nv", "state": "on"}],
        },
        {
            "name": "second",
            "conditions": [
                {"type": "switch", "channel_id": "S1-yuk0nv", "state": "on"}
            ],
            "actions": [{"channel_id": "S2-yuk0nv", "state": "on"}],
        },
    ]

    with pytest.raises(ValueError, match="dependency cycle"):
        module.validate_rules(
            rules, ["S1-yuk0nv", "S2-yuk0nv"], ["inTemp"]
        )
