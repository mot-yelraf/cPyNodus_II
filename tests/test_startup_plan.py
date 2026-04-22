from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig
from cpynodus_ii.core.plan import StartupPlan
from cpynodus_ii.core.settings import Settings


def test_nodusweb_profile_disables_mqtt_and_keeps_web_enabled():
    settings = Settings(active_profile="nodusweb", sensor_kind="i2c")
    plan = StartupPlan.from_settings(settings)
    assert plan.profile == "nodusweb"
    assert plan.ap_mode is False
    assert plan.sensor_enabled is True
    assert plan.sensor_family == "i2c"
    assert plan.sensor_interface == "i2c"
    assert plan.active_sensor_file == "sensor_i2c.toml"
    assert plan.switch_enabled is False
    assert plan.mqtt_enabled is False
    assert plan.web_enabled is True
    assert plan.ntp_enabled is True
    assert plan.calibration_mqtt_available is False
    assert plan.onboarding_allowed is True


def test_mqtt_profile_enables_transport_and_disables_steady_state_web():
    settings = Settings(active_profile="sensorius", switch_enabled=True)
    plan = StartupPlan.from_settings(settings)
    assert plan.profile == "sensorius"
    assert plan.ap_mode is False
    assert plan.sensor_enabled is False
    assert plan.sensor_family == ""
    assert plan.sensor_interface == ""
    assert plan.active_sensor_file == ""
    assert plan.switch_enabled is True
    assert plan.mqtt_enabled is True
    assert plan.web_enabled is False
    assert plan.ntp_enabled is True
    assert plan.calibration_mqtt_available is True
    assert plan.onboarding_allowed is False


def test_standalone_alias_normalizes_to_nodusweb():
    settings = Settings(active_profile="standalone")
    plan = StartupPlan.from_settings(settings)
    assert plan.profile == "nodusweb"
    assert plan.mqtt_enabled is False
    assert plan.web_enabled is True


def test_soil_sensor_is_detected_by_startup_plan():
    settings = Settings(active_profile="sensorius", sensor_kind="soil")
    plan = StartupPlan.from_settings(settings)
    assert plan.sensor_enabled is True
    assert plan.sensor_family == "soil"
    assert plan.sensor_interface == "modbus_rs485"
    assert plan.active_sensor_file == "sensor_soil.toml"


def test_unknown_sensor_kind_does_not_present_as_detected_sensor():
    settings = Settings(active_profile="sensorius", sensor_kind="unknown")
    plan = StartupPlan.from_settings(settings)
    assert plan.sensor_enabled is False
    assert plan.sensor_family == ""
    assert plan.sensor_interface == ""
    assert plan.active_sensor_file == ""


def test_explicit_sensor_interface_is_preserved():
    settings = Settings(
        active_profile="sensorius",
        sensor_family="soil",
        sensor_interface="modbus_rs485",
    )
    plan = StartupPlan.from_settings(settings)
    assert plan.sensor_enabled is True
    assert plan.sensor_family == "soil"
    assert plan.sensor_interface == "modbus_rs485"


def test_ap_mode_keeps_web_enabled_and_disables_ntp_even_for_mqtt_profile():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        ap_mode=True,
        sensor=DetectedSensor(family="soil"),
    )
    plan = StartupPlan.from_runtime_config(runtime_config)
    assert plan.ap_mode is True
    assert plan.web_enabled is True
    assert plan.ntp_enabled is False
    assert plan.onboarding_allowed is True
    assert plan.mqtt_enabled is True


def test_homeassistant_profile_keeps_mqtt_but_disables_web_and_ntp():
    runtime_config = RuntimeConfig(active_profile="homeassistant")
    plan = StartupPlan.from_runtime_config(runtime_config)
    assert plan.profile == "homeassistant"
    assert plan.mqtt_enabled is True
    assert plan.web_enabled is False
    assert plan.ntp_enabled is False
    assert plan.calibration_mqtt_available is True
    assert plan.onboarding_allowed is False
