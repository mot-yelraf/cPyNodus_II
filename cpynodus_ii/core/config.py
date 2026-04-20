"""Normalized runtime configuration models.

These models capture cPyNodus_II startup intent without carrying over the
legacy implementation structure from cPyNodus.
"""

from dataclasses import dataclass, field


def _raw_setattr(instance, name, value):
    setattr(instance, name, value)


def _normalize_profile(profile):
    value = str(profile or "nodusweb").strip().lower()
    if value == "standalone":
        return "nodusweb"
    return value or "nodusweb"


def _clean_str(value):
    return str(value or "").strip()


@dataclass(frozen=True)
class I2CConfig:
    """Normalized I2C sensor transport settings."""

    bus: int = 0
    scl_pin: str = ""
    sda_pin: str = ""
    address: int = 0

    def __post_init__(self):
        _raw_setattr(self, "bus", int(self.bus or 0))
        _raw_setattr(self, "scl_pin", _clean_str(self.scl_pin))
        _raw_setattr(self, "sda_pin", _clean_str(self.sda_pin))
        _raw_setattr(self, "address", int(self.address or 0))


@dataclass(frozen=True)
class SoilModbusConfig:
    """Normalized soil sensor Modbus transport settings."""

    uart_tx: str = "GP0"
    uart_rx: str = "GP1"
    baud: int = 9600
    timeout_s: float = 0.30
    address: int = 1
    variant: str = "canonical"

    def __post_init__(self):
        _raw_setattr(self, "uart_tx", _clean_str(self.uart_tx) or "GP0")
        _raw_setattr(self, "uart_rx", _clean_str(self.uart_rx) or "GP1")
        _raw_setattr(self, "baud", int(self.baud or 9600))
        _raw_setattr(self, "timeout_s", float(self.timeout_s or 0.30))
        _raw_setattr(self, "address", int(self.address or 1))
        _raw_setattr(self, "variant", _clean_str(self.variant) or "canonical")


@dataclass(frozen=True)
class SoilRegisterMap:
    """Normalized soil register mapping."""

    temperature: int = 0
    moisture: int = 1
    ec: int = 2
    ph: int = 3
    n: int = 4
    p: int = 5
    k: int = 6


@dataclass(frozen=True)
class SoilScaleMap:
    """Normalized soil scaling values."""

    moisture: float = 10.0
    temperature: float = 10.0
    ec: float = 1.0
    ph: float = 10.0
    n: float = 1.0
    p: float = 1.0
    k: float = 1.0


@dataclass(frozen=True)
class SoilThresholdConfig:
    """Normalized soil deficit thresholds."""

    wet_pct: float = 38.0
    dry_pct: float = 18.0


@dataclass(frozen=True)
class SoilStressConfig:
    """Normalized soil stress weighting and temperature bands."""

    temp_low_crit_c: float = 15.0
    temp_low_ok_c: float = 18.0
    temp_high_ok_c: float = 24.0
    temp_high_crit_c: float = 30.0
    moisture_weight_pct: float = 70.0
    temp_weight_pct: float = 30.0


@dataclass(frozen=True)
class NetworkConfig:
    """Normalized network and AP bootstrap settings."""

    ssid: str = ""
    password: str = ""
    ap_ssid: str = "Nodus_Setup"
    ap_password: str = "password"
    hostname: str = ""
    http_port: int = 8000

    def __post_init__(self):
        _raw_setattr(self, "ssid", _clean_str(self.ssid))
        _raw_setattr(self, "password", _clean_str(self.password))
        _raw_setattr(self, "ap_ssid", _clean_str(self.ap_ssid) or "Nodus_Setup")
        _raw_setattr(self, "ap_password", _clean_str(self.ap_password) or "password")
        _raw_setattr(self, "hostname", _clean_str(self.hostname))
        _raw_setattr(self, "http_port", int(self.http_port or 8000))


@dataclass(frozen=True)
class MQTTConfig:
    """Normalized MQTT connection and topic settings."""

    broker: str = ""
    broker_ip: str = ""
    port: int = 1883
    use_tls: bool = False
    base_topic: str = "nodus"
    username: str = ""
    password: str = ""

    def __post_init__(self):
        _raw_setattr(self, "broker", _clean_str(self.broker))
        _raw_setattr(self, "broker_ip", _clean_str(self.broker_ip))
        _raw_setattr(self, "port", int(self.port or 1883))
        _raw_setattr(self, "use_tls", bool(self.use_tls))
        _raw_setattr(self, "base_topic", _clean_str(self.base_topic) or "nodus")
        _raw_setattr(self, "username", _clean_str(self.username))
        _raw_setattr(self, "password", _clean_str(self.password))

    @property
    def preferred_host(self):
        return self.broker or self.broker_ip

    @property
    def connection_targets(self):
        targets = []
        if self.broker:
            targets.append(self.broker)
        if self.broker_ip and self.broker_ip not in targets:
            targets.append(self.broker_ip)
        return tuple(targets)


@dataclass(frozen=True)
class HomeAssistantConfig:
    """Normalized Home Assistant integration settings."""

    discovery_prefix: str = "homeassistant"
    base_topic: str = "nodus"
    publish_discovery_retain: bool = True
    publish_state_retain: bool = True
    publish_legacy_sensor_topic: bool = True

    def __post_init__(self):
        _raw_setattr(self, "discovery_prefix", _clean_str(self.discovery_prefix) or "homeassistant")
        _raw_setattr(self, "base_topic", _clean_str(self.base_topic) or "nodus")
        _raw_setattr(self, "publish_discovery_retain", bool(self.publish_discovery_retain))
        _raw_setattr(self, "publish_state_retain", bool(self.publish_state_retain))
        _raw_setattr(self, "publish_legacy_sensor_topic", bool(self.publish_legacy_sensor_topic))


@dataclass(frozen=True)
class TimeConfig:
    """Normalized time settings."""

    tz: str = "America/Denver"
    tz_offset: int = -25200
    tz_name: str = "MST"

    def __post_init__(self):
        _raw_setattr(self, "tz", _clean_str(self.tz) or "America/Denver")
        _raw_setattr(self, "tz_offset", int(self.tz_offset or -25200))
        _raw_setattr(self, "tz_name", _clean_str(self.tz_name) or "MST")


@dataclass(frozen=True)
class DetectedSensor:
    """Describe the sensor family detected or configured for this boot."""

    family: str = ""
    interface: str = ""
    active_config_file: str = ""
    device: str = ""
    sensor_id: str = ""
    serial_number: str = ""
    location: str = ""
    i2c: I2CConfig | None = None
    modbus: SoilModbusConfig | None = None
    soil_registers: SoilRegisterMap | None = None
    soil_scales: SoilScaleMap | None = None
    soil_thresholds: SoilThresholdConfig | None = None
    soil_stress: SoilStressConfig | None = None

    def __post_init__(self):
        family = str(self.family or "").strip().lower()
        interface = str(self.interface or "").strip().lower()

        if family not in {"", "i2c", "soil"}:
            family = ""
        if not interface:
            interface = self.default_interface_for_family(family)
        if interface not in {"", "i2c", "modbus_rs485"}:
            interface = ""

        active_config_file = str(self.active_config_file or "").strip()
        if not active_config_file:
            active_config_file = self.default_config_file_for_family(family)

        _raw_setattr(self, "family", family)
        _raw_setattr(self, "interface", interface)
        _raw_setattr(self, "active_config_file", active_config_file)
        _raw_setattr(self, "device", _clean_str(self.device).lower())
        _raw_setattr(self, "sensor_id", _clean_str(self.sensor_id))
        _raw_setattr(self, "serial_number", _clean_str(self.serial_number))
        _raw_setattr(self, "location", _clean_str(self.location))

    @staticmethod
    def default_interface_for_family(family):
        if family == "i2c":
            return "i2c"
        if family == "soil":
            return "modbus_rs485"
        return ""

    @staticmethod
    def default_config_file_for_family(family):
        if family == "i2c":
            return "sensor_i2c.toml"
        if family == "soil":
            return "sensor_soil.toml"
        return ""

    @property
    def present(self):
        return bool(self.family)


@dataclass(frozen=True)
class SwitchConfig:
    """Normalized switch runtime identity and enablement."""

    present: bool = False
    device_id: str = ""
    serial_number: str = ""
    location: str = ""
    channel_count: int = 0
    channels: tuple = ()

    def __post_init__(self):
        _raw_setattr(self, "present", bool(self.present))
        _raw_setattr(self, "device_id", _clean_str(self.device_id))
        _raw_setattr(self, "serial_number", _clean_str(self.serial_number))
        _raw_setattr(self, "location", _clean_str(self.location))
        _raw_setattr(self, "channel_count", max(0, int(self.channel_count or 0)))
        _raw_setattr(self, "channels", tuple(self.channels or ()))


@dataclass(frozen=True)
class SwitchChannelConfig:
    """Normalized switch channel definition."""

    key: str = ""
    label: str = ""
    channel_id: str = ""
    enable_pin: str = ""
    control_pin: str = ""
    last_state: bool = False
    override_script: bool = False

    def __post_init__(self):
        _raw_setattr(self, "key", _clean_str(self.key))
        _raw_setattr(self, "label", _clean_str(self.label))
        _raw_setattr(self, "channel_id", _clean_str(self.channel_id))
        _raw_setattr(self, "enable_pin", _clean_str(self.enable_pin))
        _raw_setattr(self, "control_pin", _clean_str(self.control_pin))
        _raw_setattr(self, "last_state", bool(self.last_state))
        _raw_setattr(self, "override_script", bool(self.override_script))


@dataclass(frozen=True)
class RuntimeConfig:
    """Describe the normalized runtime configuration for one boot."""

    active_profile: str = "nodusweb"
    network: NetworkConfig = field(default_factory=NetworkConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    sensor: DetectedSensor = field(default_factory=DetectedSensor)
    switch: SwitchConfig = field(default_factory=SwitchConfig)
    ap_mode: bool = False

    def __post_init__(self):
        _raw_setattr(self, "active_profile", _normalize_profile(self.active_profile))
        _raw_setattr(self, "ap_mode", bool(self.ap_mode))

    @property
    def mqtt_enabled(self):
        return self.active_profile in {"sensorius", "weewx", "homeassistant"}

    @property
    def web_enabled(self):
        return self.ap_mode or self.active_profile == "nodusweb"

    @property
    def ntp_enabled(self):
        return (not self.ap_mode) and self.active_profile == "nodusweb"

    @property
    def calibration_mqtt_available(self):
        return self.mqtt_enabled

    @property
    def onboarding_allowed(self):
        return self.ap_mode or self.active_profile == "nodusweb"

    @property
    def switch_config_present(self):
        return self.switch.present
