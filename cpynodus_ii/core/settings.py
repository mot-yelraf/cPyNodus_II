"""Settings facade for the scaffold."""

import os

from cpynodus_ii.core.config import (
    DetectedSensor,
    HomeAssistantConfig,
    I2CConfig,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SoilModbusConfig,
    SoilRegisterMap,
    SoilScaleMap,
    SoilStressConfig,
    SoilThresholdConfig,
    SwitchConfig,
    SwitchChannelConfig,
    TimeConfig,
)
from cpynodus_ii.core.obfuscation import decode_password
from cpynodus_ii.core import toml_compat


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _join_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


class Settings:
    """Provide a narrow host-testable view of runtime configuration."""

    SETTINGS_FILE = "settings.toml"
    SENSOR_I2C_FILE = "sensor_i2c.toml"
    SENSOR_SOIL_FILE = "sensor_soil.toml"
    SWITCH_FILE = "switch.toml"

    def __init__(
        self,
        *,
        active_profile="nodusweb",
        sensor_enabled=False,
        sensor_kind="",
        sensor_family="",
        sensor_interface="",
        switch_enabled=False,
        mqtt_broker="",
        mqtt_port=1883,
    ):
        if sensor_enabled and not (sensor_family or sensor_kind):
            sensor_family = "i2c"
        sensor = DetectedSensor(
            family=(sensor_family or sensor_kind),
            interface=sensor_interface,
        )
        self._runtime_config = RuntimeConfig(
            active_profile=active_profile,
            mqtt=MQTTConfig(broker=mqtt_broker, port=mqtt_port),
            sensor=sensor,
            switch=SwitchConfig(present=switch_enabled),
        )

    @classmethod
    def from_runtime_config(cls, runtime_config):
        instance = cls()
        instance._runtime_config = runtime_config
        return instance

    @classmethod
    def from_directory(cls, root):
        root_path = str(root or ".")
        settings_doc = cls._read_toml_file(_join_path(root_path, cls.SETTINGS_FILE))
        sensor_i2c_doc = cls._read_toml_file(_join_path(root_path, cls.SENSOR_I2C_FILE))
        sensor_soil_doc = cls._read_toml_file(_join_path(root_path, cls.SENSOR_SOIL_FILE))
        switch_doc = cls._read_toml_file(_join_path(root_path, cls.SWITCH_FILE))
        runtime_config = cls._runtime_config_from_documents(
            settings_doc=settings_doc,
            sensor_i2c_doc=sensor_i2c_doc,
            sensor_soil_doc=sensor_soil_doc,
            switch_doc=switch_doc,
        )
        return cls.from_runtime_config(runtime_config)

    @classmethod
    def from_working_directory(cls):
        if _path_exists(cls.SETTINGS_FILE):
            return cls.from_directory(".")
        return cls()

    @staticmethod
    def _read_toml_file(path):
        if not _path_exists(path):
            return {}
        return toml_compat.load_file(path)

    @classmethod
    def apply_updates_to_directory(cls, root, runtime_config, updates, *, reload_runtime=True):
        """Persist supported TOML updates and return runtime config plus diagnostics."""
        root_path = str(root or ".")
        if not updates:
            return runtime_config, (), ()

        grouped_updates = {}
        for update in updates:
            filename = cls._target_file_for_update(runtime_config, update)
            if not filename:
                continue
            grouped_updates.setdefault(filename, []).append(update)

        applied_updates = []
        try:
            for filename, file_updates in grouped_updates.items():
                path = _join_path(root_path, filename)
                document = cls._read_toml_file(path)
                for update in file_updates:
                    if cls._apply_update_to_document(document, update):
                        applied_updates.append(update)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(cls._dump_toml(document))
        except OSError as exc:
            code = getattr(exc, "errno", None)
            if code in {30} or "read-only" in str(exc).lower():
                return runtime_config, tuple(applied_updates), ("read_only_filesystem",)
            return runtime_config, tuple(applied_updates), ("persistence_failed", str(exc))

        if not reload_runtime:
            return runtime_config, tuple(applied_updates), ()

        reloaded = cls.from_directory(root_path).runtime_config()
        return reloaded, tuple(applied_updates), ()

    @staticmethod
    def filesystem_writable(root="/"):
        """Return best-effort filesystem writability for diagnostics."""
        try:
            import storage  # type: ignore
        except ImportError:
            return None
        try:
            mount = storage.getmount(str(root))
        except Exception:
            try:
                mount = storage.getmount("/")
            except Exception:
                return None
        readonly = getattr(mount, "readonly", None)
        if readonly is None:
            return None
        return not bool(readonly)

    @classmethod
    def _runtime_config_from_documents(
        cls,
        *,
        settings_doc,
        sensor_i2c_doc,
        sensor_soil_doc,
        switch_doc,
    ):
        profile_doc = settings_doc.get("Profile", {})
        network_doc = settings_doc.get("Network", {})
        mqtt_doc = settings_doc.get("MQTT", {})
        homeassistant_doc = settings_doc.get("HomeAssistant", {})
        time_doc = settings_doc.get("Time", {})
        sensor = cls._detect_sensor(sensor_i2c_doc=sensor_i2c_doc, sensor_soil_doc=sensor_soil_doc)
        switch = cls._detect_switch(switch_doc=switch_doc)
        return RuntimeConfig(
            active_profile=profile_doc.get("ACTIVE_PROFILE", "nodusweb"),
            network=NetworkConfig(
                ssid=network_doc.get("SSID", ""),
                password=decode_password(
                    network_doc.get("PASSWORD", ""),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
                ap_ssid=network_doc.get("AP_SSID", "Nodus_Setup"),
                ap_password=decode_password(
                    network_doc.get("AP_PASSWORD", "password"),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
                hostname=network_doc.get("HOSTNAME", ""),
                http_port=network_doc.get("HTTPPORT", 8000),
            ),
            mqtt=MQTTConfig(
                broker=mqtt_doc.get("BROKER", ""),
                broker_ip=mqtt_doc.get("BROKER_IP", ""),
                port=mqtt_doc.get("PORT", 1883),
                use_tls=mqtt_doc.get("USE_TLS", False),
                base_topic=mqtt_doc.get("BASE_TOPIC", "nodus"),
                username=mqtt_doc.get("USERNAME", ""),
                password=decode_password(
                    mqtt_doc.get("PASSWORD", ""),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
            ),
            homeassistant=HomeAssistantConfig(
                discovery_prefix=homeassistant_doc.get("DISCOVERY_PREFIX", "homeassistant"),
                base_topic=homeassistant_doc.get("BASE_TOPIC", "nodus"),
                publish_discovery_retain=homeassistant_doc.get("PUBLISH_DISCOVERY_RETAIN", True),
                publish_state_retain=homeassistant_doc.get("PUBLISH_STATE_RETAIN", True),
                publish_legacy_sensor_topic=homeassistant_doc.get("PUBLISH_LEGACY_SENSOR_TOPIC", True),
            ),
            time=TimeConfig(
                tz=time_doc.get("TZ", "America/Denver"),
                tz_offset=time_doc.get("TZ_OFFSET", -25200),
                tz_name=time_doc.get("TZ_NAME", "MST"),
            ),
            sensor=sensor,
            switch=switch,
        )

    @classmethod
    def _detect_sensor(cls, *, sensor_i2c_doc, sensor_soil_doc):
        soil_sensor_doc = sensor_soil_doc.get("Sensor", {})
        soil_modbus_doc = sensor_soil_doc.get("Modbus", {})
        soil_register_doc = sensor_soil_doc.get("SoilSensorRegisters", {})
        soil_scale_doc = sensor_soil_doc.get("SoilSensorScales", {})
        soil_deficit_doc = sensor_soil_doc.get("SoilDeficit", {})
        soil_stress_doc = sensor_soil_doc.get("SoilStress", {})
        i2c_sensor_doc = sensor_i2c_doc.get("Sensor", {})
        i2c_bus_doc = sensor_i2c_doc.get("I2Cbus", {})
        soil_device = str(soil_sensor_doc.get("DEVICE", "") or "").strip().lower()
        i2c_device = str(i2c_sensor_doc.get("DEVICE", "") or "").strip().lower()

        if soil_device == "soil":
            return DetectedSensor(
                family="soil",
                interface="modbus_rs485",
                active_config_file=cls.SENSOR_SOIL_FILE,
                device=soil_device,
                sensor_id=soil_sensor_doc.get("SENSOR_ID", ""),
                serial_number=soil_sensor_doc.get("SERIAL_NUM", ""),
                location=soil_sensor_doc.get("LOCATION", ""),
                modbus=SoilModbusConfig(
                    uart_tx=soil_modbus_doc.get("UART_TX", "GP0"),
                    uart_rx=soil_modbus_doc.get("UART_RX", "GP1"),
                    baud=soil_modbus_doc.get("MODBUS_BAUD", 9600),
                    timeout_s=soil_modbus_doc.get("MODBUS_TIMEOUT_S", 0.30),
                    address=soil_modbus_doc.get("MODBUS_ADDR", 1),
                    variant=soil_modbus_doc.get("SOIL_VARIANT", "canonical"),
                ),
                soil_registers=SoilRegisterMap(
                    temperature=int(soil_register_doc.get("TEMPERATURE_REG", 0)),
                    moisture=int(soil_register_doc.get("MOISTURE_REG", 1)),
                    ec=int(soil_register_doc.get("EC_REG", 2)),
                    ph=int(soil_register_doc.get("PH_REG", 3)),
                    n=int(soil_register_doc.get("N_REG", 4)),
                    p=int(soil_register_doc.get("P_REG", 5)),
                    k=int(soil_register_doc.get("K_REG", 6)),
                ),
                soil_scales=SoilScaleMap(
                    moisture=float(soil_scale_doc.get("MOISTURE_SCALE", 10.0)),
                    temperature=float(soil_scale_doc.get("TEMPERATURE_SCALE", 10.0)),
                    ec=float(soil_scale_doc.get("EC_SCALE", 1.0)),
                    ph=float(soil_scale_doc.get("PH_SCALE", 10.0)),
                    n=float(soil_scale_doc.get("N_SCALE", 1.0)),
                    p=float(soil_scale_doc.get("P_SCALE", 1.0)),
                    k=float(soil_scale_doc.get("K_SCALE", 1.0)),
                ),
                soil_thresholds=SoilThresholdConfig(
                    wet_pct=float(soil_deficit_doc.get("SPD_WET_THRESHOLD_PCT", 38.0)),
                    dry_pct=float(soil_deficit_doc.get("SPD_DRY_THRESHOLD_PCT", 18.0)),
                ),
                soil_stress=SoilStressConfig(
                    temp_low_crit_c=float(soil_stress_doc.get("SSI_TEMP_LOW_CRIT_C", 15.0)),
                    temp_low_ok_c=float(soil_stress_doc.get("SSI_TEMP_LOW_OK_C", 18.0)),
                    temp_high_ok_c=float(soil_stress_doc.get("SSI_TEMP_HIGH_OK_C", 24.0)),
                    temp_high_crit_c=float(soil_stress_doc.get("SSI_TEMP_HIGH_CRIT_C", 30.0)),
                    moisture_weight_pct=float(soil_stress_doc.get("SSI_MOISTURE_WEIGHT_PCT", 70.0)),
                    temp_weight_pct=float(soil_stress_doc.get("SSI_TEMP_WEIGHT_PCT", 30.0)),
                ),
            )

        if i2c_device:
            return DetectedSensor(
                family="i2c",
                interface="i2c",
                active_config_file=cls.SENSOR_I2C_FILE,
                device=i2c_device,
                sensor_id=i2c_sensor_doc.get("SENSOR_ID", ""),
                serial_number=i2c_sensor_doc.get("SERIAL_NUM", ""),
                location=i2c_sensor_doc.get("LOCATION", ""),
                i2c=I2CConfig(
                    bus=i2c_bus_doc.get("I2C_BUS", 0),
                    scl_pin=i2c_bus_doc.get("I2C_SCL", ""),
                    sda_pin=i2c_bus_doc.get("I2C_SDA", ""),
                    address=i2c_bus_doc.get("I2C_ADDR", 0),
                ),
            )

        return DetectedSensor()

    @staticmethod
    def _detect_switch(*, switch_doc):
        switch_section = switch_doc.get("Switch", {})
        if not switch_section:
            return SwitchConfig()

        channels = []
        for index in (1, 2):
            control_pin = str(switch_section.get(f"SWITCH_{index}_PIN", "") or "").strip()
            enable_pin = str(switch_section.get(f"SWITCH_{index}_ENABLE_PIN", "") or "").strip()
            if not (control_pin or enable_pin):
                continue
            channels.append(
                SwitchChannelConfig(
                    key=f"SWITCH_{index}",
                    label=switch_section.get(f"SWITCH_{index}_LABEL", ""),
                    channel_id=switch_section.get(f"SWITCH_{index}_CHANNEL_ID", ""),
                    enable_pin=enable_pin,
                    control_pin=control_pin,
                    last_state=switch_section.get(f"SWITCH_{index}_LAST_STATE", False),
                    override_script=switch_section.get(f"SWITCH_{index}_OVERRIDE_SCRIPT", False),
                )
            )

        return SwitchConfig(
            present=True,
            device_id=switch_section.get("SWITCH_DEVICE_ID", ""),
            serial_number=switch_section.get("DEVICE_SERIAL_NUM", ""),
            location=switch_section.get("SWITCH_LOCATION", ""),
            channel_count=len(channels),
            channels=tuple(channels),
        )

    @classmethod
    def _target_file_for_update(cls, runtime_config, update):
        section = str(update.get("section", "") or "").strip()
        if section in {"Network", "Profile", "MQTT", "HomeAssistant", "Time"}:
            return cls.SETTINGS_FILE
        if section == "Switch":
            return cls.SWITCH_FILE
        if section.startswith("Calibration") or section in {"Display", "Display.Style"}:
            if runtime_config.sensor.active_config_file:
                return runtime_config.sensor.active_config_file
            return cls.SENSOR_I2C_FILE
        return ""

    @staticmethod
    def _apply_update_to_document(document, update):
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        if not (section and key):
            return False
        current = document
        for part in section.split("."):
            current = current.setdefault(part, {})
        current[key] = update.get("value")
        return True

    @classmethod
    def _dump_toml(cls, document):
        return toml_compat.dumps(document)

    def active_profile(self):
        return self._runtime_config.active_profile

    def sensor_enabled(self):
        return self._runtime_config.sensor.present

    def sensor_family(self):
        return self._runtime_config.sensor.family

    def sensor_interface(self):
        return self._runtime_config.sensor.interface

    def sensor_kind(self):
        return self._runtime_config.sensor.family

    def active_sensor_file(self):
        return self._runtime_config.sensor.active_config_file

    def switch_enabled(self):
        return self._runtime_config.switch_config_present

    def mqtt_enabled(self):
        return self._runtime_config.mqtt_enabled

    def mqtt_config(self):
        return {
            "BROKER": self._runtime_config.mqtt.broker,
            "PORT": self._runtime_config.mqtt.port,
        }

    def runtime_config(self):
        return self._runtime_config
