"""Tests for persisted switch state and calibration storage flows.

The cases verify atomic updates, unchanged values, and failure handling for
runtime state written back to TOML documents.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.command_intake import (
    process_calibration_message,
    process_device_config_message,
    process_switch_command_message,
)


def _repo_template_path(name):
    repo_root = Path(__file__).resolve().parents[1]
    if name == Settings.SETTINGS_DEF_FILE:
        return repo_root / "boards" / name
    return repo_root / "boards" / "pico2w" / "templates" / name


def test_device_config_message_persists_settings_toml_and_reloads_runtime_config():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/switch-w9umh8/config/set",
            payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"switch-new"}]}}',
            settings_root=tmpdir_path,
        )
        second = process_device_config_message(
            transport,
            result.runtime_config,
            topic="nodus/switch-w9umh8/config/set",
            payload_text='{"message_id":"cfg-2","payload":{"updates":[{"section":"MQTT","key":"BROKER","value":"broker2.local"}]}}',
            settings_root=tmpdir_path,
        )
        reloaded = Settings.from_directory(tmpdir_path).runtime_config()

    assert result.phase == "published"
    assert second.phase == "published"
    assert second.runtime_config.network.hostname == "switch-new"
    assert second.runtime_config.mqtt.broker == "broker2.local"
    assert reloaded.network.hostname == "switch-new"
    assert reloaded.mqtt.broker == "broker2.local"
    assert "nodus/switch-w9umh8/meta/patch" in [
        message.topic for message in transport.published_messages
    ]


def test_switch_only_sensor_location_update_persists_switch_location():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/switch-w9umh8/config/set",
            payload_text='{"message_id":"cfg-loc","payload":{"updates":[{"section":"Sensor","key":"LOCATION","value":"OfficeDesk","name":"sensor_i2c.toml"}]}}',
            settings_root=tmpdir_path,
        )
        switch_doc = Settings._read_toml_file(tmpdir_path / Settings.SWITCH_FILE)
        reloaded = Settings.from_directory(tmpdir_path).runtime_config()
        sensor_file_exists = (tmpdir_path / Settings.SENSOR_I2C_FILE).exists()

    assert result.phase == "published"
    assert result.runtime_config.sensor.location == ""
    assert result.runtime_config.switch.location == "OfficeDesk"
    assert reloaded.switch.location == "OfficeDesk"
    assert switch_doc["Switch"]["SWITCH_LOCATION"] == "OfficeDesk"
    assert not sensor_file_exists
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Switch",
            "key": "SWITCH_LOCATION",
            "value": "OfficeDesk",
        },
    ]


def test_device_config_message_reports_pystack_persistence_failure(monkeypatch):
    from cpynodus_ii.features import scalar_persistence

    docs_root = Path(__file__).resolve().parent / "fixtures" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        def _raise_pystack(*args, **kwargs):
            raise RuntimeError("pystack exhausted")

        monkeypatch.setattr(scalar_persistence, "write_toml_scalar", _raise_pystack)
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/switch-w9umh8/config/set",
            payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Switch","key":"SWITCH_LOCATION","value":"OfficeDesk"}]}}',
            settings_root=tmpdir_path,
        )

    assert result.phase == "published"
    assert result.errors == ("config_persist_pystack",)
    assert result.published_count == 3
    assert transport.published_messages[0].payload["accepted"] is True
    assert transport.published_messages[1].payload["applied"] is True


def test_calibration_message_persists_active_sensor_toml_without_runtime_reload(
    monkeypatch,
):
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        def _fail_on_reload(cls, root):
            raise AssertionError(
                "calibration persistence should not reload runtime config"
            )

        def _fail_on_dump(cls, path, document):
            raise AssertionError("calibration update should not dump full TOML")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        monkeypatch.setattr(Settings, "_dump_toml_for_path", classmethod(_fail_on_dump))
        result = process_calibration_message(
            transport,
            runtime_config,
            topic="nodus/aqi-x943fm/calibration/set",
            payload_text='{"message_id":"cal-1","action":"apply","payload":{"offsets":[{"key":"Calibration.System.RH_OFFSET","value":-0.5}]}}',
            settings_root=tmpdir_path,
        )
        sensor_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_I2C_FILE)

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert result.runtime_config.sensor.calibration_system.rh_offset == -0.5
    assert sensor_doc["Calibration"]["System"]["RH_OFFSET"] == -0.5
    assert transport.published_messages[-1].payload["updates"][0]["value"] == -0.5


def test_calibration_message_reports_pystack_persistence_failure(monkeypatch):
    from cpynodus_ii.features import scalar_persistence

    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        def _raise_pystack(*args, **kwargs):
            raise RuntimeError("pystack exhausted")

        monkeypatch.setattr(scalar_persistence, "write_toml_scalar", _raise_pystack)
        result = process_calibration_message(
            transport,
            runtime_config,
            topic="nodus/aqi-x943fm/calibration/set",
            payload_text='{"message_id":"cal-1","action":"apply","payload":{"offsets":[{"key":"Calibration.Device.TEMP_OFFSET","value":1.5}]}}',
            settings_root=tmpdir_path,
        )

    assert result.phase == "published"
    assert result.errors == ("calibration_offset_persist_pystack",)
    assert result.published_count == 3
    assert result.persistence_mode == "volatile"
    assert result.runtime_config.sensor.calibration_device.temp_offset == 1.5
    assert transport.published_messages[0].payload["accepted"] is True
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[1].payload["error"] == ""
    assert transport.published_messages[2].topic == "nodus/aqi-x943fm/meta/patch"


def test_soil_ph_calibration_persists_without_recursive_toml_dump(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml.def", "sensor_soil.toml.def"):
            source = _repo_template_path(name)
            target = tmpdir_path / name.replace(".def", "")
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        soil_path = tmpdir_path / Settings.SENSOR_SOIL_FILE
        soil_path.write_text(
            soil_path.read_text(encoding="utf-8")
            .replace('DEVICE = ""', 'DEVICE = "soil"')
            .replace('SENSOR_ID = ""', 'SENSOR_ID = "soil-bd1234"'),
            encoding="utf-8",
        )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        def _fail_on_reload(cls, root):
            raise AssertionError(
                "calibration persistence should not reload runtime config"
            )

        def _fail_on_dump(cls, path, document):
            raise AssertionError("soil calibration update should not dump full TOML")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        monkeypatch.setattr(Settings, "_dump_toml_for_path", classmethod(_fail_on_dump))
        result = process_calibration_message(
            transport,
            runtime_config,
            topic="nodus/soil-bd1234/calibration/set",
            payload_text='{"message_id":"soil-ph-1","action":"apply","payload":{"offsets":[{"key":"soil_ph_offset","value":0.42}]}}',
            settings_root=tmpdir_path,
        )
        soil_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_SOIL_FILE)

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert result.runtime_config.sensor.calibration_device.soil_ph_cal_val == 0.42
    assert soil_doc["Calibration"]["Device"]["SOIL_PH_CAL_VAL"] == 0.42


def test_soil_npk_target_persists_without_runtime_reload(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml.def", "sensor_soil.toml.def"):
            source = _repo_template_path(name)
            target = tmpdir_path / name.replace(".def", "")
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        soil_path = tmpdir_path / Settings.SENSOR_SOIL_FILE
        soil_path.write_text(
            soil_path.read_text(encoding="utf-8")
            .replace('DEVICE = ""', 'DEVICE = "soil"')
            .replace('SENSOR_ID = ""', 'SENSOR_ID = "soil-bd1234"'),
            encoding="utf-8",
        )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        def _fail_on_reload(cls, root):
            raise AssertionError("NPK persistence should not reload runtime config")

        def _fail_on_dump(cls, path, document):
            raise AssertionError("NPK target update should not dump full TOML")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        monkeypatch.setattr(Settings, "_dump_toml_for_path", classmethod(_fail_on_dump))
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/soil-bd1234/config/set",
            payload_text='{"message_id":"cfg-npk","payload":{"updates":[{"section":"NPK","key":"K_TARGET","value":175.0}]}}',
            settings_root=tmpdir_path,
        )
        soil_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_SOIL_FILE)

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert result.runtime_config.sensor.soil_npk.k_target == 175.0
    assert soil_doc["NPK"]["K_TARGET"] == 175.0


def test_switch_command_persists_switch_toml_last_state():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        class _Handle:
            def __init__(self, value=False):
                self.value = value

        switch_service = type(
            "_SwitchService",
            (),
            {
                "phase": "ready",
                "device_id": runtime_config.switch.device_id,
                "channel_count": 1,
                "errors": (),
                "channels": (
                    type(
                        "_Channel",
                        (),
                        {
                            "key": "SWITCH_1",
                            "channel_id": "S1-w9umh8",
                            "phase": "ready",
                            "control_handle": _Handle(False),
                            "enable_handle": _Handle(True),
                            "errors": (),
                        },
                    )(),
                ),
            },
        )()

        result = process_switch_command_message(
            transport,
            runtime_config,
            switch_service,
            topic="nodus/S1-w9umh8/config/set",
            payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Switch","key":"SWITCH_1_LAST_STATE","value":true,"name":"switch.toml"}]}}',
            settings_root=tmpdir_path,
        )
        switch_doc = Settings._read_toml_file(tmpdir_path / Settings.SWITCH_FILE)

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert switch_doc["Switch"]["SWITCH_1_LAST_STATE"] is True


def test_switch_command_persists_without_reloading_runtime_config(monkeypatch):
    docs_root = Path(__file__).resolve().parent / "fixtures" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)

        class _Handle:
            def __init__(self, value=False):
                self.value = value

        switch_service = type(
            "_SwitchService",
            (),
            {
                "phase": "ready",
                "device_id": runtime_config.switch.device_id,
                "channel_count": 1,
                "errors": (),
                "channels": (
                    type(
                        "_Channel",
                        (),
                        {
                            "key": "SWITCH_1",
                            "channel_id": "S1-w9umh8",
                            "phase": "ready",
                            "control_handle": _Handle(False),
                            "enable_handle": _Handle(True),
                            "errors": (),
                        },
                    )(),
                ),
            },
        )()

        def _fail_on_reload(cls, root):
            raise AssertionError("switch persistence should not reload runtime config")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        result = process_switch_command_message(
            transport,
            runtime_config,
            switch_service,
            topic="nodus/S1-w9umh8/config/set",
            payload_text="ON",
            settings_root=tmpdir_path,
        )
        switch_doc = Settings._read_toml_file(tmpdir_path / Settings.SWITCH_FILE)

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert switch_doc["Switch"]["SWITCH_1_LAST_STATE"] is True


def test_device_config_message_persists_sensor_location_without_runtime_reload(
    monkeypatch,
):
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        original_text = (tmpdir_path / Settings.SENSOR_I2C_FILE).read_text(
            encoding="utf-8"
        )
        transport = MQTTTransport("broker.local", 1883)

        def _fail_on_reload(cls, root):
            raise AssertionError(
                "generic config persistence should not reload runtime config"
            )

        def _fail_on_dump(cls, path, document):
            raise AssertionError("scalar location update should not dump full TOML")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        monkeypatch.setattr(Settings, "_dump_toml_for_path", classmethod(_fail_on_dump))
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/aqi-x943fm/config/set",
            payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Sensor","key":"LOCATION","value":"DeskTest","name":"sensor_i2c.toml"}]}}',
            settings_root=tmpdir_path,
        )
        sensor_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_I2C_FILE)
        backup_text = (
            tmpdir_path / "{}.bak".format(Settings.SENSOR_I2C_FILE)
        ).read_text(encoding="utf-8")

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert result.runtime_config.sensor.location == "DeskTest"
    assert sensor_doc["Sensor"]["LOCATION"] == "DeskTest"
    assert backup_text == original_text


def test_device_config_message_persists_display_metrics_with_backup(monkeypatch):
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        original_text = (tmpdir_path / Settings.SENSOR_I2C_FILE).read_text(
            encoding="utf-8"
        )
        transport = MQTTTransport("broker.local", 1883)

        def _fail_on_reload(cls, root):
            raise AssertionError("display persistence should not reload runtime config")

        monkeypatch.setattr(Settings, "from_directory", classmethod(_fail_on_reload))
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/aqi-x943fm/config/set",
            payload_text=(
                '{"message_id":"cfg-1","payload":{"updates":['
                '{"section":"Display","key":"METRIC_1","value":"Plant VPD"}'
                "]}}"
            ),
            settings_root=tmpdir_path,
        )
        sensor_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_I2C_FILE)
        backup_text = (
            tmpdir_path / "{}.bak".format(Settings.SENSOR_I2C_FILE)
        ).read_text(encoding="utf-8")

    assert result.phase == "published"
    assert result.persistence_mode == "persisted"
    assert result.runtime_config.sensor.display.metrics[0] == "Plant VPD"
    assert result.runtime_config.sensor.display.styles[0] == "Graph24hr"
    assert sensor_doc["Display"]["METRIC_1"] == "Plant VPD"
    assert sensor_doc["Display"]["Style"]["METRIC_1"] == "Graph24hr"
    assert backup_text == original_text
