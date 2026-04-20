from types import SimpleNamespace

from cpynodus_ii.core.config import RuntimeConfig, SwitchChannelConfig, SwitchConfig
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features import (
    parse_calibration_command,
    parse_device_config_command,
    parse_switch_command,
    process_calibration_message,
    process_device_config_message,
    process_inbound_messages,
    process_switch_command_message,
    subscribe_runtime_topics,
)


def _runtime_config():
    return RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
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
                    label="Light",
                    enable_pin="GP6",
                    control_pin="GP27",
                ),
            ),
        )
    )


def _switch_service():
    class _Handle:
        def __init__(self, value=False):
            self.value = value

    return SimpleNamespace(
        phase="ready",
        device_id="switch-x943fm",
        channel_count=2,
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
            SimpleNamespace(
                key="SWITCH_2",
                channel_id="S2-x943fm",
                phase="ready",
                control_handle=_Handle(True),
                enable_handle=_Handle(True),
                errors=(),
            ),
        ),
    )


def test_subscribe_runtime_topics_tracks_all_switch_channels():
    transport = MQTTTransport("broker.local", 1883)

    topics = subscribe_runtime_topics(transport, _runtime_config())

    assert topics == (
        "nodus/switch-x943fm/config/set",
        "nodus/switch-x943fm/calibration/set",
        "nodus/S1-x943fm/config/set",
        "nodus/S2-x943fm/config/set",
    )
    assert transport.subscriptions == list(topics)


def test_parse_switch_command_accepts_compact_and_json_payloads():
    command = parse_switch_command("nodus/S1-x943fm/config/set", "ON")
    json_command = parse_switch_command(
        "nodus/S2-x943fm/config/set",
        '{"state":"off","message_id":"abc-123"}',
    )

    assert command.channel_id == "S1-x943fm"
    assert command.desired_state is True
    assert json_command.channel_id == "S2-x943fm"
    assert json_command.desired_state is False
    assert json_command.message_id == "abc-123"


def test_parse_switch_command_accepts_device_style_updates_payload_on_channel_topic():
    command = parse_switch_command(
        "nodus/S1-x943fm/config/set",
        '{"message_id":"cfg-1","payload":{"updates":[{"section":"Switch","key":"SWITCH_1_LAST_STATE","value":true,"name":"switch.toml"}]}}',
        runtime_config=_runtime_config(),
    )

    assert command.channel_id == "S1-x943fm"
    assert command.desired_state is True
    assert command.message_id == "cfg-1"


def test_process_switch_command_message_applies_state_and_publishes_result():
    transport = MQTTTransport("broker.local", 1883)
    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text="ON",
    )

    assert result.phase == "published"
    assert result.published_count == 5
    assert [message.topic for message in transport.published_messages] == [
        "nodus/S1-x943fm/config/ack",
        "nodus/S1-x943fm/config/result",
        "nodus/S1-x943fm/state",
        "nodus/switch-x943fm/meta/patch",
        "nodus/S1-x943fm/config/set",
    ]
    assert transport.published_messages[0].payload["message_id"] == ""
    assert transport.published_messages[2].payload == "ON"
    assert transport.published_messages[3].payload["source"] == "switch_set"
    assert transport.published_messages[4].payload == ""


def test_process_switch_command_message_accepts_device_style_updates_payload():
    transport = MQTTTransport("broker.local", 1883)
    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Switch","key":"SWITCH_1_LAST_STATE","value":true,"name":"switch.toml"}]}}',
    )

    assert result.phase == "published"
    assert result.message_id == "cfg-1"
    assert transport.published_messages[0].payload["message_id"] == "cfg-1"
    assert transport.published_messages[2].payload == "ON"
    assert transport.published_messages[3].payload["updates"][0]["key"] == "SWITCH_1_LAST_STATE"


def test_process_inbound_messages_drains_queue_and_ignores_non_command_topics():
    transport = MQTTTransport("broker.local", 1883)
    transport.receive("nodus/S1-x943fm/config/set", '{"payload":{"state":"OFF"}}')
    transport.receive("nodus/S1-x943fm/state", '{"state":"ON"}')

    results = process_inbound_messages(
        transport,
        _runtime_config(),
        _switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert transport.received_messages == []


def test_process_switch_command_message_rejects_invalid_payload():
    transport = MQTTTransport("broker.local", 1883)

    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text='{"unexpected":true}',
    )

    assert result.phase == "error"
    assert result.published_count == 0
    assert "invalid_switch_command" in result.errors


def test_process_switch_command_message_ignores_empty_retained_clear():
    transport = MQTTTransport("broker.local", 1883)

    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text="",
    )

    assert result.phase == "ignored"
    assert result.published_count == 0
    assert result.errors == ()


def test_parse_device_config_command_accepts_updates_and_settings_shapes():
    updates_command = parse_device_config_command(
        '{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"aqi-new"}]}}'
    )
    settings_command = parse_device_config_command(
        '{"message_id":"cfg-2","payload":{"settings":{"MQTT":{"BROKER":"broker2.local"}}}}'
    )

    assert updates_command.message_id == "cfg-1"
    assert updates_command.updates[0]["section"] == "Network"
    assert settings_command.updates[0]["key"] == "BROKER"


def test_process_device_config_message_applies_runtime_update_and_publishes_patch():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"switch-new"}]}}',
    )

    assert result.phase == "published"
    assert result.published_count == 3
    assert result.runtime_config.network.hostname == "switch-new"
    assert transport.published_messages[0].topic == "nodus/switch-x943fm/config/ack"
    assert transport.published_messages[1].topic == "nodus/switch-x943fm/config/result"
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"


def test_process_device_config_message_replays_duplicate_message_id_idempotently():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text='{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"switch-new"}]}}',
        duplicate_message_ids=("cfg-1",),
    )

    assert result.phase == "published"
    assert result.duplicate is True
    assert result.published_count == 2
    assert transport.published_messages[-1].payload["duplicate"] is True


def test_parse_calibration_command_accepts_apply_payload():
    command = parse_calibration_command(
        '{"message_id":"cal-1","action":"apply","payload":{"calibration":{"system":{"RH_OFFSET":-0.5}}}}'
    )

    assert command.message_id == "cal-1"
    assert command.action == "apply"
    assert command.updates[0]["section"] == "Calibration.System"
    assert command.updates[0]["key"] == "RH_OFFSET"


def test_process_calibration_message_publishes_result_and_meta_patch():
    transport = MQTTTransport("broker.local", 1883)

    result = process_calibration_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/calibration/set",
        payload_text='{"message_id":"cal-1","action":"apply","payload":{"offsets":[{"key":"Calibration.Device.TEMP_OFFSET","value":1.5}]}}',
    )

    assert result.phase == "published"
    assert result.published_count == 3
    assert transport.published_messages[0].topic == "nodus/switch-x943fm/calibration/ack"
    assert transport.published_messages[1].topic == "nodus/switch-x943fm/calibration/result"
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"


def test_process_inbound_messages_handles_device_topics_before_switch_topics():
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/switch-x943fm/config/set",
        '{"message_id":"cfg-1","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"switch-new"}]}}',
    )
    transport.receive("nodus/S1-x943fm/config/set", '{"payload":{"state":"OFF"}}')

    results = process_inbound_messages(
        transport,
        _runtime_config(),
        _switch_service(),
    )

    assert len(results) == 2
    assert results[0].command_type == "config"
    assert results[0].runtime_config.network.hostname == "switch-new"
    assert results[1].command_type == "switch"
