"""Tests for inbound command parsing and runtime settings updates."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.config import (
    DetectedSensor,
    MQTTConfig,
    RuntimeConfig,
    SensorCalibration,
    SoilModbusConfig,
    SoilRegisterMap,
    SoilScaleMap,
    SoilStressConfig,
    SoilThresholdConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.command_intake import (
    parse_calibration_command,
    parse_device_config_command,
    parse_fwupdate_command,
    parse_switch_command,
    process_calibration_message,
    process_device_config_message,
    process_fwupdate_message,
    process_inbound_messages,
    process_soil_calibration_session,
    process_switch_command_message,
    subscribe_runtime_topics,
)
from cpynodus_ii.features.log_transfer import (
    parse_log_transfer_command,
    process_log_transfer_message,
    process_log_transfer_session,
)
from cpynodus_ii.features.web_services import save_onboarding_state
from cpynodus_ii.ota import load_ota_state


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


def _soil_runtime_config():
    return RuntimeConfig(
        sensor=DetectedSensor(
            family="soil",
            interface="modbus_rs485",
            active_config_file="sensor_soil.toml",
            device="soil",
            sensor_id="soil-abc123",
            serial_number="abc123",
            location="Bed A",
            modbus=SoilModbusConfig(
                uart_tx="GP4", uart_rx="GP5", baud=4800, timeout_s=0.3, address=1
            ),
            soil_registers=SoilRegisterMap(),
            soil_scales=SoilScaleMap(),
            soil_thresholds=SoilThresholdConfig(),
            soil_stress=SoilStressConfig(),
            calibration_system=SensorCalibration(),
            calibration_device=SensorCalibration(soil_ph_cal_val=0.0),
        ),
        switch=SwitchConfig(),
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
        "nodus/switch-x943fm/fwupdate",
        "nodus/switch-x943fm/logs/get",
        "nodus/S1-x943fm/config/set",
        "nodus/S2-x943fm/config/set",
    )
    assert transport.subscriptions == list(topics)


def test_subscribe_runtime_topics_uses_configured_base_topic():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        mqtt=MQTTConfig(base_topic="greenhouse"),
        switch=_runtime_config().switch,
    )

    topics = subscribe_runtime_topics(transport, runtime_config)

    assert topics == (
        "greenhouse/switch-x943fm/config/set",
        "greenhouse/switch-x943fm/calibration/set",
        "greenhouse/switch-x943fm/fwupdate",
        "greenhouse/switch-x943fm/logs/get",
        "greenhouse/S1-x943fm/config/set",
        "greenhouse/S2-x943fm/config/set",
    )


def test_parse_log_transfer_command_accepts_filename_and_chunk_size():
    command = parse_log_transfer_command(
        '{"message_id":"log-1","filename":"_reboot.log","chunk_size":512}'
    )

    assert command.message_id == "log-1"
    assert command.filename == "_reboot.log"
    assert command.chunk_size == 512


def test_process_log_transfer_message_starts_session_and_publishes_ack(tmp_path):
    (tmp_path / "_reboot.log").write_text("boot one\nboot two\n", encoding="utf-8")
    transport = MQTTTransport("broker.local", 1883)

    result = process_log_transfer_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/logs/get",
        payload_text='{"message_id":"log-1","filename":"_reboot.log","chunk_size":8}',
        settings_root=tmp_path,
    )

    assert result.phase == "published"
    assert result.command_type == "logs"
    assert result.message_id == "log-1"
    assert transport.published_messages[0].topic == "nodus/switch-x943fm/logs/ack"
    assert transport.published_messages[0].payload["accepted"] is True
    assert transport.published_messages[0].payload["size"] == 18


def test_process_log_transfer_session_publishes_chunks_then_result(tmp_path):
    (tmp_path / "_reboot.log").write_bytes(b"abcdefghijkl")
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connected()
    process_log_transfer_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/logs/get",
        payload_text='{"message_id":"log-1","filename":"_reboot.log","chunk_size":5}',
        settings_root=tmp_path,
    )

    first = process_log_transfer_session(transport, _runtime_config())
    second = process_log_transfer_session(transport, _runtime_config())
    third = process_log_transfer_session(transport, _runtime_config())
    done = process_log_transfer_session(transport, _runtime_config())

    assert first.phase == "published"
    assert second.phase == "published"
    assert third.phase == "published"
    assert done.phase == "published"
    topics = [message.topic for message in transport.published_messages]
    assert topics == [
        "nodus/switch-x943fm/logs/ack",
        "nodus/switch-x943fm/logs/chunk",
        "nodus/switch-x943fm/logs/chunk",
        "nodus/switch-x943fm/logs/chunk",
        "nodus/switch-x943fm/logs/result",
    ]
    assert transport.published_messages[1].payload["offset"] == 0
    assert transport.published_messages[1].payload["next_offset"] == 5
    assert transport.published_messages[-1].payload["complete"] is True
    assert transport.published_messages[-1].payload["chunks"] == 3


def test_parse_fwupdate_command_accepts_prepare_payload():
    command = parse_fwupdate_command(
        '{"message_id":"fw-1","command":"prepare","package_id":"ota-tagA-to-tagB"}'
    )

    assert command.message_id == "fw-1"
    assert command.command == "prepare"
    assert command.package_id == "ota-tagA-to-tagB"


def test_process_fwupdate_message_persists_prepare_state(tmp_path):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _runtime_config()

    result = process_fwupdate_message(
        transport,
        runtime_config,
        topic="nodus/switch-x943fm/fwupdate",
        payload_text=(
            '{"message_id":"fw-1","command":"prepare",'
            '"package_id":"ota-tagA-to-tagB"}'
        ),
        settings_root=tmp_path,
    )

    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert result.phase == "published"
    assert result.command_type == "fwupdate"
    assert result.message_id == "fw-1"
    assert result.persistence_mode == "persisted"
    assert result.reboot_requested is True
    assert state.prior_profile == runtime_config.active_profile
    assert state.package_id == "ota-tagA-to-tagB"
    assert state.phase == "requested"
    assert [message.topic for message in transport.published_messages] == [
        "nodus/switch-x943fm/fwupdate/ack",
        "nodus/switch-x943fm/fwupdate/result",
    ]
    assert transport.published_messages[0].payload["accepted"] is True
    assert transport.published_messages[1].payload["prepared"] is True


def test_process_fwupdate_message_rejects_missing_writable_root():
    transport = MQTTTransport("broker.local", 1883)

    result = process_fwupdate_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/fwupdate",
        payload_text=(
            '{"message_id":"fw-1","command":"prepare",'
            '"package_id":"ota-tagA-to-tagB"}'
        ),
        settings_root=None,
    )

    assert result.phase == "error"
    assert result.errors == ("read_only_filesystem",)
    assert transport.published_messages[1].payload["prepared"] is False
    assert transport.published_messages[1].payload["error"] == "read_only_filesystem"


def test_process_inbound_messages_handles_fwupdate_prepare(tmp_path):
    transport = MQTTTransport("broker.local", 1883)
    transport.receive(
        "nodus/switch-x943fm/fwupdate",
        '{"message_id":"fw-1","command":"prepare","package_id":"ota-tagA-to-tagB"}',
    )

    results = process_inbound_messages(
        transport,
        _runtime_config(),
        _switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].command_type == "fwupdate"
    assert results[0].phase == "published"
    assert load_ota_state(str(tmp_path / "_ota" / "state.json")).package_id == (
        "ota-tagA-to-tagB"
    )


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
        "nodus/S1-x943fm/event",
        "nodus/S1-x943fm/state",
        "nodus/switch-x943fm/meta/patch",
    ]
    assert transport.published_messages[0].payload["message_id"] == ""
    assert transport.published_messages[2].payload["schema"] == "nodus-switch-event/v1"
    assert transport.published_messages[2].payload["state"] == "ON"
    assert transport.published_messages[3].payload == "ON"
    assert transport.published_messages[4].payload["source"] == "switch_set"


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
    assert transport.published_messages[2].payload["state"] == "ON"
    assert transport.published_messages[3].payload == "ON"
    assert (
        transport.published_messages[4].payload["updates"][0]["key"]
        == "SWITCH_1_LAST_STATE"
    )


def test_process_switch_command_message_uses_configured_base_topic():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = RuntimeConfig(
        mqtt=MQTTConfig(base_topic="greenhouse"),
        switch=_runtime_config().switch,
    )

    result = process_switch_command_message(
        transport,
        runtime_config,
        _switch_service(),
        topic="greenhouse/S1-x943fm/config/set",
        payload_text="ON",
    )

    assert result.phase == "published"
    assert [message.topic for message in transport.published_messages] == [
        "greenhouse/S1-x943fm/config/ack",
        "greenhouse/S1-x943fm/config/result",
        "greenhouse/S1-x943fm/event",
        "greenhouse/S1-x943fm/state",
        "greenhouse/switch-x943fm/meta/patch",
    ]


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


def test_process_device_config_message_rejects_onboarding_token_mismatch():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        save_onboarding_state(tmpdir, {"onboard_token": "expected"})
        result = process_device_config_message(
            transport,
            _runtime_config(),
            topic="nodus/switch-x943fm/config/set",
            payload_text='{"message_id":"cfg-1","onboard_token":"wrong","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"new"}]}}',
            settings_root=tmpdir,
        )

    assert result.phase == "error"
    assert result.errors == ("onboard_token_invalid",)
    assert transport.published_messages[0].payload["accepted"] is False
    assert transport.published_messages[1].payload["error"] == "onboard_token_invalid"


def test_process_device_config_message_clears_onboarding_state_after_success():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "settings.toml").write_text(
            '[Network]\nHOSTNAME = "old"\n', encoding="utf-8"
        )
        save_onboarding_state(tmpdir, {"onboard_token": "expected"})
        result = process_device_config_message(
            transport,
            _runtime_config(),
            topic="nodus/switch-x943fm/config/set",
            payload_text='{"message_id":"cfg-1","onboard_token":"expected","payload":{"updates":[{"section":"Network","key":"HOSTNAME","value":"new-host"}]}}',
            settings_root=tmpdir,
        )

    assert result.phase == "published"
    assert (root / "onboarding_state.json").exists() is False


def test_process_calibration_message_starts_soil_ph_session():
    transport = MQTTTransport("broker.local", 1883)
    result = process_calibration_message(
        transport,
        _soil_runtime_config(),
        topic="nodus/soil-abc123/calibration/set",
        payload_text='{"message_id":"soil-1","action":"soil_ph_session_start","payload":{"reference_ph":7.0,"sample_interval_s":5,"sample_count":6}}',
    )

    assert result.phase == "published"
    assert (
        transport.published_messages[1].topic
        == "nodus/soil-abc123/event/calibration_status"
    )
    assert transport.published_messages[2].payload["started"] is True


def test_process_soil_calibration_session_samples_and_completes():
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connected()
    process_calibration_message(
        transport,
        _soil_runtime_config(),
        topic="nodus/soil-abc123/calibration/set",
        payload_text='{"message_id":"soil-1","action":"soil_ph_session_start","payload":{"reference_ph":7.0,"sample_interval_s":0.1,"sample_count":6}}',
    )
    sensor_service = SimpleNamespace(
        phase="ready",
        device="soil",
        interface="modbus_rs485",
        driver_kind="fake",
        driver=None,
        transport=SimpleNamespace(
            read_registers=lambda reg, count: {
                0: 250,
                1: 200,
                2: 100,
                3: 65,
                4: 1,
                5: 2,
                6: 3,
            }[reg]
        ),
        errors=(),
    )
    runtime_config = _soil_runtime_config()
    published_result = None

    for step in range(8):
        result = process_soil_calibration_session(
            transport,
            runtime_config,
            sensor_service,
            now_monotonic=float(step) * 0.1,
        )
        if result.runtime_config is not None:
            runtime_config = result.runtime_config
        if result.phase != "ignored":
            published_result = result

    topics = [message.topic for message in transport.published_messages]
    assert published_result is not None
    assert published_result.phase == "published"
    assert "nodus/soil-abc123/event/calibration_sample" in topics
    assert "nodus/soil-abc123/event/calibration_progress" in topics
    assert "nodus/soil-abc123/event/calibration_result" in topics
    assert (
        transport.published_messages[-2].topic == "nodus/soil-abc123/calibration/result"
    )
    assert runtime_config.sensor.calibration_device.soil_ph_cal_val != 0.0


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


def test_process_device_config_message_ignores_empty_retained_clear():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text="",
    )

    assert result.phase == "ignored"
    assert result.published_count == 0
    assert result.errors == ()


def test_process_calibration_message_ignores_empty_retained_clear():
    transport = MQTTTransport("broker.local", 1883)

    result = process_calibration_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/calibration/set",
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
    assert (
        transport.published_messages[0].topic == "nodus/switch-x943fm/calibration/ack"
    )
    assert (
        transport.published_messages[1].topic
        == "nodus/switch-x943fm/calibration/result"
    )
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"


def test_process_calibration_message_updates_runtime_calibration_offsets():
    transport = MQTTTransport("broker.local", 1883)

    result = process_calibration_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/calibration/set",
        payload_text='{"message_id":"cal-2","action":"apply","payload":{"offsets":[{"key":"Calibration.System.CO2_OFFSET","value":-400.0},{"key":"Calibration.Device.TEMP_OFFSET","value":1.5}]}}',
    )

    assert result.phase == "published"
    assert result.runtime_config.sensor.calibration_system.co2_offset == -400.0
    assert result.runtime_config.sensor.calibration_device.temp_offset == 1.5


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
