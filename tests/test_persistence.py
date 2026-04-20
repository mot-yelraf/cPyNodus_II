from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.settings import Settings
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import process_calibration_message, process_device_config_message


def test_device_config_message_persists_settings_toml_and_reloads_runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        result = process_device_config_message(
            transport,
            runtime_config,
            topic="nodus/switch-w9umh8/config/set",
            payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"switch-new"},{"section":"MQTT","key":"BROKER","value":"broker2.local"}]}}',
            settings_root=tmpdir_path,
        )
        reloaded = Settings.from_directory(tmpdir_path).runtime_config()

    assert result.phase == "published"
    assert result.runtime_config.network.hostname == "switch-new"
    assert result.runtime_config.mqtt.broker == "broker2.local"
    assert reloaded.network.hostname == "switch-new"
    assert reloaded.mqtt.broker == "broker2.local"
    assert "nodus/switch-w9umh8/meta/patch" in [message.topic for message in transport.published_messages]


def test_calibration_message_persists_active_sensor_toml_and_reloads_runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        result = process_calibration_message(
            transport,
            runtime_config,
            topic="nodus/aqi-x943fm/calibration/set",
            payload_text='{"message_id":"cal-1","action":"apply","payload":{"offsets":[{"key":"Calibration.Device.TEMP_OFFSET","value":1.5}]}}',
            settings_root=tmpdir_path,
        )
        sensor_doc = Settings._read_toml_file(tmpdir_path / Settings.SENSOR_I2C_FILE)

    assert result.phase == "published"
    assert sensor_doc["Calibration"]["Device"]["TEMP_OFFSET"] == 1.5
    assert transport.published_messages[-1].payload["updates"][0]["value"] == 1.5
