"""Tests for factory bootstrap writes and template-ordered TOML output."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.obfuscation import encode_password
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


class _ScanAHTI2C:
    def __init__(self, scl, sda):
        self._pins = (scl, sda)

    def try_lock(self):
        return True

    def scan(self):
        if self._pins in {("GP1", "GP0"), ("GP3", "GP2")}:
            return (0x38,)
        return ()

    def unlock(self):
        return

    def deinit(self):
        return


class _ScanSingleAHTI2C(_ScanAHTI2C):
    def scan(self):
        if self._pins == ("GP1", "GP0"):
            return (0x38,)
        return ()


class _ScanXiaoAHTI2C:
    def __init__(self, scl, sda):
        self._pins = (scl, sda)

    def try_lock(self):
        return True

    def scan(self):
        if self._pins == ("SCL", "SDA"):
            return (0x38,)
        return ()

    def unlock(self):
        return

    def deinit(self):
        return


class _NoSensorI2C:
    def __init__(self, _scl, _sda):
        return

    def try_lock(self):
        return True

    def scan(self):
        return ()

    def unlock(self):
        return

    def deinit(self):
        return


class _ProbeSoilUART:
    def __init__(self, tx, rx, *, baudrate, timeout):
        self.tx = tx
        self.rx = rx
        self.baudrate = baudrate
        self.timeout = timeout
        self._last_write = b""

    def write(self, payload):
        self._last_write = bytes(payload)

    def read(self, _count):
        if self.baudrate != 9600 or self.tx not in {"GP0", "GP4"}:
            return None
        address = self._last_write[0]
        count = (self._last_write[4] << 8) | self._last_write[5]
        body = bytes([address, 0x03, count * 2]) + (b"\x00\x01" * count)
        crc = Settings._modbus_crc16(body)
        return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

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


def _display_metrics(sensor_doc):
    return tuple(
        sensor_doc["Display"].get("METRIC_{}".format(index), "")
        for index in range(1, 7)
    )


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
    assert _display_metrics(sensor_doc) == (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Temperature",
        "Dew Point",
        "CO2",
    )
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
                    "modbus": {
                        "baud": 4800,
                        "timeout_s": 0.30,
                        "addr": 3,
                        "soil_variant": "soil_7in1",
                    },
                    "modbus_channels": (
                        {
                            "name": "CH2",
                            "tx": "GP4",
                            "rx": "GP5",
                            "baud": 4800,
                            "timeout_s": 0.30,
                            "addr": 3,
                            "soil_variant": "soil_7in1",
                        },
                    ),
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
    assert soil_doc["Modbus"]["CH1"]["UART_TX"] == ""
    assert soil_doc["Modbus"]["CH2"]["UART_TX"] == "GP4"
    assert soil_doc["Modbus"]["CH2"]["UART_RX"] == "GP5"
    assert soil_doc["Modbus"]["CH2"]["MODBUS_ADDR"] == 3
    assert settings_doc["Network"]["HOSTNAME"] == soil_doc["Sensor"]["SENSOR_ID"]


def test_factory_profile_reset_forces_nodusweb_when_gp17_held_low():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        (tmpdir_path / "settings.toml").write_text(
            (
                '[Profile]\nACTIVE_PROFILE = "sensorius"\n'
                '[Network]\nHOSTNAME = "co2-abc123"\n'
            ),
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
            time_module=SimpleNamespace(
                monotonic=lambda: next(monotonic_values), sleep=lambda _s: None
            ),
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
    assert interfaces["i2c0"]["bus"] == 0
    assert interfaces["i2c1"]["bus"] == 1


def test_factory_sensor_detect_finds_apvpd_aht_when_aht_on_both_buses():
    detected_device, interfaces = Settings._detect_factory_sensor(
        board_module=SimpleNamespace(GP0="GP0", GP1="GP1", GP2="GP2", GP3="GP3"),
        busio_module=SimpleNamespace(I2C=_ScanAHTI2C),
    )

    assert detected_device == "apvpd_aht"
    assert interfaces["i2c0"]["addr"] == 0x38
    assert interfaces["i2c1"]["addr"] == 0x38


def test_factory_sensor_detect_finds_aht_when_aht_on_one_bus():
    detected_device, interfaces = Settings._detect_factory_sensor(
        board_module=SimpleNamespace(GP0="GP0", GP1="GP1", GP2="GP2", GP3="GP3"),
        busio_module=SimpleNamespace(I2C=_ScanSingleAHTI2C),
    )

    assert detected_device == "aht"
    assert interfaces["i2c"]["addr"] == 0x38


def test_factory_sensor_detect_uses_xiao_i2c_defaults():
    detected_device, interfaces = Settings._detect_factory_sensor(
        board_module=SimpleNamespace(
            board_id="seeed_xiao_esp32_s3_sense",
            SDA="SDA",
            SCL="SCL",
            D0="D0",
        ),
        busio_module=SimpleNamespace(I2C=_ScanXiaoAHTI2C),
    )

    assert detected_device == "aht"
    assert interfaces["i2c"] == {
        "bus": 0,
        "scl": "SCL",
        "sda": "SDA",
        "addr": 0x38,
    }


def test_factory_profile_reset_is_disabled_when_board_has_no_reset_pin():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        (tmpdir_path / "settings.toml").write_text(
            (
                '[Profile]\nACTIVE_PROFILE = "sensorius"\n'
                '[Network]\nHOSTNAME = "co2-abc123"\n'
            ),
            encoding="utf-8",
        )

        result = Settings.apply_factory_profile_reset_if_requested(
            tmpdir_path,
            board_module=SimpleNamespace(
                board_id="seeed_xiao_esp32_s3_sense",
                SDA="SDA",
                SCL="SCL",
                D0="D0",
            ),
            digitalio_module=SimpleNamespace(
                DigitalInOut=_HeldLowPin,
                Direction=SimpleNamespace(INPUT="input"),
                Pull=SimpleNamespace(UP="up"),
            ),
        )
        document = Settings._read_toml_file(tmpdir_path / "settings.toml")

    assert result is False
    assert document["Profile"]["ACTIVE_PROFILE"] == "sensorius"


def test_factory_switch_detect_uses_xiao_switch_defaults():
    class _XiaoProbePin:
        def __init__(self, pin):
            self.pin = pin
            self.direction = None
            self.pull = None
            self.value = pin not in ("D0", "D2")

        def deinit(self):
            return

    active = Settings._detect_factory_switch_channels(
        board_module=SimpleNamespace(
            board_id="seeed_xiao_esp32_s3_sense",
            SDA="SDA",
            SCL="SCL",
            D0="D0",
            D1="D1",
            D2="D2",
            D3="D3",
        ),
        digitalio_module=SimpleNamespace(
            DigitalInOut=_XiaoProbePin,
            Direction=SimpleNamespace(INPUT="input"),
            Pull=SimpleNamespace(UP="up"),
        ),
    )

    assert active[1]["enable"] == "D0"
    assert active[1]["control"] == "D1"
    assert active[2]["enable"] == "D2"
    assert active[2]["control"] == "D3"


def test_factory_sensor_detect_finds_soil_on_both_rs485_channels():
    detected_device, interfaces = Settings._detect_factory_sensor(
        board_module=SimpleNamespace(
            GP0="GP0",
            GP1="GP1",
            GP2="GP2",
            GP3="GP3",
            GP4="GP4",
            GP5="GP5",
        ),
        busio_module=SimpleNamespace(I2C=_NoSensorI2C, UART=_ProbeSoilUART),
    )

    assert detected_device == "soil"
    assert [channel["name"] for channel in interfaces["modbus_channels"]] == [
        "CH1",
        "CH2",
    ]
    assert interfaces["modbus_channels"][0]["tx"] == "GP0"
    assert interfaces["modbus_channels"][1]["tx"] == "GP4"


def test_factory_bootstrap_writes_dual_i2c_sections_for_apvpd():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)

        Settings.bootstrap_factory_defaults(
            tmpdir_path,
            detect_fn=lambda: (
                "apvpd",
                {
                    "i2c0": {"bus": 0, "scl": "GP1", "sda": "GP0", "addr": 0x76},
                    "i2c1": {"bus": 1, "scl": "GP3", "sda": "GP2", "addr": 0x76},
                },
            ),
            board_module=SimpleNamespace(),
            digitalio_module=SimpleNamespace(
                DigitalInOut=_ProbePin,
                Direction=SimpleNamespace(INPUT="input"),
                Pull=SimpleNamespace(UP="up"),
            ),
        )

        sensor_doc = Settings._read_toml_file(tmpdir_path / "sensor_i2c.toml")

    assert sensor_doc["Sensor"]["DEVICE"] == "apvpd"
    assert sensor_doc["I2Cbus"]["I2C_BUS"] == 0
    assert sensor_doc["I2Cbus"]["I2C_SCL"] == "GP1"
    assert sensor_doc["I2Cbus"]["I2C_SDA"] == "GP0"
    assert sensor_doc["I2Cbus"]["I2C_ADDR"] == 0x76
    assert sensor_doc["I2Cbus"]["Plant"]["I2C_BUS"] == 1
    assert sensor_doc["I2Cbus"]["Plant"]["I2C_SCL"] == "GP3"
    assert sensor_doc["I2Cbus"]["Plant"]["I2C_SDA"] == "GP2"
    assert sensor_doc["I2Cbus"]["Plant"]["I2C_ADDR"] == 0x76
    assert _display_metrics(sensor_doc) == (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Plant Temperature_F",
        "Plant Rel-Humidity",
        "Plant VPD",
    )


def test_factory_bootstrap_writes_dual_i2c_sections_for_apvpd_aht():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)

        Settings.bootstrap_factory_defaults(
            tmpdir_path,
            detect_fn=lambda: (
                "apvpd_aht",
                {
                    "i2c0": {"bus": 0, "scl": "GP1", "sda": "GP0", "addr": 0x38},
                    "i2c1": {"bus": 1, "scl": "GP3", "sda": "GP2", "addr": 0x38},
                },
            ),
            board_module=SimpleNamespace(),
            digitalio_module=SimpleNamespace(
                DigitalInOut=_ProbePin,
                Direction=SimpleNamespace(INPUT="input"),
                Pull=SimpleNamespace(UP="up"),
            ),
        )

        sensor_doc = Settings._read_toml_file(tmpdir_path / "sensor_i2c.toml")

    assert sensor_doc["Sensor"]["DEVICE"] == "apvpd_aht"
    assert sensor_doc["I2Cbus"]["I2C_ADDR"] == 0x38
    assert sensor_doc["I2Cbus"]["Plant"]["I2C_ADDR"] == 0x38
    assert _display_metrics(sensor_doc) == (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Plant Temperature_F",
        "Plant Rel-Humidity",
        "Plant VPD",
    )


def test_write_toml_file_preserves_template_section_and_key_order():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        document = Settings._read_toml_file(tmpdir_path / "settings.toml.def")
        document["Network"]["HOSTNAME"] = "apvpd-uv9he6"

        Settings._write_toml_file(tmpdir_path / "settings.toml", document)
        text = (tmpdir_path / "settings.toml").read_text(encoding="utf-8")

    assert (
        text.index("[Network]")
        < text.index("[Profile]")
        < text.index("[MQTT]")
        < text.index("[HomeAssistant]")
        < text.index("[Time]")
    )
    assert (
        text.index('SSID = ""')
        < text.index("PASSWORD = ")
        < text.index('AP_SSID = "Nodus_Setup"')
        < text.index("AP_PASSWORD = ")
        < text.index("AP_CHANNEL = 6")
        < text.index('HOSTNAME = "apvpd-uv9he6"')
        < text.index("HTTPPORT = 8000")
    )


def test_apply_updates_preserves_template_order_in_settings_file():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        document = Settings._read_toml_file(tmpdir_path / "settings.toml.def")
        Settings._write_toml_file(tmpdir_path / "settings.toml", document)
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

        Settings.apply_updates_to_directory(
            tmpdir_path,
            runtime_config,
            (
                {"section": "Network", "key": "HOSTNAME", "value": "apvpd-uv9he6"},
                {"section": "MQTT", "key": "BROKER", "value": "samhain.local"},
            ),
        )
        text = (tmpdir_path / "settings.toml").read_text(encoding="utf-8")

    assert (
        text.index("[Network]")
        < text.index("[Profile]")
        < text.index("[MQTT]")
        < text.index("[HomeAssistant]")
        < text.index("[Time]")
    )
    assert (
        text.index('BROKER = "samhain.local"')
        < text.index('BROKER_IP = ""')
        < text.index('BROKER_IP_ALT = ""')
        < text.index("PORT = 1883")
    )


def test_write_toml_file_keeps_previous_live_file_as_backup():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        path = tmpdir_path / "sensor_i2c.toml"
        path.write_text('[Sensor]\nDEVICE = "apvpd"\n', encoding="utf-8")
        document = Settings._read_toml_file(path)
        document["Sensor"]["LOCATION"] = "Greenhouse"

        Settings._write_toml_file(path, document)

        backup_text = (tmpdir_path / "sensor_i2c.toml.bak").read_text(encoding="utf-8")
        live_doc = Settings._read_toml_file(path)

    assert 'DEVICE = "apvpd"' in backup_text
    assert live_doc["Sensor"]["LOCATION"] == "Greenhouse"


def test_read_toml_file_uses_backup_when_live_file_is_empty():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        live_path = tmpdir_path / "sensor_i2c.toml"
        backup_path = tmpdir_path / "sensor_i2c.toml.bak"
        live_path.write_text("", encoding="utf-8")
        backup_path.write_text('[Sensor]\nDEVICE = "apvpd"\n', encoding="utf-8")

        document = Settings._read_toml_file(live_path)

    assert document["Sensor"]["DEVICE"] == "apvpd"


def test_write_toml_file_obfuscates_settings_password_fields():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _copy_defs(tmpdir_path)
        document = Settings._read_toml_file(tmpdir_path / "settings.toml.def")
        document["Network"]["HOSTNAME"] = "apvpd-uv9he6"
        document["Network"]["PASSWORD"] = "wifi-secret"
        document["Network"]["AP_PASSWORD"] = "ap-secret"
        document["MQTT"]["PASSWORD"] = "mqtt-secret"

        Settings._write_toml_file(tmpdir_path / "settings.toml", document)
        text = (tmpdir_path / "settings.toml").read_text(encoding="utf-8")

    assert (
        'PASSWORD = "{}"'.format(
            encode_password("wifi-secret", hostname="apvpd-uv9he6")
        )
        in text
    )
    assert (
        'AP_PASSWORD = "{}"'.format(
            encode_password("ap-secret", hostname="apvpd-uv9he6")
        )
        in text
    )
    assert (
        text.count(
            'PASSWORD = "{}"'.format(
                encode_password("mqtt-secret", hostname="apvpd-uv9he6")
            )
        )
        == 1
    )
