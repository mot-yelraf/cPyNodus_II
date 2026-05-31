"""Tests for steady-state behavior across MQTT transport conditions."""

from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.steady_state import (
    DEFAULT_AVAILABILITY_INTERVAL_S,
    SteadyState,
    run_steady_state_iteration,
)


def _switch_service():
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


def _sensor_service():
    return SimpleNamespace(
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


def _runtime_config():
    return RuntimeConfig(
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


def _switch_only_runtime_config():
    config = _runtime_config()
    return RuntimeConfig(switch=config.switch)


def test_transport_tracks_connection_generations():
    transport = MQTTTransport("broker.local", 1883)

    assert transport.connected is False
    assert transport.connection_generation == 0
    assert transport.mark_connected(now_monotonic=10.0) == 1
    assert transport.connected is True
    assert transport.connection_generation == 1
    assert transport.last_connected_at == 10.0
    assert transport.last_success_at == 10.0
    transport.mark_success(now_monotonic=12.0)
    assert transport.last_success_at == 12.0
    transport.mark_disconnected(now_monotonic=13.0)
    assert transport.connected is False
    assert transport.last_disconnected_at == 13.0
    assert transport.mark_connected(now_monotonic=14.0) == 2
    assert transport.connection_generation == 2
    assert transport.last_success_at == 14.0


def test_steady_state_resubscribes_and_republishes_on_connect_generation_change():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()

    result = run_steady_state_iteration(
        transport,
        _runtime_config(),
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
    )

    assert result.startup_publish_phase == "published"
    assert result.startup_published_count == 7
    assert result.subscribed_topics == (
        "nodus/aqi-x943fm/config/set",
        "nodus/aqi-x943fm/calibration/set",
        "nodus/aqi-x943fm/fwupdate",
        "nodus/aqi-x943fm/logs/get",
        "nodus/S1-x943fm/config/set",
    )
    assert result.state.connection_generation == 1
    assert result.state.switch_meta_generation == 1
    assert result.state.last_sensor_publish_at == 10.0
    assert result.state.last_availability_publish_at == 10.0
    assert transport.subscriptions == list(result.subscribed_topics)
    assert "nodus/aqi-x943fm/meta/switch" in [
        message.topic for message in transport.published_messages
    ]


def test_steady_state_does_not_duplicate_switch_meta_after_startup_queues_drain():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    transport.subscriptions.clear()
    transport.published_messages.clear()

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=first.state,
        version="0.1.0",
        now_monotonic=11.0,
    )

    assert transport.published_messages == []
    assert second.state.switch_meta_generation == transport.connection_generation

    run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=second.state,
        version="0.1.0",
        now_monotonic=12.0,
    )

    assert transport.published_messages == []


def test_steady_state_can_defer_switch_subscriptions_on_connect():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()

    result = run_steady_state_iteration(
        transport,
        _runtime_config(),
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
        subscribe_switch_topics=False,
    )

    assert result.subscribed_topics == (
        "nodus/aqi-x943fm/config/set",
        "nodus/aqi-x943fm/calibration/set",
        "nodus/aqi-x943fm/fwupdate",
        "nodus/aqi-x943fm/logs/get",
    )
    assert transport.subscriptions == list(result.subscribed_topics)


def test_steady_state_can_skip_switch_retained_startup_topics_on_connect():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()

    result = run_steady_state_iteration(
        transport,
        _runtime_config(),
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
        publish_switch_startup=False,
    )

    assert result.startup_publish_phase == "published"
    assert "nodus/aqi-x943fm/meta" in [
        message.topic for message in transport.published_messages
    ]
    assert "nodus/S1-x943fm/availability" not in [
        message.topic for message in transport.published_messages
    ]
    assert "nodus/S1-x943fm/state" not in [
        message.topic for message in transport.published_messages
    ]


def test_steady_state_can_publish_reduced_switch_meta_on_connect():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()

    run_steady_state_iteration(
        transport,
        _runtime_config(),
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=60.0),
        version="0.1.0",
        now_monotonic=10.0,
        include_switch_meta_channels=False,
    )

    meta = transport.published_messages[0].payload
    assert meta["capabilities"]["switch"] is True
    assert meta["switch"]["channel_count"] == 1
    assert "channels" not in meta["switch"]


def test_steady_state_respects_sensor_publish_interval():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=30.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    published_after_first = len(transport.published_messages)

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=first.state,
        version="0.1.0",
        now_monotonic=20.0,
    )

    assert second.startup_publish_phase == "skipped"
    assert second.sensor_publish_phase == "skipped"
    assert second.sensor_published_count == 0
    assert len(transport.published_messages) == published_after_first

    third = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=second.state,
        version="0.1.0",
        now_monotonic=41.0,
    )

    assert third.sensor_publish_phase == "published"
    assert third.sensor_published_count == 1
    assert third.state.last_sensor_publish_at == 41.0


def test_steady_state_refreshes_availability_on_interval():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=300.0, availability_interval_s=15.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    published_after_first = len(transport.published_messages)

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=first.state,
        version="0.1.0",
        now_monotonic=20.0,
    )
    assert second.availability_refresh_phase == "skipped"
    assert second.availability_refresh_published_count == 0
    assert len(transport.published_messages) == published_after_first

    third = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=second.state,
        version="0.1.0",
        now_monotonic=26.0,
    )
    assert third.availability_refresh_phase == "published"
    assert third.availability_refresh_published_count == 3
    assert third.state.last_availability_publish_at == 26.0


def test_default_availability_refresh_interval_is_debug_30_seconds():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=300.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    transport.subscriptions.clear()
    transport.published_messages.clear()

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=first.state,
        version="0.1.0",
        now_monotonic=10.0 + DEFAULT_AVAILABILITY_INTERVAL_S - 1.0,
    )
    assert second.availability_refresh_phase == "skipped"
    assert transport.published_messages == []

    third = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=second.state,
        version="0.1.0",
        now_monotonic=10.0 + DEFAULT_AVAILABILITY_INTERVAL_S,
    )
    assert third.availability_refresh_phase == "published"
    assert third.availability_refresh_published_count == 3


def test_switch_only_steady_state_refreshes_availability_before_idle_gap():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _switch_only_runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        None,
        state=SteadyState(availability_interval_s=120.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    transport.subscriptions.clear()
    transport.published_messages.clear()

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        None,
        state=first.state,
        version="0.1.0",
        now_monotonic=54.0,
    )
    assert second.availability_refresh_phase == "skipped"
    assert transport.published_messages == []

    third = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        None,
        state=second.state,
        version="0.1.0",
        now_monotonic=55.0,
    )
    assert third.availability_refresh_phase == "published"
    assert third.availability_refresh_published_count == 2
    assert third.state.last_availability_publish_at == 55.0
    assert [message.topic for message in transport.published_messages] == [
        "nodus/switch-x943fm/status/heartbeat",
        "nodus/S1-x943fm/availability",
    ]


def test_sensor_steady_state_keeps_configured_availability_interval():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    transport.mark_connected()
    runtime_config = _runtime_config()

    first = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=SteadyState(sensor_interval_s=300.0, availability_interval_s=120.0),
        version="0.1.0",
        now_monotonic=10.0,
    )
    transport.subscriptions.clear()
    transport.published_messages.clear()

    second = run_steady_state_iteration(
        transport,
        runtime_config,
        _switch_service(),
        _sensor_service(),
        state=first.state,
        version="0.1.0",
        now_monotonic=55.0,
    )
    assert second.availability_refresh_phase == "skipped"
    assert transport.published_messages == []


def test_steady_state_bounds_handled_message_ids():
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/S1-x943fm/config/set",
        '{"message_id":"cfg-1","payload":{"state":"ON"}}',
    )
    result = run_steady_state_iteration(
        transport,
        _runtime_config(),
        _switch_service(),
        state=SteadyState(
            handled_message_ids=("old-1", "old-2"), handled_message_id_limit=2
        ),
        version="0.1.0",
        now_monotonic=10.0,
    )

    assert result.state.handled_message_ids == ("old-2", "cfg-1")
