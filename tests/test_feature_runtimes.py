from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.config import DetectedSensor, I2CConfig, RuntimeConfig, SwitchChannelConfig, SwitchConfig
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import (
    build_sensor_runtime,
    build_switch_runtime,
    plan_sensor_initialization,
    plan_switch_initialization,
)


def test_sensor_runtime_builds_ready_i2c_target_from_reference_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    sensor_init = plan_sensor_initialization(runtime_config)
    sensor_runtime = build_sensor_runtime(sensor_init, runtime_config)
    assert sensor_runtime.phase == "ready"
    assert sensor_runtime.transport_target == "i2c:0@0x77"
    assert sensor_runtime.device == "aqi"


def test_sensor_runtime_is_blocked_when_initialization_is_not_ready():
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
    sensor_runtime = build_sensor_runtime(sensor_init, runtime_config)
    assert sensor_runtime.phase == "blocked"
    assert "missing_i2c_scl_pin" in sensor_runtime.errors
    assert sensor_runtime.transport_target == ""


def test_switch_runtime_builds_ready_channels_from_reference_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    switch_init = plan_switch_initialization(runtime_config)
    switch_runtime = build_switch_runtime(switch_init)
    assert switch_runtime.phase == "ready"
    assert switch_runtime.channel_count == 2
    assert switch_runtime.channels[0].phase == "ready"
    assert switch_runtime.channels[0].control_pin == "GP28"
    assert switch_runtime.channels[1].channel_id == "S2-x943fm"


def test_switch_runtime_is_blocked_when_channel_validation_fails():
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
    switch_runtime = build_switch_runtime(switch_init)
    assert switch_runtime.phase == "blocked"
    assert switch_runtime.channels[0].phase == "blocked"
    assert "missing_channel_id" in switch_runtime.channels[0].errors
