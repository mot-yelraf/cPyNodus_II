"""Tests for normalized MQTT and runtime payload generation."""

import json
from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    DisplayConfig,
    HomeAssistantConfig,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.obfuscation import decode_password
from cpynodus_ii.features.payloads import (
    build_calibration_ack_payload,
    build_calibration_result_payload,
    build_config_ack_payload,
    build_config_result_payload,
    build_device_heartbeat_payload,
    build_homeassistant_discovery_plan,
    build_meta_patch_payload,
    build_runtime_meta_payload,
    build_sensor_availability_payload,
    build_sensor_data_payload,
    build_switch_meta_payload,
    build_switch_state_payload,
)


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
    )
    assert payload["schema"] == "nodus-meta/v1"
    assert payload["device_id"] == "aqi-x943fm"
    assert payload["network"]["ssid"] == "PeaceHill"
    assert payload["network"]["hostname"] == "aqi-x943fm"
    assert payload["network"]["password"] != "wifi-secret"
    assert (
        decode_password(payload["network"]["password"], hostname="aqi-x943fm")
        == "wifi-secret"
    )
    assert payload["profile"]["active_profile"] == "sensorius"
    assert payload["status"]["state"] == "online"
    assert payload["status"]["heartbeat_topic"] == "nodus/aqi-x943fm/status/heartbeat"
    assert payload["capabilities"]["fwupdate"] is True
    assert payload["capabilities"]["log_transfer"] is True
    assert "logs" not in payload
    assert payload["fwupdate"] == {
        "schema": "nodus-fwupdate/v1",
        "transport": "http",
        "prepare_topic": "nodus/aqi-x943fm/fwupdate",
        "ack_topic": "nodus/aqi-x943fm/fwupdate/ack",
        "result_topic": "nodus/aqi-x943fm/fwupdate/result",
    }
    assert payload["mqtt"]["broker"] == "broker.local"
    assert payload["mqtt"]["broker_ip"] == "10.0.0.20"
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
    assert payload["sensor"]["data_topic"] == "nodus/aqi-x943fm/data"
    assert payload["sensor"]["event_topic"] == "nodus/aqi-x943fm/event"
    assert payload["switch"]["channel_count"] == 1
    assert payload["switch"]["meta_topic"] == "nodus/aqi-x943fm/meta/switch"
    assert "channels" not in payload["switch"]


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
        "location": "TestLab",
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


def test_config_and_calibration_payload_helpers_use_compact_contracts():
    config_ack = build_config_ack_payload("cfg-1", accepted=True, duplicate=False)
    config_result = build_config_result_payload(
        "cfg-1", applied=True, updated=2, error=""
    )
    calibration_ack = build_calibration_ack_payload("cal-1", accepted=True)
    calibration_result = build_calibration_result_payload(
        "cal-1", applied=False, error="calibration_not_supported"
    )

    assert config_ack["accepted"] is True
    assert config_result["updated"] == 2
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
