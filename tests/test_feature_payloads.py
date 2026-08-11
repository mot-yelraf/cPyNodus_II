"""Tests for normalized MQTT and runtime payload generation.

The cases pin externally visible schemas, identifiers, values, and optional
fields emitted by feature payload builders.
"""

import json
from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    DisplayConfig,
    HomeAssistantConfig,
    I2CConfig,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SoilModbusConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt_client import _mqtt_publish_packet_size
from cpynodus_ii.core.obfuscation import decode_password
from cpynodus_ii.features.payloads import (
    build_calibration_ack_payload,
    build_calibration_result_payload,
    build_config_ack_payload,
    build_config_result_payload,
    build_device_heartbeat_payload,
    build_homeassistant_discovery_plan,
    build_meta_patch_payload,
    build_onboarding_hello_payload,
    build_runtime_meta_payload,
    build_sensor_availability_payload,
    build_sensor_data_payload,
    build_switch_meta_payload,
    build_switch_state_payload,
)


def _ha_payload_for_metric(messages, metric):
    suffix = " {}".format(metric)
    for _topic, payload, _retain, _is_clear in messages:
        if str(payload.get("name", "")).endswith(suffix):
            return payload
    raise AssertionError("missing HA discovery metric {}".format(metric))


def test_sensor_data_payload_uses_values_contract():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
            location="TestLab",
        ),
    )
    snapshot = SimpleNamespace(
        phase="ready",
        metrics={
            "Temperature": 24.5,
            "Temperature_F": 76.1,
            "Ambient VPD": 1.2,
            "Air Quality": 87,
        },
    )

    payload = build_sensor_data_payload(runtime_config, snapshot)
    assert payload["schema"] == "nodus-sensor-data/v1"
    assert payload["sensor_id"] == "aqi-x943fm"
    assert payload["location"] == "TestLab"
    assert payload["values"]["Temperature_F"] == 76.1
    assert payload["values"]["Air Quality"] == 87
    assert "metrics" not in payload


def test_switch_state_payloads_use_channel_ids_and_states():
    runtime_config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            location="TestLab",
            channel_count=2,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
                SwitchChannelConfig(
                    key="SWITCH_2",
                    channel_id="S2-x943fm",
                    label="Pump",
                    enable_pin="GP10",
                    control_pin="GP21",
                ),
            ),
        )
    )
    state_snapshot = {
        "SWITCH_1": {"phase": "ready", "state": True},
        "SWITCH_2": {"phase": "ready", "state": False},
    }

    payloads = build_switch_state_payload(runtime_config, state_snapshot)
    assert payloads["SWITCH_1"]["channel_id"] == "S1-x943fm"
    assert payloads["SWITCH_1"]["state"] == "ON"
    assert payloads["SWITCH_2"]["state"] == "OFF"


def test_runtime_meta_payload_includes_sensor_and_switch_topics():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="wifi-secret",
            hostname="aqi-x943fm",
        ),
        mqtt=MQTTConfig(
            broker="broker.local",
            broker_ip="10.0.0.20",
            port=1885,
            use_tls=True,
            base_topic="nodus",
            username="nodus-user",
            password="mqtt-secret",
        ),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
            serial_number="x943fm",
            location="TestLab",
            display=DisplayConfig(
                metrics=("Air Quality", "Temperature", "", "Rel-Humidity"),
                styles=("graph24hr", "graph24hr", "", "gauge"),
            ),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            serial_number="x943fm",
            location="TestLab",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=True,
                ),
            ),
        ),
    )

    payload = build_runtime_meta_payload(
        runtime_config,
        version="0.1.0",
        active_broker="sensoria-hub-0.local",
        ip_address="10.0.0.44",
    )
    assert payload["schema"] == "nodus-meta/v1"
    assert payload["device_id"] == "aqi-x943fm"
    assert payload["mcu"] == "pico2w"
    assert payload["network"]["ssid"] == "PeaceHill"
    assert payload["network"]["hostname"] == "aqi-x943fm"
    assert payload["network"]["ipv4addr"] == "10.0.0.44"
    assert payload["network"]["password"] != "wifi-secret"
    assert (
        decode_password(payload["network"]["password"], hostname="aqi-x943fm")
        == "wifi-secret"
    )
    assert payload["profile"]["active_profile"] == "sensorius"
    assert "state" not in payload["status"]
    assert payload["status"]["heartbeat_topic"] == "nodus/aqi-x943fm/status/heartbeat"
    assert payload["capabilities"]["fwupdate"] is True
    assert payload["capabilities"]["log_transfer"] is True
    assert "logs" not in payload
    assert payload["fwupdate"] == {
        "schema": "nodus-fwupdate/v2",
        "transport": "http",
        "prepare_topic": "nodus/aqi-x943fm/fwupdate",
        "ack_topic": "nodus/aqi-x943fm/fwupdate/ack",
        "result_topic": "nodus/aqi-x943fm/fwupdate/result",
    }
    assert payload["mqtt"]["broker"] == "broker.local"
    assert payload["mqtt"]["broker_ip"] == "10.0.0.20"
    assert "broker_ip_alt" not in payload["mqtt"]
    assert payload["mqtt"]["active_broker"] == "sensoria-hub-0.local"
    assert payload["mqtt"]["port"] == 1885
    assert payload["mqtt"]["use_tls"] is True
    assert payload["mqtt"]["username"] == "nodus-user"
    assert payload["mqtt"]["base_topic"] == "nodus"
    assert payload["mqtt"]["password"] != "mqtt-secret"
    assert (
        decode_password(payload["mqtt"]["password"], hostname="aqi-x943fm")
        == "mqtt-secret"
    )
    assert payload["sensor"]["display_metrics"] == [
        "Air Quality",
        "Temperature",
        "Rel-Humidity",
    ]
    assert payload["sensor"]["display_styles"] == ["graph24hr", "graph24hr", "gauge"]
    assert payload["sensor"]["hardware"] == "BME680"
    assert payload["sensor"]["data_topic"] == "nodus/aqi-x943fm/data"
    assert payload["sensor"]["event_topic"] == "nodus/aqi-x943fm/event"
    assert payload["switch"]["channel_count"] == 1
    assert payload["switch"]["meta_topic"] == "nodus/aqi-x943fm/meta/switch"
    assert "location" not in payload["switch"]
    assert "channels" not in payload["switch"]


def test_runtime_meta_payload_distinguishes_co2_hardware_family():
    scd4x_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-scd4x",
            i2c=I2CConfig(address=0x62),
        )
    )
    scd30_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-scd30",
            i2c=I2CConfig(address=0x61),
        )
    )

    scd4x = build_runtime_meta_payload(scd4x_config, version="0.1.0")
    scd30 = build_runtime_meta_payload(scd30_config, version="0.1.0")

    assert scd4x["sensor"]["hardware"] == "SCD4x"
    assert scd30["sensor"]["hardware"] == "SCD30"


def test_runtime_meta_payload_reports_soil_variant_as_hardware():
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            device="soil",
            sensor_id="soil-bed-a",
            modbus=SoilModbusConfig(variant="soil_7in1"),
        )
    )

    payload = build_runtime_meta_payload(runtime_config, version="0.1.0")

    assert payload["sensor"]["hardware"] == "soil_7in1"


def test_runtime_meta_payload_uses_selected_board_profile_for_mcu(monkeypatch):
    monkeypatch.setenv("NODUS_BOARD_PROFILE", "xesp32s3")
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="xiao-node"),
        mqtt=MQTTConfig(base_topic="nodus"),
        sensor=DetectedSensor(sensor_id="xiao-node"),
    )

    payload = build_runtime_meta_payload(runtime_config, version="0.1.0")

    assert payload["mcu"] == "xesp32s3"


def test_onboarding_hello_payload_reports_device_type_and_mcu(monkeypatch):
    monkeypatch.setenv("NODUS_BOARD_PROFILE", "xesp32s3")
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="xiao-node"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="xiao-node",
            serial_number="abc123",
            i2c=I2CConfig(address=0x62),
        ),
    )

    payload = build_onboarding_hello_payload(
        runtime_config,
        {"onboard_token": "token-123"},
        version="v0.26.171.2",
    )

    assert payload["type"] == "nodus"
    assert payload["mcu"] == "xesp32s3"
    assert payload["sensor"] == {
        "present": True,
        "device": "co2",
        "hardware": "SCD4x",
    }


def test_onboarding_hello_payload_reports_soil_variant_as_hardware():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="soil-bed-a"),
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            device="soil",
            sensor_id="soil-bed-a",
            serial_number="abc123",
            modbus=SoilModbusConfig(variant="soil_7in1"),
        ),
    )

    payload = build_onboarding_hello_payload(
        runtime_config,
        {"onboard_token": "token-123"},
        version="v0.26.172.3",
    )

    assert payload["sensor"] == {
        "present": True,
        "device": "soil",
        "hardware": "soil_7in1",
    }


def test_onboarding_hello_payload_reports_absent_sensor_without_duplicate_id():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="switch-node"),
        switch=SwitchConfig(
            present=True,
            device_id="switch-node",
            serial_number="abc123",
        ),
    )

    payload = build_onboarding_hello_payload(
        runtime_config,
        {"onboard_token": "token-123"},
        version="v0.26.172.2",
    )

    assert payload["device_id"] == "switch-node"
    assert payload["sensor"] == {"present": False}


def test_switch_meta_payload_uses_split_contract_without_pin_fields():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="nodus"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            location="TestLab",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=False,
                ),
            ),
        ),
    )

    payload = build_switch_meta_payload(
        runtime_config,
        {"SWITCH_1": {"phase": "ready", "state": True}},
    )

    assert payload["schema"] == "nodus-meta-switch/v1"
    assert payload["device_id"] == "aqi-x943fm"
    assert payload["switch_device_id"] == "switch-x943fm"
    assert payload["channel_count"] == 1
    channel = payload["channels"][0]
    assert channel["state"] is True
    assert channel["set_topic"] == "nodus/S1-x943fm/config/set"
    assert channel["ack_topic"] == "nodus/S1-x943fm/config/ack"
    assert channel["result_topic"] == "nodus/S1-x943fm/config/result"
    assert "pin" not in channel
    assert "enable_pin" not in channel


def test_runtime_meta_payload_can_omit_switch_channel_detail():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="nodus"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            location="TestLab",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=True,
                ),
            ),
        ),
    )

    payload = build_runtime_meta_payload(
        runtime_config,
        version="0.1.0",
        include_switch_channels=False,
    )

    assert payload["capabilities"]["switch"] is True
    assert payload["location_group"]["members"] == ["aqi-x943fm", "S1-x943fm"]
    assert payload["switch"] == {
        "device_id": "switch-x943fm",
        "channel_count": 1,
        "meta_topic": "nodus/aqi-x943fm/meta/switch",
    }
    assert "channels" not in payload["switch"]
    assert payload["location_group"]["members"] == ["aqi-x943fm", "S1-x943fm"]
    assert "logs" not in payload


def test_compact_runtime_meta_stays_below_startup_payload_budget():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(hostname="co2-ykdvea", ssid="PeaceHill"),
        mqtt=MQTTConfig(
            broker="10.0.0.248",
            broker_ip="10.0.0.248",
            base_topic="nodus",
        ),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-ykdvea",
            serial_number="ykdvea",
            location="OfficeTest",
            display=DisplayConfig(
                metrics=(
                    "DewVPD Risk",
                    "Dew Point",
                    "Temperature_F",
                    "Humidity",
                    "Rel-Humidity",
                    "Dew Point_F",
                    "Temperature",
                    "Dew Point Deficit",
                    "Ambient VPD",
                    "CO2",
                ),
                styles=(
                    "gauge",
                    "graph24hr",
                    "graph24hr",
                    "gauge",
                    "gauge",
                    "graph24hr",
                    "graph24hr",
                    "gauge",
                    "gauge",
                    "gauge",
                ),
            ),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-ykdvea",
            serial_number="ykdvea",
            location="OfficeDesk",
            channel_count=2,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-ykdvea",
                    label="Fan",
                    last_state=True,
                ),
                SwitchChannelConfig(
                    key="SWITCH_2",
                    channel_id="S2-ykdvea",
                    label="Humidifier",
                    last_state=False,
                ),
            ),
        ),
    )

    payload = build_runtime_meta_payload(
        runtime_config,
        version="v0.26.143.3",
        active_broker="10.0.0.248",
        include_switch_channels=False,
    )
    encoded = json.dumps(payload, separators=(",", ":"))

    assert "logs" not in payload
    assert len(encoded) < 1500


def test_avpd_switch_runtime_meta_packet_stays_under_single_mss():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            hostname="avpd-0kl7sx",
            ssid="PeaceHill",
            password="wifi-secret",
        ),
        mqtt=MQTTConfig(
            broker="sensoria-hub-0.local",
            broker_ip="10.0.0.246",
            base_topic="nodus",
        ),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="avpd",
            sensor_id="avpd-0kl7sx",
            serial_number="0kl7sx",
            location="Unknown",
            display=DisplayConfig(
                metrics=(
                    "Ambient VPD",
                    "Temperature",
                    "Rel-Humidity",
                    "Baro-Pressure",
                    "Dew Point Deficit",
                    "DewVPD Risk",
                ),
                styles=(
                    "Graph24hr",
                    "Graph24hr",
                    "Graph24hr",
                    "Graph24hr",
                    "Graph24hr",
                    "Graph24hr",
                ),
            ),
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-0kl7sx",
            serial_number="0kl7sx",
            location="Unknown",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-0kl7sx",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        ),
    )

    payload = build_runtime_meta_payload(
        runtime_config,
        version="v0.26.150.15",
        active_broker="10.0.0.246",
        ip_address="10.0.0.226",
        include_switch_channels=False,
    )
    encoded = json.dumps(payload, separators=(",", ":"))
    packet_size = _mqtt_publish_packet_size(
        "nodus/avpd-0kl7sx/meta",
        encoded,
        qos=1,
    )

    assert payload["capabilities"]["switch"] is True
    assert payload["network"]["ipv4addr"] == "10.0.0.226"
    assert payload["switch"] == {
        "device_id": "switch-0kl7sx",
        "channel_count": 1,
        "meta_topic": "nodus/avpd-0kl7sx/meta/switch",
    }
    assert payload["sensor"]["hardware"] == "BME280"
    assert "username" not in payload["mqtt"]
    assert "password" not in payload["mqtt"]
    assert packet_size <= 1460


def test_switch_meta_payload_includes_channel_topic_map_without_pin_fields():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(base_topic="nodus"),
        sensor=DetectedSensor(sensor_id="aqi-x943fm"),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            location="TestLab",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                    last_state=False,
                ),
            ),
        ),
    )

    payload = build_switch_meta_payload(
        runtime_config,
        {"SWITCH_1": {"phase": "ready", "state": True}},
    )

    assert payload["schema"] == "nodus-meta-switch/v1"
    assert payload["device_id"] == "aqi-x943fm"
    assert payload["switch_device_id"] == "switch-x943fm"
    assert payload["channel_count"] == 1
    assert payload["channels"][0] == {
        "index": 1,
        "label": "Fan",
        "channel_id": "S1-x943fm",
        "state": True,
        "event_topic": "nodus/S1-x943fm/event",
        "state_topic": "nodus/S1-x943fm/state",
        "set_topic": "nodus/S1-x943fm/config/set",
        "ack_topic": "nodus/S1-x943fm/config/ack",
        "result_topic": "nodus/S1-x943fm/config/result",
        "availability_topic": "nodus/S1-x943fm/availability",
    }
    assert "pin" not in payload["channels"][0]
    assert "enable_pin" not in payload["channels"][0]


def test_availability_and_heartbeat_payloads_use_online_state():
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
    )

    availability = build_sensor_availability_payload(runtime_config, online=True)
    heartbeat = build_device_heartbeat_payload(runtime_config, online=False)
    assert availability["status"] == "online"
    assert heartbeat["status"] == "offline"
    assert heartbeat["device_id"] == "aqi-x943fm"


def test_homeassistant_discovery_uses_percent_for_soil_fertility_index():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="soil-bd1234"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            device="soil",
            sensor_id="soil-bd1234",
        ),
    )

    messages = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=SimpleNamespace(
            phase="ready",
            metrics={"CH1 Soil Fertility Index": 93.0},
        ),
    )

    payload = messages[0][1]
    assert payload["unit_of_measurement"] == "%"


def test_homeassistant_discovery_describes_co2_metric_units_and_classes():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="co2-ykdvea"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-ykdvea",
        ),
    )

    messages = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=SimpleNamespace(
            phase="ready",
            metrics={
                "CO2": 406.0,
                "Temperature": 28.48,
                "Temperature_F": 83.3,
                "Rel-Humidity": 35.0,
                "Humidity": 9.772,
                "Ambient VPD": 2.526,
                "Dew Point": 11.53,
                "Dew Point_F": 52.8,
                "Dew Point Deficit": 16.95,
                "DewVPD Risk": 35.0,
            },
        ),
    )

    co2 = _ha_payload_for_metric(messages, "CO2")
    assert co2["unit_of_measurement"] == "ppm"
    assert co2["device_class"] == "carbon_dioxide"
    assert co2["state_class"] == "measurement"
    assert _ha_payload_for_metric(messages, "Temperature")["unit_of_measurement"] == (
        "\u00b0C"
    )
    assert _ha_payload_for_metric(messages, "Temperature_F")[
        "unit_of_measurement"
    ] == "\u00b0F"
    assert _ha_payload_for_metric(messages, "Rel-Humidity")["device_class"] == (
        "humidity"
    )
    humidity = _ha_payload_for_metric(messages, "Humidity")
    assert humidity["unit_of_measurement"] == "g/m\u00b3"
    assert humidity["device_class"] == "absolute_humidity"
    vpd = _ha_payload_for_metric(messages, "Ambient VPD")
    assert vpd["unit_of_measurement"] == "kPa"
    assert vpd["device_class"] == "pressure"
    dew_deficit = _ha_payload_for_metric(messages, "Dew Point Deficit")
    assert dew_deficit["unit_of_measurement"] == "\u00b0C"
    assert dew_deficit["device_class"] == "temperature_delta"
    assert _ha_payload_for_metric(messages, "DewVPD Risk")[
        "unit_of_measurement"
    ] == "%"


def test_homeassistant_discovery_describes_plant_and_baro_metric_units():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="apvpd-lab"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="apvpd",
            sensor_id="apvpd-lab",
        ),
    )

    messages = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=SimpleNamespace(
            phase="ready",
            metrics={
                "Baro-Pressure": 842.5,
                "Plant Temperature": 24.2,
                "Plant Temperature_F": 75.6,
                "Plant Rel-Humidity": 60.0,
                "Plant Humidity": 13.1,
                "Plant VPD": 1.2,
                "Plant Baro-Pressure": 843.0,
                "Plant Dew Point": 16.1,
                "Plant Dew Point_F": 61.0,
                "Plant Dew Point Deficit": 8.1,
                "Plant DewVPD Risk": 25.0,
            },
        ),
    )

    baro = _ha_payload_for_metric(messages, "Baro-Pressure")
    assert baro["unit_of_measurement"] == "hPa"
    assert baro["device_class"] == "atmospheric_pressure"
    plant_temp = _ha_payload_for_metric(messages, "Plant Temperature")
    assert plant_temp["unit_of_measurement"] == "\u00b0C"
    assert plant_temp["device_class"] == "temperature"
    plant_humidity = _ha_payload_for_metric(messages, "Plant Humidity")
    assert plant_humidity["unit_of_measurement"] == "g/m\u00b3"
    assert plant_humidity["device_class"] == "absolute_humidity"
    plant_vpd = _ha_payload_for_metric(messages, "Plant VPD")
    assert plant_vpd["unit_of_measurement"] == "kPa"
    assert plant_vpd["device_class"] == "pressure"
    plant_baro = _ha_payload_for_metric(messages, "Plant Baro-Pressure")
    assert plant_baro["unit_of_measurement"] == "hPa"
    assert plant_baro["device_class"] == "atmospheric_pressure"
    plant_deficit = _ha_payload_for_metric(messages, "Plant Dew Point Deficit")
    assert plant_deficit["unit_of_measurement"] == "\u00b0C"
    assert plant_deficit["device_class"] == "temperature_delta"


def test_homeassistant_discovery_describes_light_aqi_and_gas_units():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="aqi-lab"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-lab",
        ),
    )

    messages = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=SimpleNamespace(
            phase="ready",
            metrics={
                "Air Quality": 87.0,
                "Gas": 12500.0,
                "Light Intensity": 5400.0,
                "Auto Light": 5400.0,
                "Estimated PPFD": 100.0,
                "Visible Light Intensity": 8.64,
            },
        ),
    )

    assert _ha_payload_for_metric(messages, "Air Quality")[
        "unit_of_measurement"
    ] == "AQI"
    assert _ha_payload_for_metric(messages, "Gas")["unit_of_measurement"] == (
        "\u03a9"
    )
    light = _ha_payload_for_metric(messages, "Light Intensity")
    assert light["unit_of_measurement"] == "lx"
    assert light["device_class"] == "illuminance"
    assert _ha_payload_for_metric(messages, "Auto Light")["device_class"] == (
        "illuminance"
    )
    assert _ha_payload_for_metric(messages, "Estimated PPFD")[
        "unit_of_measurement"
    ] == "\u00b5mol/m\u00b2/s"
    assert _ha_payload_for_metric(messages, "Visible Light Intensity")[
        "unit_of_measurement"
    ] == "mol/m\u00b2/day"


def test_homeassistant_discovery_describes_prefixed_soil_metric_units():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="soil-bd1234"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            device="soil",
            sensor_id="soil-bd1234",
        ),
    )

    messages = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=SimpleNamespace(
            phase="ready",
            metrics={
                "CH1 Soil Moisture": 43.0,
                "CH1 Soil Temp_C": 21.5,
                "CH1 Soil Temp_F": 70.7,
                "CH1 Soil pH": 6.8,
                "CH1 Soil EC": 0.55,
                "CH1 Soil Nitrogen": 11.0,
                "CH1 Soil Phosphorus": 22.0,
                "CH1 Soil Potassium": 33.0,
                "CH1 Soil Moisture Deficit": 0.0,
                "CH1 Soil Stress Index": 0.0,
                "CH1 Soil Fertility Index": 93.0,
            },
        ),
    )

    moisture = _ha_payload_for_metric(messages, "CH1 Soil Moisture")
    assert moisture["unit_of_measurement"] == "%"
    assert moisture["device_class"] == "moisture"
    soil_temp = _ha_payload_for_metric(messages, "CH1 Soil Temp_C")
    assert soil_temp["unit_of_measurement"] == "\u00b0C"
    assert soil_temp["device_class"] == "temperature"
    assert _ha_payload_for_metric(messages, "CH1 Soil Temp_F")[
        "unit_of_measurement"
    ] == "\u00b0F"
    soil_ph = _ha_payload_for_metric(messages, "CH1 Soil pH")
    assert soil_ph["unit_of_measurement"] == "pH"
    assert soil_ph["device_class"] == "ph"
    assert _ha_payload_for_metric(messages, "CH1 Soil EC")[
        "unit_of_measurement"
    ] == "mS/cm"
    assert _ha_payload_for_metric(messages, "CH1 Soil Nitrogen")[
        "unit_of_measurement"
    ] == "mg/kg"
    assert _ha_payload_for_metric(messages, "CH1 Soil Fertility Index")[
        "unit_of_measurement"
    ] == "%"


def test_config_and_calibration_payload_helpers_use_compact_contracts():
    config_ack = build_config_ack_payload("cfg-1", accepted=True, duplicate=False)
    config_result = build_config_result_payload(
        "cfg-1", applied=True, updated=2, error=""
    )
    restart_result = build_config_result_payload(
        "rst-1",
        applied=True,
        updated=0,
        error="",
        restart=True,
        restart_mode="hard",
    )
    calibration_ack = build_calibration_ack_payload("cal-1", accepted=True)
    calibration_result = build_calibration_result_payload(
        "cal-1", applied=False, error="calibration_not_supported"
    )

    assert config_ack["accepted"] is True
    assert config_result["updated"] == 2
    assert "restart" not in config_result
    assert restart_result["restart"] is True
    assert restart_result["restart_mode"] == "hard"
    assert calibration_ack["message_id"] == "cal-1"
    assert calibration_result["error"] == "calibration_not_supported"


def test_meta_patch_payload_keeps_sections_and_updates():
    runtime_config = RuntimeConfig(network=NetworkConfig(hostname="aqi-x943fm"))

    payload = build_meta_patch_payload(
        runtime_config,
        source="config_set",
        message_id="cfg-1",
        updates=(
            {"section": "Network", "key": "HOSTNAME", "value": "aqi-x943fm"},
            {"section": "MQTT", "key": "BROKER", "value": "broker.local"},
        ),
    )

    assert payload["schema"] == "nodus-meta-patch/v1"
    assert payload["source"] == "config_set"
    assert payload["sections"] == ["Network", "MQTT"]
    assert payload["updates"][1]["key"] == "BROKER"
