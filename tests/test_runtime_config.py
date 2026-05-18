"""Tests for runtime configuration loading and password handling."""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig, TimeConfig
from cpynodus_ii.core.obfuscation import encode_password
from cpynodus_ii.core.settings import Settings


def test_detected_sensor_defaults_file_and_interface_for_i2c():
    sensor = DetectedSensor(family="i2c")
    assert sensor.present is True
    assert sensor.family == "i2c"
    assert sensor.interface == "i2c"
    assert sensor.active_config_file == "sensor_i2c.toml"


def test_detected_sensor_defaults_file_and_interface_for_soil():
    sensor = DetectedSensor(family="soil")
    assert sensor.present is True
    assert sensor.family == "soil"
    assert sensor.interface == "modbus_rs485"
    assert sensor.active_config_file == "sensor_soil.toml"


def test_runtime_config_normalizes_standalone_to_nodusweb():
    runtime_config = RuntimeConfig(active_profile="standalone")
    assert runtime_config.active_profile == "nodusweb"
    assert runtime_config.mqtt_enabled is False
    assert runtime_config.web_enabled is True
    assert runtime_config.ntp_enabled is True
    assert runtime_config.network.ap_ssid == "Nodus_Setup"
    assert runtime_config.mqtt.base_topic == "nodus"


def test_settings_from_directory_loads_switch_only_runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        settings = Settings.from_directory(tmpdir_path)
        runtime_config = settings.runtime_config()

    assert runtime_config.active_profile == "sensorius"
    assert runtime_config.network.hostname == "switch-w9umh8"
    assert runtime_config.network.http_port == 8000
    assert runtime_config.mqtt.broker == "sensoria-hub-0.local"
    assert runtime_config.mqtt.broker_ip == "10.0.0.246"
    assert runtime_config.mqtt.port == 1883
    assert runtime_config.mqtt.preferred_host == "10.0.0.246"
    assert runtime_config.mqtt.connection_targets == (
        "10.0.0.246",
    )
    assert runtime_config.homeassistant.discovery_prefix == "homeassistant"
    assert runtime_config.time.tz == "America/Denver"
    assert runtime_config.sensor.present is False
    assert runtime_config.switch_config_present is True
    assert runtime_config.switch.device_id == "switch-w9umh8"
    assert runtime_config.switch.serial_number == "w9umh8"
    assert runtime_config.switch.location == "TestSwitch"
    assert runtime_config.switch.channel_count == 1


def test_settings_from_directory_loads_sensor_switch_runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        settings = Settings.from_directory(tmpdir_path)
        runtime_config = settings.runtime_config()

    assert runtime_config.active_profile == "sensorius"
    assert runtime_config.network.hostname == "aqi-x943fm"
    assert runtime_config.sensor.present is True
    assert runtime_config.sensor.family == "i2c"
    assert runtime_config.sensor.interface == "i2c"
    assert runtime_config.sensor.active_config_file == "sensor_i2c.toml"
    assert runtime_config.sensor.device == "aqi"
    assert runtime_config.sensor.sensor_id == "aqi-x943fm"
    assert runtime_config.sensor.serial_number == "x943fm"
    assert runtime_config.sensor.location == "TestLab"
    assert runtime_config.sensor.i2c is not None
    assert runtime_config.sensor.i2c.bus == 0
    assert runtime_config.sensor.i2c.scl_pin == "GP1"
    assert runtime_config.sensor.i2c.sda_pin == "GP0"
    assert runtime_config.sensor.i2c.address == 119
    assert runtime_config.sensor.display.metrics[:3] == (
        "Air Quality",
        "Temperature",
        "Rel-Humidity",
    )
    assert runtime_config.sensor.display.styles[:2] == (
        "Graph24hr",
        "Graph24hr",
    )
    assert runtime_config.switch_config_present is True
    assert runtime_config.switch.channel_count == 2
    assert runtime_config.switch.channels[0].key == "SWITCH_1"
    assert runtime_config.switch.channels[0].label == "Fan"
    assert runtime_config.switch.channels[0].channel_id == "S1-x943fm"
    assert runtime_config.switch.channels[0].enable_pin == "GP5"
    assert runtime_config.switch.channels[0].control_pin == "GP28"
    assert runtime_config.switch.channels[0].last_state is True
    assert runtime_config.switch.channels[1].key == "SWITCH_2"
    assert runtime_config.switch.channels[1].label == "Pump"
    assert runtime_config.switch.channels[1].channel_id == "S2-x943fm"
    assert runtime_config.switch.channels[1].enable_pin == "GP10"
    assert runtime_config.switch.channels[1].control_pin == "GP21"
    assert runtime_config.switch.channels[1].last_state is False


def test_settings_from_directory_prefers_soil_sensor_file_when_soil_config_is_active():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            (
                '[Profile]\nACTIVE_PROFILE = "homeassistant"\n'
                '[MQTT]\nBROKER = "broker.local"\nPORT = 1885\n'
            ),
            encoding="utf-8",
        )
        (tmpdir_path / "sensor_i2c.toml").write_text(
            '[Sensor]\nDEVICE = "aqi"\n',
            encoding="utf-8",
        )
        (tmpdir_path / "sensor_soil.toml").write_text(
            (
                '[Sensor]\nDEVICE = "soil"\nSENSOR_ID = "soil-1"\n'
                'SERIAL_NUM = "abc123"\nLOCATION = "Bed A"\n'
                '[Modbus]\nUART_TX = "GP4"\nUART_RX = "GP5"\n'
                "MODBUS_BAUD = 4800\nMODBUS_TIMEOUT_S = 0.5\n"
                'MODBUS_ADDR = 3\nSOIL_VARIANT = "soil_7in1"\n'
                "[SoilSensorRegisters]\nTEMPERATURE_REG = 11\n"
                "MOISTURE_REG = 12\nEC_REG = 13\nPH_REG = 14\n"
                "N_REG = 15\nP_REG = 16\nK_REG = 17\n"
                "[SoilSensorScales]\nMOISTURE_SCALE = 20.0\n"
                "TEMPERATURE_SCALE = 30.0\nEC_SCALE = 2.0\n"
                "PH_SCALE = 40.0\nN_SCALE = 5.0\nP_SCALE = 6.0\n"
                "K_SCALE = 7.0\n"
            ),
            encoding="utf-8",
        )

        settings = Settings.from_directory(tmpdir_path)
        runtime_config = settings.runtime_config()

    assert runtime_config.active_profile == "homeassistant"
    assert runtime_config.mqtt.broker == "broker.local"
    assert runtime_config.mqtt.port == 1885
    assert runtime_config.sensor.family == "soil"
    assert runtime_config.sensor.interface == "modbus_rs485"
    assert runtime_config.sensor.active_config_file == "sensor_soil.toml"
    assert runtime_config.sensor.sensor_id == "soil-1"
    assert runtime_config.sensor.serial_number == "abc123"
    assert runtime_config.sensor.location == "Bed A"
    assert runtime_config.sensor.modbus is not None
    assert runtime_config.sensor.modbus.uart_tx == "GP4"
    assert runtime_config.sensor.modbus.uart_rx == "GP5"
    assert runtime_config.sensor.modbus.baud == 4800
    assert runtime_config.sensor.modbus.timeout_s == 0.5
    assert runtime_config.sensor.modbus.address == 3
    assert runtime_config.sensor.modbus.variant == "soil_7in1"
    assert len(runtime_config.sensor.modbus.channels) == 1
    assert runtime_config.sensor.modbus.channels[0].name == "CH1"
    assert runtime_config.sensor.soil_registers is not None
    assert runtime_config.sensor.soil_registers.temperature == 11
    assert runtime_config.sensor.soil_registers.moisture == 12
    assert runtime_config.sensor.soil_scales is not None
    assert runtime_config.sensor.soil_scales.moisture == 20.0
    assert runtime_config.sensor.soil_scales.k == 7.0
    assert runtime_config.sensor.soil_thresholds is not None
    assert runtime_config.sensor.soil_thresholds.wet_pct == 38.0
    assert runtime_config.sensor.soil_thresholds.dry_pct == 18.0
    assert runtime_config.sensor.soil_stress is not None
    assert runtime_config.sensor.soil_stress.temp_low_crit_c == 15.0
    assert runtime_config.sensor.soil_stress.moisture_weight_pct == 70.0


def test_settings_from_directory_loads_dual_soil_modbus_channels():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            '[Profile]\nACTIVE_PROFILE = "homeassistant"\n',
            encoding="utf-8",
        )
        (tmpdir_path / "sensor_soil.toml").write_text(
            (
                '[Sensor]\nDEVICE = "soil"\nSENSOR_ID = "soil-1"\n'
                '[Modbus]\nUART_TX = "GP0"\nUART_RX = "GP1"\n'
                "MODBUS_BAUD = 9600\nMODBUS_ADDR = 1\n"
                '[Modbus.CH1]\nUART_TX = "GP0"\nUART_RX = "GP1"\n'
                "MODBUS_BAUD = 9600\nMODBUS_ADDR = 1\n"
                '[Modbus.CH2]\nUART_TX = "GP4"\nUART_RX = "GP5"\n'
                "MODBUS_BAUD = 4800\nMODBUS_ADDR = 3\n"
                'SOIL_VARIANT = "soil_7in1"\n'
            ),
            encoding="utf-8",
        )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    channels = runtime_config.sensor.modbus.channels
    assert [channel.name for channel in channels] == ["CH1", "CH2"]
    assert channels[0].uart_tx == "GP0"
    assert channels[0].baud == 9600
    assert channels[1].uart_rx == "GP5"
    assert channels[1].baud == 4800
    assert channels[1].address == 3


def test_runtime_config_keeps_minimal_defaults_for_missing_sections():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            '[Profile]\nACTIVE_PROFILE = "nodusweb"\n',
            encoding="utf-8",
        )

        settings = Settings.from_directory(tmpdir_path)
        runtime_config = settings.runtime_config()

    assert runtime_config.network.ap_ssid == "Nodus_Setup"
    assert runtime_config.network.ap_channel == 6
    assert runtime_config.network.http_port == 8000
    assert runtime_config.mqtt.base_topic == "nodus"
    assert runtime_config.homeassistant.publish_state_retain is True
    assert runtime_config.time.tz_name == "MST"
    assert runtime_config.time.ntp_server == ""
    assert runtime_config.time.ntp_server_ip == ""
    assert runtime_config.switch.present is False


def test_time_config_normalizes_hour_offset_to_seconds():
    normalized = TimeConfig(tz_offset=-6)

    assert normalized.tz_offset == -21600


def test_switch_only_runtime_config_parses_single_channel_details():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "switch_only"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )

        settings = Settings.from_directory(tmpdir_path)
        runtime_config = settings.runtime_config()

    assert runtime_config.switch.channel_count == 1
    assert len(runtime_config.switch.channels) == 1
    assert runtime_config.switch.channels[0].label == "Fan"
    assert runtime_config.switch.channels[0].channel_id == "S1-w9umh8"
    assert runtime_config.switch.channels[0].enable_pin == "GP5"
    assert runtime_config.switch.channels[0].control_pin == "GP28"
    assert runtime_config.switch.channels[0].last_state is False
    assert runtime_config.switch.channels[0].override_script is False


def test_settings_from_directory_decodes_obf1_password_fields():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        hostname = "switch-w9umh8"
        wifi_password = encode_password("wifi-secret", hostname=hostname)
        ap_password = encode_password("ap-secret", hostname=hostname)
        mqtt_password = encode_password("mqtt-secret", hostname=hostname)
        (tmpdir_path / "settings.toml").write_text(
            (
                "[Network]\n"
                'SSID = "PeaceHill"\n'
                'PASSWORD = "{}"\n'
                'AP_SSID = "Nodus_Setup"\n'
                'AP_PASSWORD = "{}"\n'
                'HOSTNAME = "{}"\n'
                "HTTPPORT = 8000\n\n"
                "[Profile]\n"
                'ACTIVE_PROFILE = "sensorius"\n\n'
                "[MQTT]\n"
                'BROKER = "broker.local"\n'
                'PASSWORD = "{}"\n'
            ).format(wifi_password, ap_password, hostname, mqtt_password),
            encoding="utf-8",
        )

        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

    assert runtime_config.network.password == "wifi-secret"
    assert runtime_config.network.ap_password == "ap-secret"
    assert runtime_config.mqtt.password == "mqtt-secret"
