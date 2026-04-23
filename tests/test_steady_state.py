from types import SimpleNamespace
from pathlib import Path

from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig, SwitchChannelConfig, SwitchConfig
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import SteadyState, run_steady_state_iteration


def _ready_switch_service():
    class _Handle:
        def __init__(self, value=False):
            self.value = value

    return SimpleNamespace(
        phase="ready",
        device_id="switch-x943fm",
        channel_count=1,
        errors=(),
        channels=(
            SimpleNamespace(
                key="SWITCH_1",
                channel_id="S1-x943fm",
                phase="ready",
                control_handle=_Handle(False),
                enable_handle=_Handle(True),
                errors=(),
            ),
        ),
    )


def test_steady_state_iteration_processes_commands_and_publishes_sensor_cycle():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = RuntimeConfig(
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
    sensor_service = SimpleNamespace(
        phase="ready",
        sensor_id="aqi-x943fm",
        family="i2c",
        device="aqi",
        driver_kind="fake",
        driver=SimpleNamespace(
            temperature=24.5,
            humidity=55.0,
            pressure=100850.0,
            gas=12345.0,
        ),
        transport=None,
        errors=(),
    )
    switch_service = _ready_switch_service()
    transport.receive("nodus/S1-x943fm/config/set", "ON")

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        switch_service,
        sensor_service,
        state=SteadyState(sensor_interval_s=0.0),
        version="0.1.0",
        now_monotonic=10.0,
    )

    assert result.startup_publish_phase == "published"
    assert result.startup_published_count == 6
    assert len(result.command_results) == 1
    assert result.command_results[0].phase == "published"
    assert result.command_published_count == 5
    assert result.sensor_publish_phase == "published"
    assert result.sensor_published_count == 1
    assert result.total_published_count == 12
    assert transport.published_messages[-1].topic == "nodus/aqi-x943fm/data"


def test_steady_state_iteration_skips_sensor_cycle_when_polling_disabled():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig()

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(phase="inactive", device_id="", channel_count=0, channels=(), errors=()),
        None,
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
    )

    assert result.command_results == ()
    assert result.startup_publish_phase == "skipped"
    assert result.startup_published_count == 0
    assert result.command_published_count == 0
    assert result.sensor_publish_phase == "skipped"
    assert result.sensor_published_count == 0
    assert result.total_published_count == 0


def test_steady_state_iteration_loads_onboarding_state_for_startup_publish(tmp_path):
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
            serial_number="x943fm",
        ),
    )
    Path(tmp_path / "onboarding_state.json").write_text('{"onboard_token":"token-123"}', encoding="utf-8")

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(phase="inactive", device_id="", channel_count=0, channels=(), errors=()),
        None,
        state=SteadyState(sensor_interval_s=60.0),
        version="v0.26.114.1",
        now_monotonic=10.0,
        settings_root=str(tmp_path),
    )

    assert result.startup_publish_phase == "published"
    assert result.startup_published_count == 4
    hello_message = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/aqi-x943fm/onboard/hello"
    )
    assert hello_message.payload["onboard_token"] == "token-123"
