"""Describe startup intent derived from persisted settings.

The startup plan converts raw configuration into explicit enablement decisions
for sensors, switches, networking, MQTT, web services, and time sync so the
boot path can remain deterministic and easy to test.
"""

from dataclasses import dataclass

from cpynodus_ii.core.config import RuntimeConfig


@dataclass(frozen=True)
class StartupPlan:
    """Describe the intended runtime shape for the current boot."""

    profile: str
    ap_mode: bool
    sensor_enabled: bool
    sensor_family: str
    sensor_interface: str
    active_sensor_file: str
    switch_enabled: bool
    mqtt_enabled: bool
    web_enabled: bool
    ntp_enabled: bool
    calibration_mqtt_available: bool
    onboarding_allowed: bool
    ap_allowed: bool

    @classmethod
    def from_runtime_config(cls, runtime_config: RuntimeConfig):
        """Build a startup plan from normalized runtime configuration."""
        profile = runtime_config.active_profile
        sensor = runtime_config.sensor
        ap_mode = runtime_config.ap_mode
        return cls(
            profile=profile,
            ap_mode=ap_mode,
            sensor_enabled=sensor.present,
            sensor_family=sensor.family,
            sensor_interface=sensor.interface,
            active_sensor_file=sensor.active_config_file,
            switch_enabled=runtime_config.switch_config_present,
            mqtt_enabled=runtime_config.mqtt_enabled,
            web_enabled=runtime_config.web_enabled,
            ntp_enabled=runtime_config.ntp_enabled,
            calibration_mqtt_available=runtime_config.calibration_mqtt_available,
            onboarding_allowed=runtime_config.onboarding_allowed,
            ap_allowed=True,
        )

    @classmethod
    def from_settings(cls, settings):
        """Build a startup plan from a settings facade."""
        return cls.from_runtime_config(settings.runtime_config())
