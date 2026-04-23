from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.settings import Settings


class _ProbePin:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.pull = None
        self.value = pin not in ("GP5", "GP10")

    def deinit(self):
        return


class _HeldLowPin:
    def __init__(self, _pin):
        self.direction = None
        self.pull = None
        self.value = False

    def deinit(self):
        return


class _ScanI2C:
    def __init__(self, scl, sda):
        self._pins = (scl, sda)

    def try_lock(self):
        return True

    def scan(self):
        if self._pins == ("GP1", "GP0"):
            return (0x76,)
        if self._pins == ("GP3", "GP2"):
            return (0x76,)
        return ()

    def unlock(self):
        return

    def deinit(self):
        return


def _copy_defs(tmpdir_path):
    root = Path(__file__).resolve().parents[1]
    for name in (
        "settings.toml.def",
        "sensor_i2c.toml.def",
        "sensor_soil.toml.def",
        "switch.toml.def",
    ):
        (tmpdir_path / name).write_text((root / name).read_text(), encoding="utf-8")


def test_make_serial_number_returns_six_lowercase_alnum_chars():
    serial = Settings.make_serial_number()

    assert len(serial) == 6
    assert serial.isalnum()
    assert serial == serial.lower()


def test_factory_bootstrap_creates_i2c_sensor_and_switch_tomls_with_seeded_ids():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)

        board_module = SimpleNamespace(GP5="GP5", GP10="GP10")
        digitalio_module = SimpleNamespace(
            DigitalInOut=_ProbePin,
            Direction=SimpleNamespace(INPUT="input"),
            Pull=SimpleNamespace(UP="up"),
        )

        runtime_config = Settings.bootstrap_factory_defaults(
            tmpdir_path,
            detect_fn=lambda: (
                "co2",
                {"i2c": {"bus": 1, "scl": "GP3", "sda": "GP2", "addr": 0x61}},
            ),
            board_module=board_module,
            digitalio_module=digitalio_module,
        )

        settings_doc = Settings._read_toml_file(tmpdir_path / "settings.toml")
        sensor_doc = Settings._read_toml_file(tmpdir_path / "sensor_i2c.toml")
        switch_doc = Settings._read_toml_file(tmpdir_path / "switch.toml")

    sensor_section = sensor_doc["Sensor"]
    switch_section = switch_doc["Switch"]
    serial = sensor_section["SERIAL_NUM"]

    assert runtime_config.sensor.device == "co2"
    assert runtime_config.sensor.sensor_id == "co2-{}".format(serial)
    assert len(serial) == 6
    assert sensor_section["SENSOR_ID"] == "co2-{}".format(serial)
    assert sensor_doc["I2Cbus"]["I2C_BUS"] == 1
    assert sensor_doc["I2Cbus"]["I2C_SCL"] == "GP3"
    assert sensor_doc["I2Cbus"]["I2C_SDA"] == "GP2"
    assert sensor_doc["I2Cbus"]["I2C_ADDR"] == 0x61
    assert switch_section["DEVICE_SERIAL_NUM"] == serial
    assert switch_section["SWITCH_DEVICE_ID"] == "switch-{}".format(serial)
    assert switch_section["SWITCH_1_CHANNEL_ID"] == "S1-{}".format(serial)
    assert switch_section["SWITCH_2_CHANNEL_ID"] == "S2-{}".format(serial)
    assert settings_doc["Network"]["HOSTNAME"] == "co2-{}".format(serial)


def test_factory_bootstrap_creates_soil_toml_without_i2c_toml():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)

        Settings.bootstrap_factory_defaults(
            tmpdir_path,
            detect_fn=lambda: (
                "soil",
                {
                    "uart": {"tx": "GP4", "rx": "GP5"},
                    "modbus": {"baud": 4800, "timeout_s": 0.30, "addr": 3, "soil_variant": "soil_7in1"},
                },
            ),
            board_module=SimpleNamespace(),
            digitalio_module=SimpleNamespace(
                DigitalInOut=_ProbePin,
                Direction=SimpleNamespace(INPUT="input"),
                Pull=SimpleNamespace(UP="up"),
            ),
        )

        settings_doc = Settings._read_toml_file(tmpdir_path / "settings.toml")
        soil_doc = Settings._read_toml_file(tmpdir_path / "sensor_soil.toml")
        has_i2c = (tmpdir_path / "sensor_i2c.toml").exists()

    assert has_i2c is False
    assert soil_doc["Sensor"]["DEVICE"] == "soil"
    assert soil_doc["Modbus"]["UART_TX"] == "GP4"
    assert soil_doc["Modbus"]["UART_RX"] == "GP5"
    assert soil_doc["Modbus"]["MODBUS_BAUD"] == 4800
    assert soil_doc["Modbus"]["MODBUS_ADDR"] == 3
    assert soil_doc["Modbus"]["SOIL_VARIANT"] == "soil_7in1"
    assert settings_doc["Network"]["HOSTNAME"] == soil_doc["Sensor"]["SENSOR_ID"]


def test_factory_profile_reset_forces_nodusweb_when_gp17_held_low():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        (tmpdir_path / "settings.toml").write_text(
            "[Profile]\nACTIVE_PROFILE = \"sensorius\"\n[Network]\nHOSTNAME = \"co2-abc123\"\n",
            encoding="utf-8",
        )
        monotonic_values = iter((0.0, 1.0, 2.0, 3.0, 4.0, 5.1))

        result = Settings.apply_factory_profile_reset_if_requested(
            tmpdir_path,
            board_module=SimpleNamespace(GP17="GP17"),
            digitalio_module=SimpleNamespace(
                DigitalInOut=_HeldLowPin,
                Direction=SimpleNamespace(INPUT="input"),
                Pull=SimpleNamespace(UP="up"),
            ),
            time_module=SimpleNamespace(monotonic=lambda: next(monotonic_values), sleep=lambda _s: None),
        )
        document = Settings._read_toml_file(tmpdir_path / "settings.toml")

    assert result is True
    assert document["Profile"]["ACTIVE_PROFILE"] == "nodusweb"


def test_factory_sensor_detect_finds_apvpd_when_bme280_on_both_buses():
    detected_device, interfaces = Settings._detect_factory_sensor(
        board_module=SimpleNamespace(GP0="GP0", GP1="GP1", GP2="GP2", GP3="GP3"),
        busio_module=SimpleNamespace(I2C=_ScanI2C),
    )

    assert detected_device == "apvpd"
    assert interfaces["i2c"]["bus"] == 0
