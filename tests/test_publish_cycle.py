"""Tests for startup and steady-state MQTT publish-cycle helpers."""

from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    HomeAssistantConfig,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import (
    publish_availability_refresh_cycle,
    publish_sensor_cycle,
    publish_shutdown_cycle,
    publish_startup_cycle,
    publish_switch_result,
)


def test_startup_cycle_publishes_heartbeat_meta_sensor_and_switch_topics():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="nodus"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
            location="TestLab",
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
                ),
            ),
        ),
    )
    sensor_snapshot = SimpleNamespace(phase="ready", metrics={"Temperature": 24.5})
    switch_snapshot = {"SWITCH_1": {"phase": "ready", "state": True}}

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="0.1.0",
        sensor_snapshot=sensor_snapshot,
        switch_snapshot=switch_snapshot,
    )

    assert result.phase == "published"
    assert result.published_count == 6
    assert "nodus/aqi-x943fm/status/heartbeat" in result.topics
    assert "nodus/aqi-x943fm/meta" in result.topics
    assert "nodus/aqi-x943fm/data" in result.topics
    assert "nodus/S1-x943fm/state" in result.topics
    assert transport.published_messages[0].retain is True
    topics = [message.topic for message in transport.published_messages]
    assert topics.index("nodus/aqi-x943fm/meta") < topics.index("nodus/aqi-x943fm/data")


def test_sensor_cycle_publishes_non_retained_sensor_data():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
    )
    sensor_snapshot = SimpleNamespace(
        phase="ready", metrics={"Temperature": 24.5, "Air Quality": 80}
    )

    result = publish_sensor_cycle(transport, runtime_config, sensor_snapshot)

    assert result.phase == "published"
    assert result.published_count == 1
    assert result.topics == ("nodus/aqi-x943fm/data",)
    assert transport.published_messages[-1].retain is False
    assert transport.published_messages[-1].payload["values"]["Air Quality"] == 80
    assert "metrics" not in transport.published_messages[-1].payload


def test_sensor_cycle_skips_when_snapshot_not_ready():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
    )
    sensor_snapshot = SimpleNamespace(phase="error", metrics={})

    result = publish_sensor_cycle(transport, runtime_config, sensor_snapshot)

    assert result.phase == "skipped"
    assert result.published_count == 0
    assert "sensor_snapshot_not_ready" in result.errors


def test_startup_cycle_publishes_online_availability_before_sensor_ready():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="co2-29j39c"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-29j39c",
        ),
    )
    sensor_snapshot = SimpleNamespace(phase="error", metrics={})

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="v0.26.116.3",
        sensor_snapshot=sensor_snapshot,
        switch_snapshot={},
    )

    assert "nodus/co2-29j39c/availability" in result.topics
    assert "nodus/co2-29j39c/data" not in result.topics
    availability = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/co2-29j39c/availability"
    )
    assert availability.retain is True
    assert availability.payload["status"] == "online"


def test_availability_refresh_cycle_republishes_retained_online_topics():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        ),
    )

    result = publish_availability_refresh_cycle(transport, runtime_config)

    assert result.phase == "published"
    assert result.published_count == 2
    assert result.topics == (
        "nodus/aqi-x943fm/availability",
        "nodus/S1-x943fm/availability",
    )
    assert transport.published_messages[0].retain is True
    assert transport.published_messages[1].retain is True
    assert transport.published_messages[0].payload["status"] == "online"
    assert transport.published_messages[1].payload["status"] == "online"


def test_sensor_cycle_preserves_snapshot_errors_when_skipped():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-29j39c",
        ),
    )
    sensor_snapshot = SimpleNamespace(
        phase="error",
        metrics={},
        errors=("sensor_metrics_empty",),
    )

    result = publish_sensor_cycle(transport, runtime_config, sensor_snapshot)

    assert result.phase == "skipped"
    assert result.published_count == 0
    assert result.errors == ("sensor_metrics_empty",)
    assert transport.published_messages == []


def test_switch_result_publishes_result_and_retained_state():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        )
    )
    apply_result = SimpleNamespace(
        phase="ready",
        key="SWITCH_1",
        device_id="switch-x943fm",
        channel_id="S1-x943fm",
        applied_state=True,
        errors=(),
    )

    result = publish_switch_result(
        transport, runtime_config, apply_result, message_id="cfg-1"
    )

    assert result.phase == "published"
    assert result.published_count == 3
    assert result.topics == (
        "nodus/S1-x943fm/config/result",
        "nodus/S1-x943fm/event",
        "nodus/S1-x943fm/state",
    )
    assert transport.published_messages[-1].retain is True
    assert transport.published_messages[0].payload == {
        "message_id": "cfg-1",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[1].retain is False
    assert transport.published_messages[1].payload["schema"] == "nodus-switch-event/v1"
    assert transport.published_messages[1].payload["state"] == "ON"
    assert transport.published_messages[1].payload["message_id"] == "cfg-1"
    assert transport.published_messages[-1].payload == "ON"


def test_startup_cycle_uses_configured_base_topic():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="greenhouse"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
    )

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="0.1.0",
        sensor_snapshot=SimpleNamespace(phase="ready", metrics={"Temperature": 24.5}),
        switch_snapshot={},
    )

    assert "greenhouse/aqi-x943fm/status/heartbeat" in result.topics
    assert "greenhouse/aqi-x943fm/meta" in result.topics
    assert "greenhouse/aqi-x943fm/data" in result.topics


def test_startup_cycle_publishes_onboarding_hello_when_state_present():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="nodus"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
            serial_number="x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            serial_number="x943fm",
        ),
    )

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="v0.26.114.1",
        onboarding_state={"onboard_token": "token-123"},
        sensor_snapshot=SimpleNamespace(phase="ready", metrics={"Temperature": 24.5}),
        switch_snapshot={},
    )

    assert "nodus/aqi-x943fm/onboard/hello" in result.topics
    hello_message = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/aqi-x943fm/onboard/hello"
    )
    assert hello_message.retain is False
    assert hello_message.payload == {
        "onboard_token": "token-123",
        "device_id": "aqi-x943fm",
        "hostname": "aqi-x943fm",
        "serial": "x943fm",
        "type": "pico2w",
        "version": "v0.26.114.1",
        "capabilities": {"sensor": True, "switch": True},
    }


def test_shutdown_cycle_publishes_offline_heartbeat_and_availability():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        network=NetworkConfig(hostname="aqi-x943fm"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        ),
    )

    result = publish_shutdown_cycle(transport, runtime_config)

    assert result.phase == "published"
    assert result.published_count == 3
    assert result.topics == (
        "nodus/aqi-x943fm/status/heartbeat",
        "nodus/aqi-x943fm/availability",
        "nodus/S1-x943fm/availability",
    )
    assert transport.published_messages[0].payload["status"] == "offline"
    assert transport.published_messages[-1].payload["status"] == "offline"


def test_homeassistant_startup_cycle_publishes_discovery_topics():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        network=NetworkConfig(hostname="co2-ykdvea"),
        mqtt=MQTTConfig(broker="broker.local", base_topic="nodus"),
        homeassistant=HomeAssistantConfig(discovery_prefix="homeassistant"),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="co2",
            sensor_id="co2-ykdvea",
            location="Bench A",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-ykdvea",
            location="Bench A",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-ykdvea",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        ),
    )

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="0.1.0",
        sensor_snapshot=SimpleNamespace(
            phase="ready", metrics={"CO2": 800, "Temperature": 24.5}
        ),
        switch_snapshot={"SWITCH_1": {"phase": "ready", "state": True}},
    )

    assert result.phase == "published"
    assert "homeassistant/sensor/co2_ykdvea/co2/config" in result.topics
    assert "homeassistant/sensor/co2_ykdvea/temperature/config" in result.topics
    assert "homeassistant/switch/co2_ykdvea/s1_ykdvea/config" in result.topics
    discovery_messages = {
        message.topic: message
        for message in transport.published_messages
        if message.topic.startswith("homeassistant/")
    }
    assert (
        discovery_messages["homeassistant/sensor/co2_ykdvea/co2/config"].retain is True
    )
    assert (
        discovery_messages["homeassistant/sensor/co2_ykdvea/co2/config"].payload[
            "state_topic"
        ]
        == "nodus/co2-ykdvea/data"
    )
    assert discovery_messages["homeassistant/sensor/co2_ykdvea/co2/config"].payload[
        "availability_template"
    ] == ("{{ value_json['status'] }}")
    assert (
        discovery_messages["homeassistant/switch/co2_ykdvea/s1_ykdvea/config"].payload[
            "command_topic"
        ]
        == "nodus/S1-ykdvea/config/set"
    )
    assert discovery_messages[
        "homeassistant/switch/co2_ykdvea/s1_ykdvea/config"
    ].payload["availability_template"] == ("{{ value_json['status'] }}")


def test_homeassistant_startup_cycle_clears_stale_discovery_topics():
    transport = MQTTTransport("broker.local", 1883)
    transport._ha_last_retained_discovery_topics = {
        "homeassistant/sensor/co2_ykdvea/old_metric/config"
    }
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

    result = publish_startup_cycle(
        transport,
        runtime_config,
        version="0.1.0",
        sensor_snapshot=SimpleNamespace(phase="ready", metrics={"CO2": 800}),
        switch_snapshot={},
    )

    assert "homeassistant/sensor/co2_ykdvea/old_metric/config" in result.topics
    stale_message = next(
        message
        for message in transport.published_messages
        if message.topic == "homeassistant/sensor/co2_ykdvea/old_metric/config"
    )
    assert stale_message.payload == ""
    assert stale_message.retain is True
