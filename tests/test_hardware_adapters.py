"""Tests for binding runtime configuration to hardware adapters."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    I2CConfig,
    RuntimeConfig,
    SoilModbusChannelConfig,
    SoilModbusConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.sensor import plan_sensor_initialization
from cpynodus_ii.features.sensor_runtime import build_sensor_runtime
from cpynodus_ii.features.switch import plan_switch_initialization
from cpynodus_ii.features.switch_runtime import build_switch_runtime
from cpynodus_ii.hardware import bind_sensor_hardware, bind_switch_hardware


class _FakeI2C:
    def __init__(self, scl, sda):
        self.scl = scl
        self.sda = sda


class _FakeUART:
    def __init__(self, tx, rx, *, baudrate, timeout):
        self.tx = tx
        self.rx = rx
        self.baudrate = baudrate
        self.timeout = timeout


class _FakeDigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None


def test_sensor_hardware_adapter_binds_i2c_transport():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    board_module = SimpleNamespace(GP0="pin-gp0", GP1="pin-gp1")
    busio_module = SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART)

    adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=board_module,
        busio_module=busio_module,
    )

    assert adapter.phase == "bound"
    assert adapter.transport_kind == "i2c"
    assert adapter.transport.scl == "pin-gp1"
    assert adapter.transport.sda == "pin-gp0"


def test_sensor_hardware_adapter_binds_modbus_uart_transport():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            modbus=SimpleNamespace(
                uart_tx="GP4", uart_rx="GP5", baud=4800, timeout_s=0.5, address=3
            ),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    board_module = SimpleNamespace(GP4="pin-gp4", GP5="pin-gp5")
    busio_module = SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART)

    adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=board_module,
        busio_module=busio_module,
    )

    assert adapter.phase == "bound"
    assert adapter.transport_kind == "uart"
    assert adapter.transport.tx == "pin-gp4"
    assert adapter.transport.rx == "pin-gp5"
    assert adapter.transport.baudrate == 4800


def test_sensor_hardware_adapter_binds_dual_modbus_uart_transports():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            modbus=SoilModbusConfig(
                channels=(
                    SoilModbusChannelConfig(
                        name="CH1",
                        uart_tx="GP0",
                        uart_rx="GP1",
                        baud=9600,
                        address=1,
                    ),
                    SoilModbusChannelConfig(
                        name="CH2",
                        uart_tx="GP4",
                        uart_rx="GP5",
                        baud=4800,
                        address=3,
                    ),
                )
            ),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )

    adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(
            GP0="pin-gp0",
            GP1="pin-gp1",
            GP4="pin-gp4",
            GP5="pin-gp5",
        ),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )

    assert adapter.phase == "bound"
    assert adapter.transport_kind == "uart_dual"
    assert adapter.transport[0][0].name == "CH1"
    assert adapter.transport[0][1].baudrate == 9600
    assert adapter.transport[1][0].name == "CH2"
    assert adapter.transport[1][1].tx == "pin-gp4"


def test_sensor_hardware_adapter_reports_missing_pin_objects():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=119),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )

    adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP1="pin-gp1"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )

    assert adapter.phase == "error"
    assert "missing_i2c_sda_pin_object" in adapter.errors


def test_switch_hardware_adapter_binds_channel_pins():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    switch_runtime = build_switch_runtime(plan_switch_initialization(runtime_config))
    board_module = SimpleNamespace(
        GP5="pin-gp5", GP28="pin-gp28", GP10="pin-gp10", GP21="pin-gp21"
    )
    digitalio_module = SimpleNamespace(
        DigitalInOut=_FakeDigitalInOut,
        Direction=SimpleNamespace(OUTPUT="output"),
    )

    adapter = bind_switch_hardware(
        switch_runtime,
        board_module=board_module,
        digitalio_module=digitalio_module,
    )

    assert adapter.phase == "bound"
    assert adapter.channels[0].phase == "bound"
    assert adapter.channels[0].enable_handle.pin == "pin-gp5"
    assert adapter.channels[0].control_handle.pin == "pin-gp28"
    assert adapter.channels[0].enable_handle.direction == "output"


def test_switch_hardware_adapter_reports_missing_control_pin_objects():
    runtime_config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-1",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-abc",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=False,
                ),
            ),
        )
    )
    switch_runtime = build_switch_runtime(plan_switch_initialization(runtime_config))
    digitalio_module = SimpleNamespace(
        DigitalInOut=_FakeDigitalInOut,
        Direction=SimpleNamespace(OUTPUT="output"),
    )

    adapter = bind_switch_hardware(
        switch_runtime,
        board_module=SimpleNamespace(GP5="pin-gp5"),
        digitalio_module=digitalio_module,
    )

    assert adapter.phase == "error"
    assert "SWITCH_1:missing_control_pin_object" in adapter.errors
