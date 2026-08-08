"""Regression coverage for shallow MQTT mutation dispatch and persistence.

These tests guard the one-key pacing contract and ensure constrained-memory
failures remain correlated with MQTT acknowledgements and results.
"""

from types import SimpleNamespace

import cpynodus_ii.features.command_intake as command_intake
from cpynodus_ii.core.config import (
    DetectedSensor,
    NetworkConfig,
    RuntimeConfig,
    SensorCalibration,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import scalar_persistence


def _config():
    return RuntimeConfig(
        network=NetworkConfig(hostname="co2-test"),
        sensor=DetectedSensor(
            family="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-test",
            calibration_system=SensorCalibration(),
            calibration_device=SensorCalibration(),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-test",
            channel_count=1,
            channels=(
                SwitchChannelConfig(key="SWITCH_1", channel_id="S1-test", label="Fan"),
            ),
        ),
    )


def _service():
    handle = SimpleNamespace(value=False)
    channel = SimpleNamespace(
        key="SWITCH_1",
        channel_id="S1-test",
        phase="ready",
        control_handle=handle,
        enable_handle=SimpleNamespace(value=True),
        errors=(),
    )
    return SimpleNamespace(
        phase="ready", device_id="switch-test", channels=(channel,), errors=()
    )


def test_network_credentials_use_scalar_writer_and_never_heavy_handler(
    monkeypatch, tmp_path
):
    (tmp_path / "settings.toml").write_text(
        '[Network]\nSSID = "old"\nPASSWORD = "old"\nHOSTNAME = "co2-test"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        command_intake,
        "_handlers",
        lambda: (_ for _ in ()).throw(AssertionError("heavy handler imported")),
    )
    config = _config()
    for message_id, key, value in (
        ("wifi-1", "SSID", "new-network"),
        ("wifi-2", "PASSWORD", "new-password"),
    ):
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/co2-test/config/set",
            '{{"message_id":"{}","payload":{{"updates":[{{"section":"Network",'
            '"key":"{}","value":"{}"}}]}}}}'.format(message_id, key, value),
        )
        results = command_intake.process_inbound_messages(
            transport, config, _service(), settings_root=tmp_path
        )
        assert results[0].phase == "published"
        assert transport.published_messages[1].payload["applied"] is True
        if key == "PASSWORD":
            patch_value = transport.published_messages[2].payload["updates"][0]["value"]
            assert patch_value.startswith("obf1:")
            assert value not in patch_value
    text = (tmp_path / "settings.toml").read_text(encoding="utf-8")
    assert 'SSID = "new-network"' in text
    assert 'PASSWORD = "obf1:' in text
    assert "new-password" not in text


def test_restart_required_pystack_is_failed_not_volatile_success(monkeypatch, tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[Network]\nSSID = "old"\n', encoding="utf-8"
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/co2-test/config/set",
        '{"message_id":"wifi-stack","payload":{"updates":['
        '{"section":"Network","key":"SSID","value":"new"}]}}',
    )
    result = command_intake.process_inbound_messages(
        transport, _config(), _service(), settings_root=tmp_path
    )[0]
    assert result.phase == "error"
    assert result.errors == ("config_persist_pystack",)
    assert result.persistence_mode == "failed"
    assert transport.published_messages[1].payload["applied"] is False


def test_live_config_pystack_remains_applied_and_volatile(monkeypatch, tmp_path):
    (tmp_path / "sensor_i2c.toml").write_text(
        '[Sensor]\nLOCATION = "old"\n', encoding="utf-8"
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/co2-test/config/set",
        '{"message_id":"loc-stack","payload":{"updates":['
        '{"section":"Sensor","key":"LOCATION","value":"new"}]}}',
    )
    result = command_intake.process_inbound_messages(
        transport, _config(), _service(), settings_root=tmp_path
    )[0]
    assert result.phase == "published"
    assert result.persistence_mode == "volatile"
    assert result.runtime_config.sensor.location == "new"
    assert transport.published_messages[1].payload["applied"] is True


def test_multi_key_device_config_is_rejected_after_ack():
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/co2-test/config/set",
        '{"message_id":"multi","payload":{"updates":['
        '{"section":"Network","key":"SSID","value":"one"},'
        '{"section":"Network","key":"PASSWORD","value":"two"}]}}',
    )
    result = command_intake.process_inbound_messages(transport, _config(), _service())[
        0
    ]
    assert result.errors == ("single_update_required",)
    assert transport.published_messages[1].payload["applied"] is False


def test_calibration_apply_and_status_never_use_heavy_handler(monkeypatch, tmp_path):
    (tmp_path / "sensor_i2c.toml").write_text(
        "[Calibration.Device]\nTEMP_OFFSET = 0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        command_intake,
        "_handlers",
        lambda: (_ for _ in ()).throw(AssertionError("heavy handler imported")),
    )
    config = _config()
    for payload in (
        '{"message_id":"cal-1","action":"apply","payload":{"offsets":['
        '{"key":"Calibration.Device.TEMP_OFFSET","value":1.5}]}}',
        '{"message_id":"cal-2","action":"status"}',
    ):
        transport = MQTTTransport("broker.local", 1883)
        transport.receive("nodus/co2-test/calibration/set", payload)
        result = command_intake.process_inbound_messages(
            transport, config, _service(), settings_root=tmp_path
        )[0]
        assert result.phase == "published"


def test_switch_pystack_is_correlated_failure(monkeypatch):
    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(command_intake, "_apply_switch_state", raise_pystack)
    transport = MQTTTransport("broker.local", 1883)
    result = command_intake.process_switch_command_message(
        transport,
        _config(),
        _service(),
        topic="nodus/S1-test/config/set",
        payload_text='{"message_id":"switch-stack","payload":{"state":"ON"}}',
    )
    assert result.errors == ("switch_command_pystack",)
    assert transport.published_messages[1].payload["applied"] is False
