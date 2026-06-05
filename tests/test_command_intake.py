"""Tests for inbound command parsing and runtime settings updates."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import cpynodus_ii.features.command_intake as command_intake
from cpynodus_ii.core.config import (
    DetectedSensor,
    MQTTConfig,
    RuntimeConfig,
    SensorCalibration,
    SoilModbusConfig,
    SoilNPKConfig,
    SoilRegisterMap,
    SoilScaleMap,
    SoilStressConfig,
    SoilThresholdConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings
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
            soil_npk=SoilNPKConfig(),
            calibration_system=SensorCalibration(),
            calibration_device=SensorCalibration(soil_ph_cal_val=0.0),
        ),
        switch=SwitchConfig(),
    )


def _sensor_switch_runtime_config():
    return RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            active_config_file="sensor_i2c.toml",
            device="co2",
            sensor_id="co2-ykdvea",
            serial_number="ykdvea",
            location="OfficeTest",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-ykdvea",
            channel_count=2,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-ykdvea",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
                SwitchChannelConfig(
                    key="SWITCH_2",
                    channel_id="S2-ykdvea",
                    label="Humidifier",
                    enable_pin="GP6",
                    control_pin="GP27",
                ),
            ),
        ),
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


def _sensor_switch_service():
    class _Handle:
        def __init__(self, value=False):
            self.value = value

    return SimpleNamespace(
        phase="ready",
        device_id="switch-ykdvea",
        channel_count=2,
        errors=(),
        channels=(
            SimpleNamespace(
                key="SWITCH_1",
                channel_id="S1-ykdvea",
                phase="ready",
                control_handle=_Handle(True),
                enable_handle=_Handle(True),
                errors=(),
            ),
            SimpleNamespace(
                key="SWITCH_2",
                channel_id="S2-ykdvea",
                phase="ready",
                control_handle=_Handle(False),
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


def test_sensor_switch_command_publishes_state_with_pending_availability():
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    switch_service = _sensor_switch_service()
    transport.publish(
        "nodus/co2-ykdvea/availability",
        {"schema": "nodus-availability/v1", "status": "online"},
        retain=True,
    )
    transport.publish(
        "nodus/S1-ykdvea/availability",
        {"schema": "nodus-availability/v1", "status": "online"},
        retain=True,
    )
    transport.publish(
        "nodus/S2-ykdvea/availability",
        {"schema": "nodus-availability/v1", "status": "online"},
        retain=True,
    )
    transport.receive(
        "nodus/S2-ykdvea/config/set",
        '{"message_id":"cfg-1","payload":{"updates":[{"section":"Switch","key":"SWITCH_2_LAST_STATE","value":true,"name":"switch.toml"}]},"restart":false}',
    )

    results = process_inbound_messages(transport, runtime_config, switch_service)

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 5
    assert switch_service.channels[1].control_handle.value is True
    assert [message.topic for message in transport.published_messages] == [
        "nodus/co2-ykdvea/availability",
        "nodus/S1-ykdvea/availability",
        "nodus/S2-ykdvea/availability",
        "nodus/S2-ykdvea/config/ack",
        "nodus/S2-ykdvea/config/result",
        "nodus/S2-ykdvea/event",
        "nodus/S2-ykdvea/state",
        "nodus/co2-ykdvea/meta/patch",
    ]
    assert transport.published_messages[6].payload == "ON"
    assert transport.published_messages[6].retain is True


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


def test_process_inbound_messages_fast_switch_location_persists():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "switch.toml").write_text(
            '[Switch]\nSWITCH_LOCATION = "TestSwitch"\n',
            encoding="utf-8",
        )
        transport.receive(
            "nodus/switch-x943fm/config/set",
            '{"message_id":"cfg-loc","payload":{"updates":[{"section":"Switch","key":"SWITCH_LOCATION","value":"Switch#1","name":"switch.toml"}]},"restart":false}',
        )

        results = process_inbound_messages(
            transport,
            _runtime_config(),
            _switch_service(),
            settings_root=tmpdir,
        )
        switch_text = (root / "switch.toml").read_text(encoding="utf-8")

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].command_type == "config"
    assert results[0].published_count == 3
    assert results[0].runtime_config.switch.location == "Switch#1"
    assert 'SWITCH_LOCATION = "Switch#1"' in switch_text
    assert transport.published_messages[0].topic == "nodus/switch-x943fm/config/ack"
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Switch",
            "key": "SWITCH_LOCATION",
            "value": "Switch#1",
        },
    ]


def test_process_inbound_messages_fast_switch_location_validates_onboarding_token():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "switch.toml").write_text(
            '[Switch]\nSWITCH_LOCATION = "TestSwitch"\n',
            encoding="utf-8",
        )
        save_onboarding_state(tmpdir, {"onboard_token": "expected"})
        transport.receive(
            "nodus/switch-x943fm/config/set",
            '{"message_id":"cfg-loc","onboard_token":"wrong","payload":{"updates":[{"section":"Switch","key":"SWITCH_LOCATION","value":"Switch#1"}]}}',
        )

        results = process_inbound_messages(
            transport,
            _runtime_config(),
            _switch_service(),
            settings_root=tmpdir,
        )
        switch_text = (root / "switch.toml").read_text(encoding="utf-8")

    assert len(results) == 1
    assert results[0].phase == "error"
    assert results[0].errors == ("onboard_token_invalid",)
    assert 'SWITCH_LOCATION = "TestSwitch"' in switch_text
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
    assert result.ntp_resync_requested is False
    assert transport.published_messages[0].topic == "nodus/switch-x943fm/config/ack"
    assert transport.published_messages[1].topic == "nodus/switch-x943fm/config/result"
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"


def test_process_device_config_message_requests_ntp_resync_for_time_update():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text=(
            '{"message_id":"cfg-time","payload":{"updates":['
            '{"section":"Time","key":"TZ_OFFSET","value":-21600}'
            "]}}"
        ),
    )

    assert result.phase == "published"
    assert result.ntp_resync_requested is True
    assert result.runtime_config.time.tz_offset == -21600
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Time",
            "key": "TZ_OFFSET",
            "value": -21600,
        },
    ]


def test_process_inbound_messages_fast_time_config_persists_and_resyncs(tmp_path):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    runtime_config.time.tz = "UTC"
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text(
        (
            '[Time]\nTZ = "UTC"\nTZ_OFFSET = -25200\nTZ_NAME = "MST"\n'
            'NTP_SERVER = ""\nNTP_SERVER_IP = "132.163.96.6"\n'
        ),
        encoding="utf-8",
    )
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-time-tz","payload":{"updates":['
            '{"section":"Time","key":"TZ","value":"America/Denver"}'
            ']},"restart":false}'
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ()
    assert results[0].persistence_mode == "persisted"
    assert results[0].ntp_resync_requested is True
    assert results[0].runtime_config.time.tz == "America/Denver"
    assert 'TZ = "America/Denver"' in settings_path.read_text(encoding="utf-8")
    assert [message.topic for message in transport.published_messages] == [
        "nodus/co2-ykdvea/config/ack",
        "nodus/co2-ykdvea/config/result",
        "nodus/co2-ykdvea/meta/patch",
    ]
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-time-tz",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Time",
            "key": "TZ",
            "value": "America/Denver",
        },
    ]


def test_process_inbound_messages_fast_time_config_pystack_is_volatile(
    monkeypatch,
    tmp_path,
):
    from cpynodus_ii.features import time_config

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    runtime_config.time.tz = "UTC"
    (tmp_path / "settings.toml").write_text(
        '[Time]\nTZ = "UTC"\n',
        encoding="utf-8",
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(time_config, "_write_time_file", raise_pystack)
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-time-stack","payload":{"updates":['
            '{"section":"Time","key":"TZ","value":"America/Denver"}'
            "]}}"
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ("time_persist_pystack",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].ntp_resync_requested is True
    assert results[0].runtime_config.time.tz == "America/Denver"
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-time-stack",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"


def test_process_device_config_message_applies_soil_npk_target_update():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _soil_runtime_config(),
        topic="nodus/soil-abc123/config/set",
        payload_text='{"message_id":"cfg-npk","payload":{"updates":[{"section":"NPK","key":"P_TARGET","value":90.0}]}}',
    )

    assert result.phase == "published"
    assert result.runtime_config.sensor.soil_npk.p_target == 90.0
    assert transport.published_messages[2].payload["updates"][0]["section"] == "NPK"


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
    assert result.ntp_resync_requested is False
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


def test_process_calibration_message_updates_runtime_altitude():
    transport = MQTTTransport("broker.local", 1883)

    result = process_calibration_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/calibration/set",
        payload_text='{"message_id":"cal-alt","action":"apply","payload":{"offsets":[{"key":"Calibration.Device.ALTITUDE_METERS","value":1609.3}]}}',
    )

    assert result.phase == "published"
    assert result.runtime_config.sensor.calibration_device.altitude_meters == 1609.3


def test_process_inbound_messages_fast_calibration_apply_persists_offsets():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (root / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = _sensor_switch_runtime_config()
        transport.receive(
            "nodus/co2-ykdvea/calibration/set",
            (
                '{"message_id":"cal-1","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.TEMP_OFFSET","value":-2.5},'
                '{"key":"Calibration.Device.ALTITUDE_METERS","value":1783.0}'
                "]}}"
            ),
        )

        results = process_inbound_messages(
            transport,
            runtime_config,
            _sensor_switch_service(),
            settings_root=tmpdir,
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].command_type == "calibration"
    assert results[0].published_count == 3
    assert results[0].runtime_config.sensor.calibration_device.temp_offset == -2.5
    assert results[0].runtime_config.sensor.calibration_device.altitude_meters == 1783.0
    assert sensor_doc["Calibration"]["Device"]["TEMP_OFFSET"] == -2.5
    assert sensor_doc["Calibration"]["Device"]["ALTITUDE_METERS"] == 1783.0
    assert transport.published_messages[0].topic == "nodus/co2-ykdvea/calibration/ack"
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 2
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"
    assert transport.published_messages[2].payload["source"] == "calibration_set"


def test_process_inbound_messages_fast_calibration_split_offsets_persist():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (root / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = _sensor_switch_runtime_config()
        transport.receive(
            "nodus/co2-ykdvea/calibration/set",
            (
                '{"message_id":"cal-co2","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
                "]}}"
            ),
        )
        transport.receive(
            "nodus/co2-ykdvea/calibration/set",
            (
                '{"message_id":"cal-alt","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.ALTITUDE_METERS","value":1783.0}'
                "]}}"
            ),
        )

        results = process_inbound_messages(
            transport,
            runtime_config,
            _sensor_switch_service(),
            settings_root=tmpdir,
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert len(results) == 2
    assert all(result.phase == "published" for result in results)
    assert all(result.persistence_mode == "persisted" for result in results)
    assert all(result.errors == () for result in results)
    assert [result.message_id for result in results] == ["cal-co2", "cal-alt"]
    calibration = results[-1].runtime_config.sensor.calibration_device
    assert calibration.co2_offset == -100.0
    assert calibration.altitude_meters == 1783.0
    assert sensor_doc["Calibration"]["Device"]["CO2_OFFSET"] == -100.0
    assert sensor_doc["Calibration"]["Device"]["ALTITUDE_METERS"] == 1783.0
    assert [message.topic for message in transport.published_messages] == [
        "nodus/co2-ykdvea/calibration/ack",
        "nodus/co2-ykdvea/calibration/result",
        "nodus/co2-ykdvea/meta/patch",
        "nodus/co2-ykdvea/calibration/ack",
        "nodus/co2-ykdvea/calibration/result",
        "nodus/co2-ykdvea/meta/patch",
    ]
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Calibration.Device",
            "key": "CO2_OFFSET",
            "value": -100.0,
        },
    ]
    assert transport.published_messages[4].payload["applied"] is True
    assert transport.published_messages[4].payload["updated"] == 1
    assert transport.published_messages[5].payload["updates"] == [
        {
            "section": "Calibration.Device",
            "key": "ALTITUDE_METERS",
            "value": 1783.0,
        },
    ]


def test_process_inbound_messages_fast_calibration_memory_error_is_contained(
    monkeypatch,
):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        (
            '{"message_id":"cal-oom","action":"apply","payload":{"offsets":['
            '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
            "]}}"
        ),
    )

    def raise_memory_error(*args, **kwargs):
        raise MemoryError("memory allocation failed")

    monkeypatch.setattr(
        command_intake,
        "_process_calibration_offsets_message",
        raise_memory_error,
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "error"
    assert results[0].command_type == "calibration"
    assert results[0].published_count == 2
    assert results[0].errors == ("calibration_offsets_memory",)
    assert results[0].message_id == "cal-oom"
    assert transport.received_messages == []
    assert transport.published_messages[0].topic == "nodus/co2-ykdvea/calibration/ack"
    assert transport.published_messages[0].payload == {
        "message_id": "cal-oom",
        "accepted": True,
    }
    assert (
        transport.published_messages[1].topic
        == "nodus/co2-ykdvea/calibration/result"
    )
    assert transport.published_messages[1].payload == {
        "message_id": "cal-oom",
        "applied": False,
        "updated": 0,
        "duplicate": False,
        "error": "calibration_offsets_memory",
    }


def test_calibration_memory_failure_publish_memory_error_is_contained(monkeypatch):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        (
            '{"message_id":"cal-pub-oom","action":"apply","payload":{"offsets":['
            '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
            "]}}"
        ),
    )

    def raise_memory_error(*args, **kwargs):
        raise MemoryError("memory allocation failed")

    monkeypatch.setattr(
        command_intake,
        "_process_calibration_offsets_message",
        raise_memory_error,
    )
    monkeypatch.setattr(transport, "publish", raise_memory_error)

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "error"
    assert results[0].published_count == 0
    assert results[0].errors == (
        "calibration_offsets_memory",
        "calibration_failure_publish_memory",
    )
    assert results[0].message_id == "cal-pub-oom"
    assert transport.received_messages == []
    assert transport.published_messages == []


def test_fast_calibration_offset_persistence_import_memory_is_reported(
    monkeypatch,
    tmp_path,
):
    import builtins

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    real_import = builtins.__import__

    def import_with_memory_error(name, *args, **kwargs):
        if name == "cpynodus_ii.features.calibration_offset_persistence":
            raise MemoryError("memory allocation failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_memory_error)
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        (
            '{"message_id":"cal-offset-persist-oom","action":"apply",'
            '"payload":{"offsets":['
            '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
            "]}}"
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ("calibration_offset_persist_import_memory",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].requested_state == "offset_fast:applied"
    assert results[0].runtime_config.sensor.calibration_device.co2_offset == -100.0
    assert transport.published_messages[1].payload == {
        "message_id": "cal-offset-persist-oom",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"


def test_fast_calibration_offset_persistence_pystack_is_nonfatal(
    monkeypatch,
    tmp_path,
):
    from cpynodus_ii.features import calibration_offset_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(
        calibration_offset_persistence,
        "persist_single_calibration_offset",
        raise_pystack,
    )
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        (
            '{"message_id":"cal-offset-persist-stack","action":"apply",'
            '"payload":{"offsets":['
            '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
            "]}}"
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ("calibration_offset_persist_pystack",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].requested_state == "offset_fast:applied"
    assert results[0].runtime_config.sensor.calibration_device.co2_offset == -100.0
    assert transport.published_messages[0].topic == "nodus/co2-ykdvea/calibration/ack"
    assert transport.published_messages[1].payload == {
        "message_id": "cal-offset-persist-stack",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"


def test_fast_calibration_persistence_import_memory_error_is_reported(
    monkeypatch,
    tmp_path,
):
    import builtins

    from cpynodus_ii.features.calibration_config import (
        process_calibration_apply_message,
    )

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    real_import = builtins.__import__

    def import_with_memory_error(name, *args, **kwargs):
        if name == "cpynodus_ii.features.calibration_persistence":
            raise MemoryError("memory allocation failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_memory_error)

    result = process_calibration_apply_message(
        transport,
        runtime_config,
        topic="nodus/co2-ykdvea/calibration/set",
        payload_text=(
            '{"message_id":"cal-persist-oom","action":"apply","payload":{"offsets":['
            '{"key":"Calibration.Device.CO2_OFFSET","value":-100.0}'
            "]}}"
        ),
        settings_root=tmp_path,
    )

    assert result.phase == "published"
    assert result.errors == ("calibration_persist_import_memory",)
    assert result.published_count == 3
    assert result.persistence_mode == "volatile"
    assert result.runtime_config.sensor.calibration_device.co2_offset == -100.0
    assert transport.published_messages[0].topic == "nodus/co2-ykdvea/calibration/ack"
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[1].payload["error"] == ""
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"


def test_process_inbound_messages_ignores_empty_calibration_clear(monkeypatch):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive("nodus/co2-ykdvea/calibration/set", "")

    def fail_handlers():
        raise AssertionError("empty calibration clear should not load handlers")

    monkeypatch.setattr(command_intake, "_handlers", fail_handlers)

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert results == ()
    assert transport.received_messages == []
    assert transport.published_messages == []


def test_process_inbound_messages_calibration_status_uses_heavy_handler(monkeypatch):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        '{"message_id":"cal-status","action":"status"}',
    )
    calls = []

    class Handlers:
        def process_inbound_messages(self, *args, **kwargs):
            calls.append(args[0].received_messages[0].payload_text)
            args[0].received_messages.clear()
            return (
                command_intake.CommandResult(
                    phase="published",
                    topic="nodus/co2-ykdvea/calibration/set",
                    command_type="calibration",
                    published_count=2,
                    runtime_config=runtime_config,
                ),
            )

    monkeypatch.setattr(command_intake, "_handlers", lambda: Handlers())

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert calls == ['{"message_id":"cal-status","action":"status"}']


def test_process_inbound_messages_location_config_memory_error_is_contained(
    monkeypatch,
):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-location-oom","payload":{"updates":['
            '{"section":"Sensor","key":"LOCATION","value":"Office"}'
            "]}}"
        ),
    )

    def raise_memory_error(*args, **kwargs):
        raise MemoryError("memory allocation failed")

    monkeypatch.setattr(
        command_intake,
        "_process_location_config_message",
        raise_memory_error,
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "error"
    assert results[0].command_type == "config"
    assert results[0].published_count == 0
    assert results[0].errors == ("config_location_handler_memory",)
    assert transport.received_messages == []
    assert transport.published_messages == []


def test_process_inbound_messages_fast_calibration_handles_aqi_offset_batch():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for name in ("settings.toml", "switch.toml", "sensor_i2c.toml"):
            (root / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(root).runtime_config()
        transport.receive(
            "nodus/aqi-x943fm/calibration/set",
            (
                '{"message_id":"cal-aqi","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.System.TEMP_OFFSET","value":0.6},'
                '{"key":"Calibration.System.RH_OFFSET","value":0.1},'
                '{"key":"Calibration.Device.AQI_OFFSET","value":0.0},'
                '{"key":"Calibration.Device.GAS_OFFSET","value":0.0},'
                '{"key":"Calibration.System.ALTITUDE_METERS","value":1783.0}'
                "]}}"
            ),
        )

        results = process_inbound_messages(
            transport,
            runtime_config,
            None,
            settings_root=tmpdir,
        )
        sensor_doc = Settings._read_toml_file(root / Settings.SENSOR_I2C_FILE)

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].runtime_config.sensor.calibration_system.temp_offset == 0.6
    assert results[0].runtime_config.sensor.calibration_system.rh_offset == 0.1
    assert results[0].runtime_config.sensor.calibration_device.aqi_offset == 0.0
    assert results[0].runtime_config.sensor.calibration_device.gas_offset == 0.0
    assert results[0].runtime_config.sensor.calibration_device.altitude_meters == 1783.0
    assert sensor_doc["Calibration"]["System"]["TEMP_OFFSET"] == 0.6
    assert sensor_doc["Calibration"]["System"]["RH_OFFSET"] == 0.1
    assert sensor_doc["Calibration"]["Device"]["AQI_OFFSET"] == 0.0
    assert sensor_doc["Calibration"]["Device"]["GAS_OFFSET"] == 0.0
    assert sensor_doc["Calibration"]["Device"]["ALTITUDE_METERS"] == 1783.0
    assert transport.published_messages[0].topic == "nodus/aqi-x943fm/calibration/ack"
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 5
    assert transport.published_messages[2].payload["updates"][-1] == {
        "section": "Calibration.Device",
        "key": "ALTITUDE_METERS",
        "value": 1783.0,
    }


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
