from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import (
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
    sensor_snapshot = SimpleNamespace(phase="ready", metrics={"temperature_c": 24.5})
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
    sensor_snapshot = SimpleNamespace(phase="ready", metrics={"temperature_c": 24.5, "air_quality_aqi": 80})

    result = publish_sensor_cycle(transport, runtime_config, sensor_snapshot)

    assert result.phase == "published"
    assert result.published_count == 1
    assert result.topics == ("nodus/aqi-x943fm/data",)
    assert transport.published_messages[-1].retain is False
    assert transport.published_messages[-1].payload["metrics"]["air_quality_aqi"] == 80


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

    result = publish_switch_result(transport, runtime_config, apply_result, message_id="cfg-1")

    assert result.phase == "published"
    assert result.published_count == 2
    assert result.topics == ("nodus/S1-x943fm/config/result", "nodus/S1-x943fm/state")
    assert transport.published_messages[-1].retain is True
    assert transport.published_messages[0].payload == {
        "message_id": "cfg-1",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[-1].payload == "ON"


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
