"""Matrix tests for inbound TOML persistence command paths."""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.command_intake import process_inbound_messages


def _copy_docs(root, names, *, source="sensor+switch"):
    docs_root = Path(__file__).resolve().parent / "fixtures" / source.replace("+", "_")
    for name in names:
        (root / name).write_text((docs_root / name).read_text(), encoding="utf-8")


def _run_one(root, topic, payload_text):
    runtime_config = Settings.from_directory(root).runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(topic, payload_text)
    results = process_inbound_messages(
        transport,
        runtime_config,
        None,
        settings_root=root,
    )
    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].persistence_mode == "persisted"
    assert transport.published_messages[0].payload["accepted"] is True
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["error"] == ""
    assert transport.published_messages[2].topic.endswith("/meta/patch")
    return results[0], transport


def test_sensor_only_calibration_set_persists_sensor_toml():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_docs(root, ("settings.toml", "sensor_i2c.toml"))

        result, transport = _run_one(
            root,
            "nodus/aqi-x943fm/calibration/set",
            (
                '{"message_id":"cal-sensor","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.TEMP_OFFSET","value":-2.5},'
                '{"key":"Calibration.Device.ALTITUDE_METERS","value":1783.0}'
                "]}}"
            ),
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert result.runtime_config.sensor.calibration_device.temp_offset == -2.5
    assert result.runtime_config.sensor.calibration_device.altitude_meters == 1783.0
    assert sensor_doc["Calibration"]["Device"]["TEMP_OFFSET"] == -2.5
    assert sensor_doc["Calibration"]["Device"]["ALTITUDE_METERS"] == 1783.0
    assert transport.published_messages[1].payload["updated"] == 2
    assert transport.published_messages[2].payload["source"] == "calibration_set"


def test_sensor_only_config_set_persists_sensor_location():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_docs(root, ("settings.toml", "sensor_i2c.toml"))

        result, transport = _run_one(
            root,
            "nodus/aqi-x943fm/config/set",
            (
                '{"message_id":"cfg-sensor","payload":{"updates":['
                '{"section":"Sensor","key":"LOCATION","value":"SensorOnly"}'
                ']}}'
            ),
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert result.runtime_config.sensor.location == "SensorOnly"
    assert sensor_doc["Sensor"]["LOCATION"] == "SensorOnly"
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].payload["updates"] == [
        {"section": "Sensor", "key": "LOCATION", "value": "SensorOnly"}
    ]


def test_switch_only_config_set_persists_switch_location():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_docs(root, ("settings.toml", "switch.toml"), source="switch_only")

        result, transport = _run_one(
            root,
            "nodus/switch-w9umh8/config/set",
            (
                '{"message_id":"cfg-switch","payload":{"updates":['
                '{"section":"Switch","key":"SWITCH_LOCATION","value":"SwitchOnly"}'
                ']}}'
            ),
        )
        switch_doc = Settings._read_toml_file(root / Settings.SWITCH_FILE)

    assert result.runtime_config.switch.location == "SwitchOnly"
    assert switch_doc["Switch"]["SWITCH_LOCATION"] == "SwitchOnly"
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].payload["updates"] == [
        {"section": "Switch", "key": "SWITCH_LOCATION", "value": "SwitchOnly"}
    ]


def test_sensor_switch_calibration_set_persists_sensor_toml():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_docs(root, ("settings.toml", "switch.toml", "sensor_i2c.toml"))

        result, transport = _run_one(
            root,
            "nodus/aqi-x943fm/calibration/set",
            (
                '{"message_id":"cal-combo","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.System.RH_OFFSET","value":-0.75},'
                '{"key":"Calibration.Device.TEMP_OFFSET","value":1.25}'
                "]}}"
            ),
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert result.runtime_config.sensor.calibration_system.rh_offset == -0.75
    assert result.runtime_config.sensor.calibration_device.temp_offset == 1.25
    assert sensor_doc["Calibration"]["System"]["RH_OFFSET"] == -0.75
    assert sensor_doc["Calibration"]["Device"]["TEMP_OFFSET"] == 1.25
    assert transport.published_messages[1].payload["updated"] == 2
    assert transport.published_messages[2].payload["source"] == "calibration_set"


def test_sensor_switch_config_set_persists_sensor_and_switch_locations():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_docs(root, ("settings.toml", "switch.toml", "sensor_i2c.toml"))

        result, transport = _run_one(
            root,
            "nodus/aqi-x943fm/config/set",
            (
                '{"message_id":"cfg-combo","payload":{"updates":['
                '{"section":"Sensor","key":"LOCATION","value":"SensorCombo"},'
                '{"section":"Switch","key":"SWITCH_LOCATION","value":"SwitchCombo"}'
                ']}}'
            ),
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)
        switch_doc = Settings._read_toml_file(root / Settings.SWITCH_FILE)

    assert result.runtime_config.sensor.location == "SensorCombo"
    assert result.runtime_config.switch.location == "SwitchCombo"
    assert sensor_doc["Sensor"]["LOCATION"] == "SensorCombo"
    assert switch_doc["Switch"]["SWITCH_LOCATION"] == "SwitchCombo"
    assert transport.published_messages[1].payload["updated"] == 2
    assert transport.published_messages[2].payload["updates"] == [
        {"section": "Sensor", "key": "LOCATION", "value": "SensorCombo"},
        {"section": "Switch", "key": "SWITCH_LOCATION", "value": "SwitchCombo"},
    ]
