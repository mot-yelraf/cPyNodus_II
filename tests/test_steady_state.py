"""Tests for one-pass steady-state runtime coordination.

The cases verify task ordering, service polling, publish decisions, and
recovery signals for a single coordinator pass.
"""

from pathlib import Path
from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.steady_state import SteadyState, run_steady_state_iteration
from cpynodus_ii.ota import FwUpdateState, save_ota_state


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
    assert result.startup_published_count == 7
    assert len(result.command_results) == 1
    assert result.command_results[0].phase == "published"
    assert result.command_published_count == 5
    assert result.sensor_publish_phase == "published"
    assert result.sensor_published_count == 1
    assert result.availability_refresh_phase == "skipped"
    assert result.availability_refresh_published_count == 0
    assert result.total_published_count == 13
    assert transport.published_messages[-1].topic == "nodus/aqi-x943fm/data"


def test_steady_state_iteration_skips_sensor_cycle_when_polling_disabled():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig()

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
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
    assert result.availability_refresh_phase == "skipped"
    assert result.availability_refresh_published_count == 0
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
    Path(tmp_path / "onboarding_state.json").write_text(
        '{"onboard_token":"token-123"}', encoding="utf-8"
    )

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
        None,
        state=SteadyState(sensor_interval_s=60.0),
        version="v0.26.114.1",
        now_monotonic=10.0,
        ip_address="10.0.0.44",
        settings_root=str(tmp_path),
    )

    assert result.startup_publish_phase == "published"
    assert result.startup_published_count == 4
    assert result.availability_refresh_phase == "skipped"
    assert result.availability_refresh_published_count == 0
    meta_message = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/aqi-x943fm/meta"
    )
    assert meta_message.payload["network"]["ipv4addr"] == "10.0.0.44"
    hello_message = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/aqi-x943fm/onboard/hello"
    )
    assert hello_message.payload["onboard_token"] == "token-123"


def test_steady_state_iteration_reports_applied_ota_on_startup_publish(tmp_path):
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
    )
    save_ota_state(
        FwUpdateState(
            prior_profile="homeassistant",
            package_id="ota-tagA-to-tagB",
            phase="applied",
        ),
        str(tmp_path / "_ota" / "state.json"),
    )

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
        None,
        state=SteadyState(sensor_interval_s=60.0),
        version="v0.26.123.12",
        now_monotonic=10.0,
        settings_root=str(tmp_path),
    )

    assert result.startup_publish_phase == "published"
    assert result.ota_status_phase == "published"
    assert result.ota_status_published_count == 1
    ota_message = next(
        message
        for message in transport.published_messages
        if message.topic == "nodus/aqi-x943fm/fwupdate/result"
    )
    assert ota_message.retain is False
    assert ota_message.payload["schema"] == "nodus-fwupdate-result/v1"
    assert ota_message.payload["prepared"] is True
    assert ota_message.payload["applied"] is True
    assert ota_message.payload["phase"] == "applied"
    assert ota_message.payload["package_id"] == "ota-tagA-to-tagB"
    assert ota_message.payload["prior_profile"] == "homeassistant"


def test_steady_state_iteration_skips_ota_report_after_initial_connection(tmp_path):
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
    )
    save_ota_state(
        FwUpdateState(package_id="ota-tagA-to-tagB", phase="applied"),
        str(tmp_path / "_ota" / "state.json"),
    )

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
        None,
        state=SteadyState(sensor_interval_s=60.0),
        version="v0.26.123.12",
        now_monotonic=10.0,
        settings_root=str(tmp_path),
    )
    published_after_first = len(transport.published_messages)
    second = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
        None,
        state=first.state,
        version="v0.26.123.12",
        now_monotonic=11.0,
        settings_root=str(tmp_path),
    )

    assert first.ota_status_phase == "published"
    assert second.ota_status_phase == "skipped"
    assert second.ota_status_published_count == 0
    assert len(transport.published_messages) == published_after_first


def test_steady_state_startup_snapshot_defers_bme280_data_publish():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="avpd",
            sensor_id="avpd-x943fm",
        ),
    )
    sensor_service = SimpleNamespace(
        phase="deferred",
        driver_kind="adafruit_bme280",
        driver=None,
        errors=("sensor_driver_start_deferred",),
    )

    result = run_steady_state_iteration(
        transport,
        runtime_config,
        SimpleNamespace(
            phase="inactive", device_id="", channel_count=0, channels=(), errors=()
        ),
        sensor_service,
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
    )

    assert result.startup_publish_phase == "published"
    assert result.sensor_publish_phase == "skipped"
    assert "nodus/avpd-x943fm/data" not in [
        message.topic for message in transport.published_messages
    ]
