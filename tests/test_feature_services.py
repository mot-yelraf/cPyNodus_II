"""Tests for sensor and switch service behavior over fake adapters.

The cases use lightweight hardware doubles to validate service lifecycle,
readings, control, and cleanup on the host.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import cpynodus_ii.features.sensor_service as sensor_service_module
from cpynodus_ii.core.config import (
    DetectedSensor,
    I2CConfig,
    RuntimeConfig,
    SensorCalibration,
    SoilModbusChannelConfig,
    SoilModbusConfig,
    SoilNPKConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.sensor import plan_sensor_initialization
from cpynodus_ii.features.sensor_runtime import build_sensor_runtime
from cpynodus_ii.features.sensor_service import (
    read_sensor_snapshot,
    start_sensor_service,
    stop_sensor_service,
)
from cpynodus_ii.features.switch import plan_switch_initialization
from cpynodus_ii.features.switch_runtime import build_switch_runtime
from cpynodus_ii.features.switch_service import (
    apply_switch_state,
    snapshot_switch_states,
    start_switch_service,
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
        self._last_write = b""

    def deinit(self):
        self.deinited = True

    def write(self, payload):
        self._last_write = bytes(payload)

    def read(self, _count):
        if not self._last_write:
            return None
        address = self._last_write[0]
        start = (self._last_write[2] << 8) | self._last_write[3]
        register_values = {
            0: 430,
            1: 215,
            2: 55,
            3: 68,
            4: 11,
            5: 22,
            6: 33,
        }
        value = register_values.get(start)
        if value is None:
            return None
        body = bytes([address, 0x03, 0x02, (value >> 8) & 0xFF, value & 0xFF])
        crc = sensor_service_module._modbus_crc16(body)
        return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class _FakeDigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.value = False
        self.deinited = False

    def deinit(self):
        self.deinited = True


def _modbus_register_response(address, *registers):
    data = []
    for value in registers:
        data.extend([(int(value) >> 8) & 0xFF, int(value) & 0xFF])
    body = bytes([address & 0xFF, 0x03, len(data)]) + bytes(data)
    crc = sensor_service_module._modbus_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def test_modbus_parser_accepts_echoed_response_prefix():
    response = b"\x04\x03\x00\x07\x00\x01\x35\x9d" + _modbus_register_response(
        4, 70
    )

    assert sensor_service_module._parse_modbus_register_response(
        response, address=4, count=1
    ) == (70,)


class _FakeBME680:
    def __init__(self, transport, *, address):
        self.transport = transport
        self.address = address
        self.temperature = 24.5
        self.humidity = 55.25
        self.pressure = 100850.0
        self.gas = 12345.0


class _FakeBME280:
    def __init__(self, transport, *, address):
        self.transport = transport
        self.address = address
        if getattr(transport, "scl", "") == "pin-gp3":
            self.temperature = 22.0
            self.relative_humidity = 61.26
            self.pressure = 100650.0
        else:
            self.temperature = 24.5
            self.relative_humidity = 55.25
            self.pressure = 100850.0


class _FakeAHTx0:
    def __init__(self, transport, *, address=0x38):
        self.transport = transport
        self.address = address
        if getattr(transport, "scl", "") == "pin-gp3":
            self.temperature = 22.0
            self.relative_humidity = 61.26
        else:
            self.temperature = 24.5
            self.relative_humidity = 55.25


class _FakeSCD4X:
    def __init__(self, transport, *, address=0x62):
        self.transport = transport
        self.address = address
        self.altitude = 0
        self.altitude_at_start = None
        self.data_ready = True
        self.CO2 = 845.4
        self.temperature = 23.5
        self.relative_humidity = 47.0
        self.periodic_started = False

    def start_periodic_measurement(self):
        self.altitude_at_start = self.altitude
        self.periodic_started = True


class _FlakySCD4X(_FakeSCD4X):
    attempts = 0

    def __init__(self, transport, *, address=0x62):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise OSError("sensor warming")
        super().__init__(transport, address=address)


class _FakeSCD30:
    def __init__(self, transport):
        self.transport = transport
        self.altitude = 0
        self.data_available = True
        self.co2_reads = 0
        self.temperature = 23.5
        self.relative_humidity = 47.0

    @property
    def CO2(self):
        self.co2_reads += 1
        return 845.4


class _FakeSCD30BothFlags(_FakeSCD30):
    def __init__(self, transport):
        super().__init__(transport)
        self.data_ready = False
        self.data_available = True


class _MissingSensorDriver:
    def __init__(self, *_args, **_kwargs):
        raise OSError("no i2c device")


class _FallbackBME680(_FakeBME680):
    def __init__(self, transport, *, address):
        if getattr(transport, "scl", "") == "pin-gp1":
            raise OSError("no i2c device")
        super().__init__(transport, address=address)


def test_sensor_service_starts_bme680_for_aqi_config():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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


def test_sensor_service_applies_altitude_for_bme680_startup():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            sensor_id="aqi-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x77),
            calibration_device=SensorCalibration(altitude_meters=1500.0),
        )
    )
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
    expected = 1008.5 / ((1.0 - (1500.0 / 44330.0)) ** 5.255)
    assert abs(sensor_service.driver.sea_level_pressure - expected) < 0.001


def test_sensor_service_reports_bme680_baro_pressure_from_altitude():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            sensor_id="aqi-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x77),
            calibration_device=SensorCalibration(altitude_meters=1783.0),
        )
    )
    sensor_service = SimpleNamespace(
        phase="ready",
        driver_kind="adafruit_bme680",
        driver=SimpleNamespace(
            temperature=24.5,
            humidity=55.25,
            pressure=818.2,
            gas=12345.0,
        ),
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Baro-Pressure"] != 818.2
    assert snapshot.metrics["Baro-Pressure"] == 1015.2


def test_sensor_service_reports_missing_i2c_sensor_at_startup():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x61),
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
        modules={"adafruit_scd30": SimpleNamespace(SCD30=_MissingSensorDriver)},
    )

    assert sensor_service.phase == "error"
    assert "sensor_not_found" in sensor_service.errors


def test_sensor_service_tries_other_i2c_bus_when_sensor_missing_on_preferred_bus():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            sensor_id="aqi-x943fm",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x77),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(
            GP0="pin-gp0",
            GP1="pin-gp1",
            GP2="pin-gp2",
            GP3="pin-gp3",
        ),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )

    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={
            "adafruit_bme680": SimpleNamespace(
                Adafruit_BME680_I2C=_FallbackBME680
            )
        },
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_bme680"
    assert sensor_service.transport.scl == "pin-gp3"
    assert sensor_service.transport.sda == "pin-gp2"
    assert sensor_adapter.transport.deinited is True
    assert "i2c_fallback:i2c:1@0x77" in sensor_service.errors
    assert any(
        str(error).startswith("i2c_preferred_not_found:OSError:no_i2c_device")
        for error in sensor_service.errors
    )


def test_sensor_snapshot_reports_empty_metrics_as_error():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x61),
        )
    )
    sensor_service = SimpleNamespace(
        phase="ready",
        driver=SimpleNamespace(CO2=None, temperature=None, relative_humidity=None),
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "error"
    assert snapshot.metrics == {}
    assert "sensor_metrics_empty" in snapshot.errors


def test_sensor_snapshot_classifies_i2c_read_oserror_as_sensor_not_found():
    class _MissingAQIDriver:
        @property
        def temperature(self):
            raise OSError(19, "No such device")

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aqi",
            sensor_id="aqi-1",
            i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x77),
        )
    )
    sensor_service = SimpleNamespace(
        phase="ready",
        driver=_MissingAQIDriver(),
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "error"
    assert snapshot.metrics == {}
    assert snapshot.errors[0] == "sensor_not_found"
    assert snapshot.errors[1].startswith("sensor_not_found:OSError:")


def test_sensor_service_starts_periodic_measurement_for_scd41():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
        )
    )
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
        modules={"adafruit_scd4x": SimpleNamespace(SCD4X=_FakeSCD4X)},
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_scd4x"
    assert sensor_service.driver.periodic_started is True
    assert sensor_service.driver.address == 0x62


def test_sensor_service_applies_altitude_before_scd41_periodic_start():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
            calibration_device=SensorCalibration(altitude_meters=1612.4),
        )
    )
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
        modules={"adafruit_scd4x": SimpleNamespace(SCD4X=_FakeSCD4X)},
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver.altitude == 1612
    assert sensor_service.driver.altitude_at_start == 1612


def test_sensor_service_retries_scd41_startup_once():
    _FlakySCD4X.attempts = 0
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
        )
    )
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
        modules={"adafruit_scd4x": SimpleNamespace(SCD4X=_FlakySCD4X)},
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver.periodic_started is True
    assert _FlakySCD4X.attempts == 2


def test_sensor_service_reports_scd41_startup_exception_type():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
        )
    )
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
        modules={"adafruit_scd4x": SimpleNamespace(SCD4X=_MissingSensorDriver)},
    )

    assert sensor_service.phase == "error"
    assert "sensor_not_found" in sensor_service.errors
    assert "sensor_not_found:OSError:no_i2c_device" in sensor_service.errors


def test_sensor_service_applies_altitude_for_scd30():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x61),
            calibration_device=SensorCalibration(altitude_meters=1499.6),
        )
    )
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
        modules={"adafruit_scd30": SimpleNamespace(SCD30=_FakeSCD30)},
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver.altitude == 1500


def test_scd41_snapshot_waits_until_data_ready():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
        )
    )
    driver = _FakeSCD4X(None)
    driver.data_ready = False
    sensor_service = SimpleNamespace(
        phase="ready",
        driver_kind="adafruit_scd4x",
        driver=driver,
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "waiting"
    assert snapshot.metrics == {}
    assert "sensor_data_not_ready" in snapshot.errors


def test_scd30_snapshot_waits_until_data_available():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x61),
        )
    )
    driver = _FakeSCD30(None)
    driver.data_available = False
    sensor_service = SimpleNamespace(
        phase="ready",
        driver_kind="adafruit_scd30",
        driver=driver,
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "waiting"
    assert snapshot.metrics == {}
    assert driver.co2_reads == 0
    assert "sensor_data_not_ready" in snapshot.errors


def test_scd30_snapshot_prefers_data_available_over_data_ready():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-29j39c",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x61),
        )
    )
    driver = _FakeSCD30BothFlags(None)
    sensor_service = SimpleNamespace(
        phase="ready",
        driver_kind="adafruit_scd30",
        driver=driver,
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["CO2"] == 845.0
    assert driver.co2_reads == 1


def test_sensor_service_reads_legacy_aqi_snapshot():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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
    assert snapshot.metrics["Rel-Humidity"] == 55.2
    assert snapshot.metrics["Humidity"] > 0
    assert snapshot.metrics["Baro-Pressure"] == 1008.5
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
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "soil_modbus_uart"
    assert sensor_service.transport.baudrate == 4800
    assert hasattr(sensor_service.driver, "read_registers")


def test_sensor_service_reads_soil_snapshot_from_wrapped_uart_transport():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-bd1234",
            modbus=SoilModbusConfig(
                uart_tx="GP4",
                uart_rx="GP5",
                baud=4800,
                address=1,
                variant="soil_7in1",
            ),
            soil_registers=SimpleNamespace(
                temperature=1, moisture=0, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
            soil_thresholds=SimpleNamespace(wet_pct=68.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=11.0, p_target=88.0, k_target=33.0),
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
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(GP4="pin-gp4", GP5="pin-gp5"),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Soil Temp_C"] == 21.5
    assert snapshot.metrics["Soil Moisture"] == 43.0
    assert snapshot.metrics["Soil pH"] == 6.8
    assert snapshot.metrics["Soil Nitrogen"] == 11.0
    assert snapshot.metrics["Soil Fertility Index"] == 50.0


def test_sensor_service_keeps_primary_soil_register_map_when_ph_is_valid():
    class _FakeSoilTransport:
        def read_registers(self, start, count):
            values = {
                0: 285,
                1: 430,
                2: 55,
                3: 68,
                4: 11,
                5: 22,
                6: 33,
                7: 70,
                12: 584,
            }
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
            modbus=SoilModbusConfig(variant="soil_7in1"),
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=11.0, p_target=22.0, k_target=33.0),
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
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=_FakeSoilTransport(),
            errors=(),
            interface="modbus_rs485",
        ),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Soil pH"] == 6.8
    assert snapshot.metrics["Soil EC"] == 55.0


def test_sensor_service_uses_observed_soil_ph_and_ec_fallback_registers():
    class _FakeSoilTransport:
        def read_registers(self, start, count):
            values = {
                0: 285,
                1: 0,
                2: 0,
                3: 0,
                4: 0,
                5: 0,
                6: 0,
                7: 70,
                12: 584,
            }
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
            modbus=SoilModbusConfig(variant="soil_7in1"),
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=20.0, p_target=30.0, k_target=70.0),
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
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=_FakeSoilTransport(),
            errors=(),
            interface="modbus_rs485",
        ),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Soil Temp_C"] == 28.5
    assert snapshot.metrics["Soil Moisture"] == 0.0
    assert snapshot.metrics["Soil pH"] == 7.0
    assert snapshot.metrics["Soil EC"] == 584.0
    assert snapshot.metrics["Soil Phosphorus"] == 0.0


def test_sensor_service_prefers_soil_block_reads_for_sparse_zero_registers():
    class _BlockOnlySoilTransport:
        def __init__(self):
            self.calls = []

        def read_registers(self, start, count):
            self.calls.append((start, count))
            if start == 0 and count == 8:
                return (276, 0, 0, 0, 0, 0, 0, 70)
            if start == 12 and count == 1:
                return 585
            return None

        def deinit(self):
            pass

    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-1",
            modbus=SoilModbusConfig(variant="soil_7in1"),
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=12, ph=7, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=20.0, p_target=30.0, k_target=70.0),
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
    soil_transport = _BlockOnlySoilTransport()
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=soil_transport,
            errors=(),
            interface="modbus_rs485",
        ),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Soil Temp_C"] == 27.6
    assert snapshot.metrics["Soil Moisture"] == 0.0
    assert snapshot.metrics["Soil pH"] == 7.0
    assert snapshot.metrics["Soil EC"] == 585.0
    assert snapshot.metrics["Soil Phosphorus"] == 0.0
    assert (0, 8) in soil_transport.calls


def test_sensor_service_reads_dual_soil_snapshots_with_channel_prefixes():
    class _FakeSoilTransport:
        def __init__(self, values):
            self.values = values

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
            modbus=SoilModbusConfig(
                channels=(
                    SoilModbusChannelConfig(
                        name="CH1",
                        uart_tx="GP0",
                        uart_rx="GP1",
                        baud=9600,
                        address=1,
                        variant="soil_7in1",
                    ),
                    SoilModbusChannelConfig(
                        name="CH2",
                        uart_tx="GP4",
                        uart_rx="GP5",
                        baud=4800,
                        address=3,
                        variant="soil_7in1",
                    ),
                )
            ),
            soil_registers=SimpleNamespace(
                temperature=0,
                moisture=1,
                ec=2,
                ph=3,
                n=4,
                p=5,
                k=6,
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0,
                moisture=10.0,
                ec=1.0,
                ph=10.0,
                n=1.0,
                p=1.0,
                k=1.0,
            ),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=12.0, p_target=23.0, k_target=34.0),
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
    channels = runtime_config.sensor.modbus.channels
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=(
                (
                    channels[0],
                    _FakeSoilTransport(
                        {0: 215, 1: 430, 2: 55, 3: 68, 4: 11, 5: 22, 6: 33}
                    ),
                ),
                (
                    channels[1],
                    _FakeSoilTransport(
                        {0: 201, 1: 250, 2: 44, 3: 71, 4: 12, 5: 23, 6: 34}
                    ),
                ),
            ),
            errors=(),
            interface="modbus_rs485",
        ),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["CH1 Soil Moisture"] == 43.0
    assert snapshot.metrics["CH2 Soil Moisture"] == 25.0
    assert snapshot.metrics["CH1 Soil pH"] == 6.8
    assert snapshot.metrics["CH2 Soil pH"] == 7.1
    assert snapshot.metrics["CH1 Soil Moisture Deficit"] == 0.0
    assert snapshot.metrics["CH2 Soil Moisture Deficit"] == 65.0
    assert snapshot.metrics["CH1 Soil Fertility Index"] == 93.0
    assert snapshot.metrics["CH2 Soil Fertility Index"] == 100.0


def test_sensor_service_omits_soil_fertility_index_for_non_7in1_variant():
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
            modbus=SoilModbusConfig(
                uart_tx="GP4",
                uart_rx="GP5",
                baud=4800,
                address=3,
                variant="soil_4in1",
            ),
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
            soil_thresholds=SimpleNamespace(wet_pct=38.0, dry_pct=18.0),
            soil_npk=SoilNPKConfig(n_target=11.0, p_target=22.0, k_target=33.0),
        )
    )
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=_FakeSoilTransport(),
            errors=(),
            interface="modbus_rs485",
        ),
        runtime_config,
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert "Soil Fertility Index" not in snapshot.metrics


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
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
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
        SimpleNamespace(
            phase="bound",
            transport=_FakeSoilTransport(),
            errors=(),
            interface="modbus_rs485",
        ),
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
            self.autolux = 5480.0

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
    assert snapshot.metrics["Auto Light"] == 5480.0
    assert snapshot.metrics["Estimated PPFD"] == 100.0
    assert snapshot.metrics["Visible Light Intensity"] == 8.64


def test_sensor_service_starts_bme280_for_avpd_config():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="avpd",
            sensor_id="avpd-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x76),
        )
    )
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
        modules={
            "adafruit_bme280.basic": SimpleNamespace(Adafruit_BME280_I2C=_FakeBME280)
        },
    )

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_bme280"
    assert sensor_service.driver.address == 0x76


def test_sensor_service_applies_altitude_for_bme280_startup():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="avpd",
            sensor_id="avpd-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x76),
            calibration_device=SensorCalibration(altitude_meters=1500.0),
        )
    )
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
        modules={
            "adafruit_bme280.basic": SimpleNamespace(Adafruit_BME280_I2C=_FakeBME280)
        },
    )

    assert sensor_service.phase == "ready"
    expected = 1008.5 / ((1.0 - (1500.0 / 44330.0)) ** 5.255)
    assert abs(sensor_service.driver.sea_level_pressure - expected) < 0.001


def test_sensor_service_reports_bme280_baro_pressure_from_altitude():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="avpd",
            sensor_id="avpd-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x76),
            calibration_device=SensorCalibration(altitude_meters=1783.0),
        )
    )
    driver = SimpleNamespace(
        temperature=24.5,
        relative_humidity=55.25,
        pressure=818.2,
    )
    sensor_service = SimpleNamespace(
        phase="ready",
        driver_kind="adafruit_bme280",
        driver=driver,
        errors=(),
    )

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert snapshot.phase == "ready"
    assert snapshot.metrics["Rel-Humidity"] == 55.2
    assert snapshot.metrics["Baro-Pressure"] != 818.2
    assert snapshot.metrics["Baro-Pressure"] == 1015.2


def test_sensor_service_reads_aht_snapshot_with_temp_humidity_derivatives():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="aht",
            sensor_id="aht-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x38),
        )
    )
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
        modules={"adafruit_ahtx0": SimpleNamespace(AHTx0=_FakeAHTx0)},
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_ahtx0"
    assert sensor_service.driver.address == 0x38
    assert snapshot.phase == "ready"
    assert snapshot.metrics["Temperature"] == 24.5
    assert snapshot.metrics["Rel-Humidity"] == 55.2
    assert snapshot.metrics["Temperature_F"] == 76.1
    assert snapshot.metrics["Ambient VPD"] > 0
    assert snapshot.metrics["Dew Point"] < snapshot.metrics["Temperature"]
    assert snapshot.metrics["Dew Point Deficit"] > 0
    assert 0 <= snapshot.metrics["DewVPD Risk"] <= 100


def test_sensor_service_reads_dual_bme280_snapshot_for_apvpd():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="apvpd",
            sensor_id="apvpd-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x76),
            secondary_i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x76),
            calibration_device=SensorCalibration(
                apvpd_temp_cal_val=0.5, apvpd_rh_cal_val=-1.0
            ),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(
            GP0="pin-gp0", GP1="pin-gp1", GP2="pin-gp2", GP3="pin-gp3"
        ),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={
            "adafruit_bme280.basic": SimpleNamespace(Adafruit_BME280_I2C=_FakeBME280)
        },
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert sensor_service.phase == "ready"
    assert sensor_adapter.i2c_fallbacks == ()
    assert snapshot.phase == "ready"
    assert snapshot.metrics["Temperature"] == 24.5
    assert snapshot.metrics["Rel-Humidity"] == 55.2
    assert snapshot.metrics["Ambient VPD"] > 0
    assert snapshot.metrics["Plant Temperature"] == 22.5
    assert snapshot.metrics["Plant Rel-Humidity"] == 60.3
    assert snapshot.metrics["Plant Baro-Pressure"] == 1006.5
    assert snapshot.metrics["Plant VPD"] > 0
    assert snapshot.metrics["Plant DewVPD Risk"] >= 0


def test_sensor_service_reads_dual_aht_snapshot_for_apvpd_aht():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="apvpd_aht",
            sensor_id="apvpd-aht-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x38),
            secondary_i2c=I2CConfig(
                bus=1,
                scl_pin="GP3",
                sda_pin="GP2",
                address=0x38,
            ),
            calibration_device=SensorCalibration(
                apvpd_temp_cal_val=0.5,
                apvpd_rh_cal_val=-1.0,
            ),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(
            GP0="pin-gp0",
            GP1="pin-gp1",
            GP2="pin-gp2",
            GP3="pin-gp3",
        ),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={"adafruit_ahtx0": SimpleNamespace(AHTx0=_FakeAHTx0)},
    )
    snapshot = read_sensor_snapshot(sensor_service, runtime_config)

    assert sensor_service.phase == "ready"
    assert sensor_service.driver_kind == "adafruit_ahtx0"
    assert sensor_adapter.i2c_fallbacks == ()
    assert snapshot.phase == "ready"
    assert snapshot.metrics["Temperature"] == 24.5
    assert snapshot.metrics["Rel-Humidity"] == 55.2
    assert snapshot.metrics["Ambient VPD"] > 0
    assert "Baro-Pressure" not in snapshot.metrics
    assert snapshot.metrics["Plant Temperature"] == 22.5
    assert snapshot.metrics["Plant Rel-Humidity"] == 60.3
    assert "Plant Baro-Pressure" not in snapshot.metrics
    assert snapshot.metrics["Plant VPD"] > 0
    assert snapshot.metrics["Plant DewVPD Risk"] >= 0


def test_sensor_service_stop_deinits_dual_i2c_transports_for_apvpd():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="apvpd",
            sensor_id="apvpd-1",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x76),
            secondary_i2c=I2CConfig(bus=1, scl_pin="GP3", sda_pin="GP2", address=0x76),
        )
    )
    sensor_runtime = build_sensor_runtime(
        plan_sensor_initialization(runtime_config),
        runtime_config,
    )
    sensor_adapter = bind_sensor_hardware(
        sensor_runtime,
        runtime_config,
        board_module=SimpleNamespace(
            GP0="pin-gp0", GP1="pin-gp1", GP2="pin-gp2", GP3="pin-gp3"
        ),
        busio_module=SimpleNamespace(I2C=_FakeI2C, UART=_FakeUART),
    )
    sensor_service = start_sensor_service(
        sensor_runtime,
        sensor_adapter,
        runtime_config,
        modules={
            "adafruit_bme280.basic": SimpleNamespace(Adafruit_BME280_I2C=_FakeBME280)
        },
    )

    stop_sensor_service(sensor_service)

    assert sensor_service.transport.deinited is True
    assert sensor_service.secondary_transport.deinited is True


def test_load_module_resolves_dotted_modules_without_fromlist_keywords(monkeypatch):
    root_module = SimpleNamespace(
        basic=SimpleNamespace(Adafruit_BME280_I2C=_FakeBME280)
    )

    monkeypatch.setattr("builtins.__import__", lambda name: root_module)

    module = sensor_service_module._load_module(
        "adafruit_bme280.basic", {}, "missing_adafruit_bme280"
    )

    assert module.Adafruit_BME280_I2C is _FakeBME280


def test_sensor_service_uses_keyword_snapshot_construction_for_co2(monkeypatch):
    class _FakeCO2Driver:
        def __init__(self):
            self.CO2 = 845.4
            self.temperature = 23.5
            self.relative_humidity = 47.26

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

    snapshot = sensor_service_module.read_sensor_snapshot(
        sensor_service, runtime_config
    )

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
    assert snapshot.metrics["Rel-Humidity"] == 47.3


def test_sensor_service_applies_system_and_device_calibration_offsets_for_co2():
    class _FakeCO2Driver:
        def __init__(self):
            self.CO2 = 845.4
            self.temperature = 23.5
            self.relative_humidity = 47.26

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

    snapshot = sensor_service_module.read_sensor_snapshot(
        sensor_service, runtime_config
    )

    assert snapshot.metrics["CO2"] == 445.0
    assert snapshot.metrics["Temperature"] == 24.0
    assert snapshot.metrics["Rel-Humidity"] == 49.3


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
    aqi_service = SimpleNamespace(
        phase="ready", driver=_FakeBME680(None, address=0x77), errors=()
    )
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
            self.autolux = 5480.0

    lux_service = SimpleNamespace(phase="ready", driver=_FakeLuxDriver(), errors=())
    lux_snapshot = sensor_service_module.read_sensor_snapshot(lux_service, lux_runtime)

    assert aqi_snapshot.metrics["Gas"] == 12445.0
    assert aqi_snapshot.metrics["Air Quality"] >= 25.0
    assert lux_snapshot.metrics["Light Intensity"] == 5500.0
    assert lux_snapshot.metrics["Auto Light"] == 5580.0
    assert lux_snapshot.metrics["Estimated PPFD"] == 107.0
    assert lux_snapshot.metrics["Visible Light Intensity"] == 9.24


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
            soil_registers=SimpleNamespace(
                temperature=0, moisture=1, ec=2, ph=3, n=4, p=5, k=6
            ),
            soil_scales=SimpleNamespace(
                temperature=10.0, moisture=10.0, ec=1.0, ph=10.0, n=1.0, p=1.0, k=1.0
            ),
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
                soil_moist_cal_val=-3.0,
                soil_ph_cal_val=0.2,
                soil_ec_cal_val=4.5,
            ),
        )
    )
    sensor_service = start_sensor_service(
        build_sensor_runtime(
            plan_sensor_initialization(runtime_config), runtime_config
        ),
        SimpleNamespace(
            phase="bound",
            transport=_FakeSoilTransport(),
            errors=(),
            interface="modbus_rs485",
        ),
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
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )
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
