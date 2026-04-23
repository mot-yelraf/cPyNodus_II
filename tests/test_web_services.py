"""Tests for onboarding and setup web-service helpers."""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.config import (
    DetectedSensor,
    I2CConfig,
    RuntimeConfig,
    SensorCalibration,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.web_services import (
    apply_itaot_init_payload,
    bootstrap_routes_enabled,
    build_itaot_meta_payload,
    load_onboarding_state,
    normalize_itaot_init_payload,
)


def test_normalize_itaot_init_payload_requires_bootstrap_fields():
    normalized = normalize_itaot_init_payload({})

    assert "onboard_token_required" in normalized["errors"]
    assert "ssid_required" in normalized["errors"]
    assert "password_required" in normalized["errors"]
    assert "hostname_required" in normalized["errors"]
    assert "mqtt_broker_host_required" in normalized["errors"]
    assert "mqtt_broker_port_required" in normalized["errors"]


def test_apply_itaot_init_payload_persists_existing_settings_fields_only():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            "[Profile]\nACTIVE_PROFILE = \"nodusweb\"\n",
            encoding="utf-8",
        )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

        result = apply_itaot_init_payload(
            {
                "onboard_token": "token-123",
                "ssid": "TestWiFi",
                "password": "secretpass",
                "hostname": "co2-w9umh8",
                "mqtt": {
                    "broker_host": "sensorius-broker.local",
                    "broker_port": 1883,
                },
            },
            runtime_config,
            settings_root=tmpdir_path,
        )

        settings = Settings.from_directory(tmpdir_path).runtime_config()
        onboarding_state = load_onboarding_state(tmpdir_path)
        settings_text = (tmpdir_path / "settings.toml").read_text(encoding="utf-8")

    assert result.status_code == 200
    assert result.accepted is True
    assert result.rebooting is True
    assert settings.active_profile == "sensorius"
    assert settings.network.ssid == "TestWiFi"
    assert settings.network.password == "secretpass"
    assert settings.network.hostname == "co2-w9umh8"
    assert settings.mqtt.broker == "sensorius-broker.local"
    assert settings.mqtt.port == 1883
    assert onboarding_state["onboard_token"] == "token-123"
    assert onboarding_state["active_profile"] == "sensorius"
    assert "onboard_token" not in settings_text
    assert "token-123" not in settings_text


def test_bootstrap_routes_enabled_for_ap_mode_only():
    assert bootstrap_routes_enabled(RuntimeConfig(active_profile="nodusweb", ap_mode=True)) is True
    assert bootstrap_routes_enabled(RuntimeConfig(active_profile="sensorius")) is False
    assert bootstrap_routes_enabled(RuntimeConfig(active_profile="nodusweb")) is False
    assert bootstrap_routes_enabled(RuntimeConfig(active_profile="homeassistant")) is False


def test_build_itaot_meta_payload_includes_sensor_switch_identity_and_state():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-w9umh8",
            serial_number="w9umh8",
            location="Bench A",
            i2c=I2CConfig(bus=0, scl_pin="GP1", sda_pin="GP0", address=0x62),
            calibration_device=SensorCalibration(co2_offset=-400.0),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-w9umh8",
            serial_number="w9umh8",
            location="Bench A",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    label="Fan",
                    channel_id="S1-w9umh8",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=False,
                ),
            ),
        ),
    )

    payload = build_itaot_meta_payload(
        runtime_config,
        version="v0.26.112.12",
        ip_address="10.0.0.44",
        switch_states={"SWITCH_1": {"state": True}},
    )

    assert payload["schema"] == "itaot-meta/v1"
    assert payload["device_id"] == "co2-w9umh8"
    assert payload["network"]["ipv4addr"] == "10.0.0.44"
    assert payload["device"]["capabilities"] == {"sensor": True, "switch": True}
    assert payload["sensor"]["present"] is True
    assert payload["sensor"]["display_metrics"] == ["CO2", "Temperature"]
    assert payload["sensor"]["calibration"]["calibrated"] is True
    assert payload["switch"]["present"] is True
    assert payload["switch"]["channels"][0]["state"] is True
    assert payload["location_group"]["members"] == ["co2-w9umh8", "S1-w9umh8"]


def test_build_itaot_meta_payload_falls_back_to_switch_identity_without_sensor():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        switch=SwitchConfig(
            present=True,
            device_id="switch-w9umh8",
            serial_number="w9umh8",
            location="Bench A",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    label="Fan",
                    channel_id="S1-w9umh8",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=False,
                ),
            ),
        ),
    )

    payload = build_itaot_meta_payload(
        runtime_config,
        version="v0.26.112.12",
        ip_address="10.0.0.45",
    )

    assert payload["device_id"] == "switch-w9umh8"
    assert payload["sensor"]["present"] is False
    assert payload["sensor"]["display_metrics"] == []
    assert payload["switch"]["channels"][0]["state"] is False
