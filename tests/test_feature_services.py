from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import cpynodus_ii.features.sensor_service as sensor_service_module
from cpynodus_ii.core.config import (
    DetectedSensor,
    I2CConfig,
    RuntimeConfig,
    SensorCalibration,
    SoilModbusConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import (
    apply_switch_state,
    build_sensor_runtime,
    build_switch_runtime,
    plan_sensor_initialization,
    plan_switch_initialization,
    read_sensor_snapshot,
    snapshot_switch_states,
    start_sensor_service,
    start_switch_service,
    stop_sensor_service,
    stop_switch_service,
)
from cpynodus_ii.hardware import bind_sensor_hardware, bind_switch_hardware


class _FakeI2C:
    def __init__(self, scl, sda):
        self.scl = scl
        self.sda = sda
        self.deinited = False

    def deinit(self):
        self.deinited = True


class _FakeUART:
    def __init__(self, tx, rx, *, baudrate, timeout):
        self.tx = tx
        self.rx = rx
        self.baudrate = baudrate
        self.timeout = timeout
        self.deinited = False

    def deinit(self):
        self.deinited = True


class _FakeDigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.value = False
        self.deinited = False

    def deinit(self):
        self.deinited = True


class _FakeBME680:
    def __init__(self, transport, *, address):
        self.transport = transport
        self.address = address
        self.temperature = 24.5
        self.humidity = 55.25
        self.pressure = 100850.0
        self.gas = 12345.0


def test_sensor_service_starts_bme680_for_aqi_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP0="pin-gp0", GP1="pin-gp1"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={"adafruit_bme680": SimpleNamespace(Adafruit_BME680_I2C=_FakeBME680)},
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_bme680"
    assert sensor_service.driver.address == 119


def test_sensor_service_reads_legacy_aqi_snapshot():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP0="pin-gp0", GP1="pin-gp1"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={"adafruit_bme680": SimpleNamespace(Adafruit_BME680_I2C=_FakeBME680)},
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.sensor_id == "aqi-x943fm"
    assert snapshot.metrics["Temperature"] == 24.5
    assert snapshot.metrics["Temperature_F"] == 76.1
    assert snapshot.metrics["Rel-Humidity"] == 55.0
    assert snapshot.metrics["Humidity"] > 0
    assert snapshot.metrics["Baro-Pressure"] == 1008.0
    assert snapshot.metrics["Gas"] == 12345.0
    assert snapshot.metrics["Air Quality"] >= 0
    assert snapshot.metrics["Ambient VPD"] > 0
    assert snapshot.metrics["Dew Point"] < snapshot.metrics["Temperature"]
    assert snapshot.metrics["Dew Point_F"] < snapshot.metrics["Temperature_F"]
    assert snapshot.metrics["Dew Point Deficit"] > 0
    assert 0 <= snapshot.metrics["DewVPD Risk"] <= 100


def test_sensor_service_uses_uart_transport_for_soil_sensor():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            modbus=SoilModbusConfig(uart_tx="GP4", uart_rx="GP5", baud=4800, address=3),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP4="pin-gp4", GP5="pin-gp5"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(sensor_runtime, sensor_adapter, runtime_config)

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "soil_modbus_uart"
    assert sensor_service.transport.baudrate == 4800


def test_sensor_service_reads_legacy_soil_snapshot():
    class _FakeSoilTransport:
        def __init__(self):
            self.values = {0: 215, 1: 430, 2: 55, 3: 68, 4: 11, 5: 22, 6: 33}

        def read_registers(self, start, count):
            return self.values.get(start)

        def deinit(self):
            pass

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-1",
            modbus=SoilModbusConfig(uart_tx="GP4", uart_rx="GP5", baud=4800, address=3),
            soil_registers=SimpleNamespace(temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6),
            soil_scales=SimpleNamespace(temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_stress=SimpleNamespace(
                temp_low_crit_c=15.0,
                temp_low_ok_c=18.0,
                temp_high_ok_c=24.0,
                temp_high_crit_c=30.0,
                moisture_weight_pct=70.0,
                temp_weight_pct=30.0,
            ),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        SimpleNamespace(phase="bound", transport=_FakeSoilTransport(), errors=(), interface="modbus_rs485"),
        runtime_config,
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Soil Temp_C"] == 21.5
    assert snapshot.metrics["Soil Temp_F"] == 70.7
    assert snapshot.metrics["Soil Moisture"] == 43.0
    assert snapshot.metrics["Soil pH"] == 6.8
    assert snapshot.metrics["Soil Moisture Deficit"] == 0.0
    assert snapshot.metrics["Soil Stress Index"] == 0.0


def test_sensor_service_reads_legacy_lux_snapshot_with_ppfd():
    class _FakeLuxDriver:
        def __init__(self, transport, *, address):
            self.transport = transport
            self.address = address
            self.lux = 5400.0

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="lux",
            sensor_id="lux-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x10),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP2="pin-gp2", GP3="pin-gp3"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={"adafruit_veml7700": SimpleNamespace(VEML7700=_FakeLuxDriver)},
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Light Intensity"] == 5400.0
    assert snapshot.metrics["Estimated PPFD"] == 100.0


def test_sensor_service_uses_keyword_snapshot_construction_for_co2(monkeypatch):
    class _FakeCO2Driver:
        def __init__(self):
            self.CO2 = 845.4
            self.temperature = 23.5
            self.relative_humidity = 47.0

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x62),
        )
    )
    sensor_service = SimpleNamespace(phase="ready", driver=_FakeCO2Driver(), errors=())
    calls = []

    def _keyword_only_snapshot(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(sensor_service_module, "SensorSnapshot", _keyword_only_snapshot)

    snapshot = sensor_service_module.read_sensor_snapshot(sensor_service, runtime_config)

    assert calls == [
        {
            "phase": "ready",
            "sensor_id": "co2-1",
            "device": "co2",
            "metrics": snapshot.metrics,
            "errors": (),
        }
    ]
    assert snapshot.metrics["CO2"] == 845.0


def test_sensor_service_applies_system_and_device_calibration_offsets_for_co2():
    class _FakeCO2Driver:
        def __init__(self):
            self.CO2 = 845.4
            self.temperature = 23.5
            self.relative_humidity = 47.0

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x62),
            calibration_system=SensorCalibration(co2_offset=-400.0, temp_offset=0.5),
            calibration_device=SensorCalibration(rh_offset=2.0),
        )
    )
    sensor_service = SimpleNamespace(phase="ready", driver=_FakeCO2Driver(), errors=())

    snapshot = sensor_service_module.read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.metrics["CO2"] == 445.0
    assert snapshot.metrics["Temperature"] == 24.0
    assert snapshot.metrics["Rel-Humidity"] == 49.0


def test_sensor_service_applies_device_calibration_offsets_for_aqi_and_lux():
    aqi_runtime = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            sensor_id="aqi-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x77),
            calibration_device=SensorCalibration(gas_offset=100.0, aqi_offset=25.0),
        )
    )
    aqi_service = SimpleNamespace(phase="ready", driver=_FakeBME680(None, address=0x77), errors=())
    aqi_snapshot = sensor_service_module.read_sensor_snapshot(aqi_service, aqi_runtime)

    lux_runtime = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="lux",
            sensor_id="lux-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x10),
            calibration_device=SensorCalibration(lux_offset=100.0, ppfd_offset=5.0),
        )
    )

    class _FakeLuxDriver:
        def __init__(self):
            self.lux = 5400.0

    lux_service = SimpleNamespace(phase="ready", driver=_FakeLuxDriver(), errors=())
    lux_snapshot = sensor_service_module.read_sensor_snapshot(lux_service, lux_runtime)

    assert aqi_snapshot.metrics["Gas"] == 12445.0
    assert aqi_snapshot.metrics["Air Quality"] >= 25.0
    assert lux_snapshot.metrics["Light Intensity"] == 5500.0
    assert lux_snapshot.metrics["Estimated PPFD"] == 107.0


def test_sensor_service_applies_soil_calibration_offsets():
    class _FakeSoilTransport:
        def read_registers(self, start, count):
            values = {0: 215, 1: 430, 2: 55, 3: 68, 4: 11, 5: 22, 6: 33}
            return values.get(start)

        def deinit(self):
            pass

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-1",
            modbus=SoilModbusConfig(uart_tx="GP4", uart_rx="GP5", baud=4800, address=3),
            soil_registers=SimpleNamespace(temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6),
            soil_scales=SimpleNamespace(temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_stress=SimpleNamespace(
                temp_low_crit_c=15.0,
                temp_low_ok_c=18.0,
                temp_high_ok_c=24.0,
                temp_high_crit_c=30.0,
                moisture_weight_pct=70.0,
                temp_weight_pct=30.0,
            ),
            calibration_device=SensorCalibration(
                soil_temp_cal_val=1.25,
                soil_temp_moist_val=-3.0,
                soil_ph_cal_val=0.2,
                soil_ec_cal_val=4.5,
            ),
        )
    )
    sensor_service = start_sensor_service(
        build_sensor_runtime(plan_sensor_initialization(runtime_config), runtime_config),
        SimpleNamespace(phase="bound", transport=_FakeSoilTransport(), errors=(), interface="modbus_rs485"),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.metrics["Soil Temp_C"] == 22.75
    assert snapshot.metrics["Soil Moisture"] == 40.0
    assert snapshot.metrics["Soil pH"] == 7.0
    assert snapshot.metrics["Soil EC"] == 59.5


def test_sensor_service_stop_deinits_transport():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            modbus=SoilModbusConfig(uart_tx="GP4", uart_rx="GP5", baud=4800, address=3),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP4="pin-gp4", GP5="pin-gp5"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(sensor_runtime, sensor_adapter, runtime_config)
    stop_sensor_service(sensor_service)

    assert sensor_service.transport.deinited is True


def test_switch_service_applies_initial_state_and_stop_deinits_handles():
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
                    last_state=True,
                ),
            ),
        )
    )
    switch_runtime = build_switch_runtime(plan_switch_initialization(runtime_config))
    switch_adapter = bind_switch_hardware(
        switch_runtime,
        board_module=SimpleNamespace(GP5="pin-gp5", GP28="pin-gp28"),
        digitalio_module=SimpleNamespace(
            DigitalInOut=_FakeDigitalInOut,
            Direction=SimpleNamespace(OUTPUT="output"),
        ),
    )
    switch_service = start_switch_service(switch_runtime, switch_adapter)

    assert switch_service.phase == "ready"
    assert switch_service.channels[0].enable_handle.value is True
    assert switch_service.channels[0].control_handle.value is True

    stop_switch_service(switch_service)
    assert switch_service.channels[0].enable_handle.deinited is True
    assert switch_service.channels[0].control_handle.deinited is True


def test_switch_service_apply_updates_channel_state_snapshot():
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
    switch_adapter = bind_switch_hardware(
        switch_runtime,
        board_module=SimpleNamespace(GP5="pin-gp5", GP28="pin-gp28"),
        digitalio_module=SimpleNamespace(
            DigitalInOut=_FakeDigitalInOut,
            Direction=SimpleNamespace(OUTPUT="output"),
        ),
    )
    switch_service = start_switch_service(switch_runtime, switch_adapter)
    result = apply_switch_state(switch_service, channel_key="SWITCH_1", state=True)
    snapshot = snapshot_switch_states(switch_service)

    assert result.phase == "ready"
    assert result.applied_state is True
    assert snapshot["SWITCH_1"]["state"] is True


def test_switch_service_apply_reports_missing_channel():
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
    switch_adapter = bind_switch_hardware(
        switch_runtime,
        board_module=SimpleNamespace(GP5="pin-gp5", GP28="pin-gp28"),
        digitalio_module=SimpleNamespace(
            DigitalInOut=_FakeDigitalInOut,
            Direction=SimpleNamespace(OUTPUT="output"),
        ),
    )
    switch_service = start_switch_service(switch_runtime, switch_adapter)
    result = apply_switch_state(switch_service, channel_key="SWITCH_2", state=True)

    assert result.phase == "error"
    assert "switch_channel_not_found" in result.errors


def test_switch_service_preserves_adapter_errors():
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
    switch_adapter = bind_switch_hardware(
        switch_runtime,
        board_module=SimpleNamespace(GP5="pin-gp5"),
        digitalio_module=SimpleNamespace(
            DigitalInOut=_FakeDigitalInOut,
            Direction=SimpleNamespace(OUTPUT="output"),
        ),
    )
    switch_service = start_switch_service(switch_runtime, switch_adapter)

    assert switch_service.phase == "error"
    assert "SWITCH_1:missing_control_pin_object" in switch_service.errors
