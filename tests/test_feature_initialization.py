"""Tests for sensor and switch feature initialization decisions.

The cases map runtime configuration to enabled feature services and verify
that absent hardware or configuration is handled explicitly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.config import (
    DetectedSensor,
    I2CConfig,
    RuntimeConfig,
    SoilModbusConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.sensor import plan_sensor_initialization
from cpynodus_ii.features.switch import plan_switch_initialization


def test_sensor_initialization_is_ready_for_i2c_sensor_config():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    sensor_init = plan_sensor_initialization(runtime_config)
    assert sensor_init.enabled is True
    assert sensor_init.ready is True
    assert sensor_init.family == "i2c"
    assert sensor_init.interface == "i2c"
    assert sensor_init.device == "aqi"
    assert sensor_init.sensor_id == "aqi-x943fm"
    assert sensor_init.errors == ()


def test_sensor_initialization_is_ready_for_soil_sensor_config():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-1",
            modbus=SoilModbusConfig(uart_tx="GP4", uart_rx="GP5", baud=4800, address=3),
        )
    )

    sensor_init = plan_sensor_initialization(runtime_config)
    assert sensor_init.enabled is True
    assert sensor_init.ready is True
    assert sensor_init.family == "soil"
    assert sensor_init.interface == "modbus_rs485"
    assert sensor_init.errors == ()


def test_sensor_initialization_reports_missing_i2c_fields():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            i2c=I2CConfig(bus=0, scl_pin="", sda_pin="GP0", address=0),
        )
    )

    sensor_init = plan_sensor_initialization(runtime_config)
    assert sensor_init.enabled is True
    assert sensor_init.ready is False
    assert "missing_i2c_scl_pin" in sensor_init.errors
    assert "missing_i2c_address" in sensor_init.errors


def test_switch_initialization_is_ready_for_parsed_switch_config():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    switch_init = plan_switch_initialization(runtime_config)
    assert switch_init.enabled is True
    assert switch_init.ready is True
    assert switch_init.device_id == "switch-x943fm"
    assert switch_init.channel_count == 2
    assert switch_init.channels[0].key == "SWITCH_1"
    assert switch_init.channels[0].ready is True
    assert switch_init.channels[0].initial_state is True


def test_switch_initialization_reports_channel_errors():
    runtime_config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-1",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="",
                    enable_pin="GP5",
                    control_pin="",
                    last_state=False,
                ),
            ),
        )
    )

    switch_init = plan_switch_initialization(runtime_config)
    assert switch_init.enabled is True
    assert switch_init.ready is False
    assert "SWITCH_1:missing_channel_id" in switch_init.errors
    assert "SWITCH_1:missing_control_pin" in switch_init.errors
