"""Tests for inbound command parsing and runtime settings updates.

The cases cover validation, dispatch, acknowledgements, and persistence for
supported MQTT command families.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import cpynodus_ii.features.command_intake as command_intake
from cpynodus_ii.core.config import (
    DetectedSensor,
    DisplayConfig,
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
            display=DisplayConfig(
                metrics=(
                    "CO2",
                    "Temperature",
                    "Rel-Humidity",
                    "Ambient VPD",
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
        '{"schema":"nodus-fwupdate/v2","message_id":"fw-1",'
        '"command":"prepare","package_id":"ota-tagA-to-tagB",'
        '"session_id":"ssssssssssssssssssssssssssssssss",'
        '"manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key_id":"test-key"}'
    )

    assert command.message_id == "fw-1"
    assert command.command == "prepare"
    assert command.package_id == "ota-tagA-to-tagB"
    assert command.session_id == "s" * 32
    assert command.manifest_sha256 == "a" * 64
    assert command.key_id == "test-key"


def test_process_fwupdate_message_persists_prepare_state(tmp_path):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _runtime_config()

    result = process_fwupdate_message(
        transport,
        runtime_config,
        topic="nodus/switch-x943fm/fwupdate",
        payload_text=(
            '{"schema":"nodus-fwupdate/v2","message_id":"fw-1",'
            '"command":"prepare","package_id":"ota-tagA-to-tagB",'
            '"session_id":"ssssssssssssssssssssssssssssssss",'
            '"manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key_id":"test-key"}'
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
    assert state.session_id == "s" * 32
    assert state.manifest_sha256 == "a" * 64
    assert state.key_id == "test-key"
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
            '{"schema":"nodus-fwupdate/v2","message_id":"fw-1",'
            '"command":"prepare","package_id":"ota-tagA-to-tagB",'
            '"session_id":"ssssssssssssssssssssssssssssssss",'
            '"manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key_id":"test-key"}'
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
        '{"schema":"nodus-fwupdate/v2","message_id":"fw-1",'
        '"command":"prepare","package_id":"ota-tagA-to-tagB",'
        '"session_id":"ssssssssssssssssssssssssssssssss",'
        '"manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key_id":"test-key"}',
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


def test_process_switch_command_message_persists_with_low_stack_writer(
    monkeypatch,
    tmp_path,
):
    switch_path = tmp_path / "switch.toml"
    switch_path.write_text(
        "[Switch]\nSWITCH_1_LAST_STATE = false\n",
        encoding="utf-8",
    )

    def fail_generic_persistence(*args, **kwargs):
        raise AssertionError("switch state should use low-stack persistence")

    monkeypatch.setattr(
        Settings,
        "apply_updates_to_directory",
        fail_generic_persistence,
    )
    transport = MQTTTransport("broker.local", 1883)
    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text="ON",
        settings_root=tmp_path,
    )

    assert result.phase == "published"
    assert result.errors == ()
    assert result.persistence_mode == "persisted"
    assert "SWITCH_1_LAST_STATE = true" in switch_path.read_text(encoding="utf-8")


def test_process_switch_command_message_pystack_persistence_is_volatile(
    monkeypatch,
    tmp_path,
):
    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(command_intake, "_write_switch_state_file", raise_pystack)
    transport = MQTTTransport("broker.local", 1883)
    result = process_switch_command_message(
        transport,
        _runtime_config(),
        _switch_service(),
        topic="nodus/S1-x943fm/config/set",
        payload_text='{"message_id":"cfg-stack","state":"ON"}',
        settings_root=tmp_path,
    )

    assert result.phase == "published"
    assert result.published_count == 5
    assert result.errors == ("switch_state_persist_pystack",)
    assert result.persistence_mode == "volatile"
    assert result.message_id == "cfg-stack"
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-stack",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }


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


def test_process_inbound_messages_fast_switch_location_appends_missing_key():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        original_text = '[Switch]\nSWITCH_1_LABEL = "Pump"\n'
        (root / "switch.toml").write_text(original_text, encoding="utf-8")
        transport.receive(
            "nodus/switch-x943fm/config/set",
            (
                '{"message_id":"cfg-loc-add","payload":{"updates":['
                '{"section":"Switch","key":"SWITCH_LOCATION","value":"Bench"}'
                "]}}"
            ),
        )

        results = process_inbound_messages(
            transport,
            _runtime_config(),
            _switch_service(),
            settings_root=tmpdir,
        )
        switch_text = (root / "switch.toml").read_text(encoding="utf-8")
        backup_text = (root / "switch.toml.bak").read_text(encoding="utf-8")

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].persistence_mode == "persisted"
    assert results[0].runtime_config.switch.location == "Bench"
    assert backup_text == original_text
    assert 'SWITCH_1_LABEL = "Pump"' in switch_text
    assert 'SWITCH_LOCATION = "Bench"' in switch_text


def test_process_inbound_messages_fast_switch_location_pystack_is_volatile(
    monkeypatch,
    tmp_path,
):
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    (tmp_path / "switch.toml").write_text(
        '[Switch]\nSWITCH_LOCATION = "TestSwitch"\n',
        encoding="utf-8",
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
    transport.receive(
        "nodus/switch-x943fm/config/set",
        (
            '{"message_id":"cfg-loc-stack","payload":{"updates":['
            '{"section":"Switch","key":"SWITCH_LOCATION","value":"Switch#1"}'
            "]}}"
        ),
    )

    results = process_inbound_messages(
        transport,
        _runtime_config(),
        _switch_service(),
        settings_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ("config_persist_pystack",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].runtime_config.switch.location == "Switch#1"
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-loc-stack",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].topic == "nodus/switch-x943fm/meta/patch"


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
            }.get(reg)
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


def test_process_inbound_messages_ignores_empty_device_config_without_heavy_handlers(
    monkeypatch,
):
    transport = MQTTTransport("broker.local", 1883)
    transport.receive("nodus/switch-x943fm/config/set", "")
    monkeypatch.setattr(
        command_intake,
        "_handlers",
        lambda: (_ for _ in ()).throw(AssertionError("heavy handler imported")),
    )

    results = process_inbound_messages(transport, _runtime_config(), _switch_service())

    assert results == ()
    assert transport.published_messages == []


def test_process_inbound_messages_rejects_bad_device_config_without_heavy_handlers(
    monkeypatch,
):
    transport = MQTTTransport("broker.local", 1883)
    transport.receive("nodus/switch-x943fm/config/set", '{"payload":{"updates":[]}}')
    monkeypatch.setattr(
        command_intake,
        "_handlers",
        lambda: (_ for _ in ()).throw(AssertionError("heavy handler imported")),
    )

    results = process_inbound_messages(transport, _runtime_config(), _switch_service())

    assert len(results) == 1
    assert results[0].phase == "error"
    assert results[0].command_type == "config"
    assert results[0].published_count == 0
    assert results[0].errors == ("schema_invalid",)
    assert transport.published_messages == []


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
    restart_command = parse_device_config_command(
        '{"message_id":"rst-1","payload":{},"restart":true,"restart_mode":"soft"}'
    )

    assert updates_command.message_id == "cfg-1"
    assert updates_command.updates[0]["section"] == "Network"
    assert settings_command.updates[0]["key"] == "BROKER"
    assert restart_command.message_id == "rst-1"
    assert restart_command.updates == ()
    assert restart_command.restart_requested is True
    assert restart_command.restart_mode == "soft"


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


def test_process_device_config_message_accepts_restart_only_command():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text=(
            '{"message_id":"rst-1","payload":{},"restart":true,"restart_mode":"soft"}'
        ),
    )

    assert result.phase == "published"
    assert result.published_count == 2
    assert result.message_id == "rst-1"
    assert result.requested_state == "restart:soft"
    assert result.reboot_requested is True
    assert result.reboot_mode == "soft"
    assert [message.topic for message in transport.published_messages] == [
        "nodus/switch-x943fm/config/ack",
        "nodus/switch-x943fm/config/result",
    ]
    assert transport.published_messages[0].payload == {
        "message_id": "rst-1",
        "accepted": True,
        "duplicate": False,
    }
    assert transport.published_messages[1].payload == {
        "message_id": "rst-1",
        "applied": True,
        "updated": 0,
        "duplicate": False,
        "error": "",
        "restart": True,
        "restart_mode": "soft",
    }


def test_process_device_config_message_does_not_reboot_duplicate_restart():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text=(
            '{"message_id":"rst-1","payload":{},"restart":true,"restart_mode":"hard"}'
        ),
        duplicate_message_ids=("rst-1",),
    )

    assert result.phase == "published"
    assert result.duplicate is True
    assert result.reboot_requested is False
    assert result.reboot_mode == "hard"
    assert transport.published_messages[1].payload["duplicate"] is True
    assert transport.published_messages[1].payload["restart"] is True
    assert transport.published_messages[1].payload["restart_mode"] == "hard"


def test_process_device_config_message_can_restart_after_config_update():
    transport = MQTTTransport("broker.local", 1883)

    result = process_device_config_message(
        transport,
        _runtime_config(),
        topic="nodus/switch-x943fm/config/set",
        payload_text=(
            '{"message_id":"cfg-rst","payload":{"updates":['
            '{"section":"Network","key":"HOSTNAME","value":"switch-new"}'
            ']},"restart":true,"restart_mode":"hard"}'
        ),
    )

    assert result.phase == "published"
    assert result.published_count == 3
    assert result.reboot_requested is True
    assert result.reboot_mode == "hard"
    assert result.runtime_config.network.hostname == "switch-new"
    assert transport.published_messages[1].payload["restart"] is True
    assert transport.published_messages[1].payload["restart_mode"] == "hard"


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
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    runtime_config.time.tz = "UTC"
    (tmp_path / "settings.toml").write_text(
        '[Time]\nTZ = "UTC"\n',
        encoding="utf-8",
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
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
    assert results[0].errors == ("config_persist_pystack",)
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


def test_process_inbound_messages_fast_display_config_persists(
    monkeypatch,
    tmp_path,
):
    def fail_generic_persistence(*args, **kwargs):
        raise AssertionError("display config should use low-stack persistence")

    monkeypatch.setattr(
        Settings,
        "apply_updates_to_directory",
        fail_generic_persistence,
    )
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    sensor_path = tmp_path / "sensor_i2c.toml"
    original_text = (
        '[Sensor]\nDEVICE = "co2"\n'
        "[Display]\n"
        'METRIC_1 = "CO2"\n'
        'METRIC_6 = "DewVPD Risk"\n'
        "[Display.Style]\n"
        'METRIC_6 = "Graph24hr"\n'
    )
    sensor_path.write_text(original_text, encoding="utf-8")
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-display","payload":{"updates":['
            '{"section":"Display","key":"METRIC_6",'
            '"value":"Temperature_F","name":"sensor_i2c.toml"}'
            ']},"restart":false}'
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )
    sensor_text = sensor_path.read_text(encoding="utf-8")
    backup_text = (tmp_path / "sensor_i2c.toml.bak").read_text(encoding="utf-8")

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ()
    assert results[0].persistence_mode == "persisted"
    assert results[0].runtime_config.sensor.display.metrics[5] == "Temperature_F"
    assert results[0].runtime_config.sensor.display.styles[5] == "Graph24hr"
    assert 'METRIC_6 = "Temperature_F"' in sensor_text
    assert 'METRIC_6 = "Graph24hr"' in sensor_text
    assert backup_text == original_text
    assert [message.topic for message in transport.published_messages] == [
        "nodus/co2-ykdvea/config/ack",
        "nodus/co2-ykdvea/config/result",
        "nodus/co2-ykdvea/meta/patch",
    ]
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-display",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Display",
            "key": "METRIC_6",
            "value": "Temperature_F",
        },
    ]


def test_process_inbound_messages_fast_display_config_pystack_is_volatile(
    monkeypatch,
    tmp_path,
):
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    (tmp_path / "sensor_i2c.toml").write_text(
        '[Display]\nMETRIC_6 = "DewVPD Risk"\n',
        encoding="utf-8",
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-display-stack","payload":{"updates":['
            '{"section":"Display","key":"METRIC_6","value":"Temperature_F"}'
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
    assert results[0].errors == ("config_persist_pystack",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].runtime_config.sensor.display.metrics[5] == "Temperature_F"
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-display-stack",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"


def test_process_inbound_messages_fast_switch_label_config_persists(
    monkeypatch,
    tmp_path,
):
    def fail_generic_persistence(*args, **kwargs):
        raise AssertionError("switch label config should use low-stack persistence")

    monkeypatch.setattr(
        Settings,
        "apply_updates_to_directory",
        fail_generic_persistence,
    )
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    switch_path = tmp_path / "switch.toml"
    original_text = '[Switch]\nSWITCH_1_LABEL = "Fan"\nSWITCH_2_LABEL = "Humidifier"\n'
    switch_path.write_text(original_text, encoding="utf-8")
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-switch-label","payload":{"updates":['
            '{"section":"Switch","key":"SWITCH_2_LABEL","value":"AC",'
            '"name":"switch.toml"}'
            ']},"restart":false}'
        ),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
        settings_root=tmp_path,
    )
    switch_text = switch_path.read_text(encoding="utf-8")
    backup_text = (tmp_path / "switch.toml.bak").read_text(encoding="utf-8")

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3
    assert results[0].errors == ()
    assert results[0].persistence_mode == "persisted"
    assert results[0].runtime_config.switch.channels[1].label == "AC"
    assert 'SWITCH_2_LABEL = "AC"' in switch_text
    assert backup_text == original_text
    assert [message.topic for message in transport.published_messages] == [
        "nodus/co2-ykdvea/config/ack",
        "nodus/co2-ykdvea/config/result",
        "nodus/co2-ykdvea/meta/patch",
    ]
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-switch-label",
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Switch",
            "key": "SWITCH_2_LABEL",
            "value": "AC",
        },
    ]


def test_process_inbound_messages_fast_switch_label_config_pystack_is_volatile(
    monkeypatch,
    tmp_path,
):
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    (tmp_path / "switch.toml").write_text(
        '[Switch]\nSWITCH_2_LABEL = "Humidifier"\n',
        encoding="utf-8",
    )

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
    transport.receive(
        "nodus/co2-ykdvea/config/set",
        (
            '{"message_id":"cfg-switch-label-stack","payload":{"updates":['
            '{"section":"Switch","key":"SWITCH_2_LABEL","value":"AC"}'
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
    assert results[0].errors == ("config_persist_pystack",)
    assert results[0].persistence_mode == "volatile"
    assert results[0].runtime_config.switch.channels[1].label == "AC"
    assert transport.published_messages[1].payload == {
        "message_id": "cfg-switch-label-stack",
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


def test_parse_calibration_command_accepts_soil_moisture_alias():
    command = parse_calibration_command(
        (
            '{"message_id":"cal-moist","action":"apply","payload":{"offsets":['
            '{"key":"soil_moisture_offset","value":20.0}'
            "]}}"
        )
    )

    assert command.message_id == "cal-moist"
    assert command.updates == (
        {
            "section": "Calibration.Device",
            "key": "SOIL_MOIST_CAL_VAL",
            "value": 20.0,
        },
    )


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
        payload_text='{"message_id":"cal-2","action":"apply","payload":{"offsets":[{"key":"Calibration.System.CO2_OFFSET","value":-400.0}]}}',
    )

    assert result.phase == "published"
    assert result.runtime_config.sensor.calibration_system.co2_offset == -400.0
    assert result.runtime_config.sensor.calibration_device.temp_offset == 0.0


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
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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
                '{"key":"Calibration.Device.TEMP_OFFSET","value":-2.5}'
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
    assert results[0].runtime_config.sensor.calibration_device.altitude_meters == 0.0
    assert sensor_doc["Calibration"]["Device"]["TEMP_OFFSET"] == -2.5
    assert transport.published_messages[0].topic == "nodus/co2-ykdvea/calibration/ack"
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].topic == "nodus/co2-ykdvea/meta/patch"
    assert transport.published_messages[2].payload["source"] == "calibration_set"


def test_process_inbound_messages_fast_calibration_split_offsets_persist():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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


def test_process_inbound_messages_fast_soil_moisture_alias_persists_canonical_key():
    transport = MQTTTransport("broker.local", 1883)
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / Settings.SENSOR_SOIL_FILE).write_text(
            (
                "[Calibration.Device]\n"
                "SOIL_TEMP_CAL_VAL = 0.0\n"
                "SOIL_MOIST_CAL_VAL = 0.0\n"
                "SOIL_PH_CAL_VAL = 0.0\n"
                "SOIL_EC_CAL_VAL = 0.0\n"
            ),
            encoding="utf-8",
        )
        runtime_config = _soil_runtime_config()
        transport.receive(
            "nodus/soil-abc123/calibration/set",
            (
                '{"message_id":"cal-moist","action":"apply","payload":{"offsets":['
                '{"key":"soil_moisture_offset","value":20.0}'
                "]}}"
            ),
        )

        results = process_inbound_messages(
            transport,
            runtime_config,
            _sensor_switch_service(),
            settings_root=tmpdir,
        )
        soil_doc = Settings._read_toml_file(root / Settings.SENSOR_SOIL_FILE)

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].requested_state == "offset_fast:applied"
    calibration = results[0].runtime_config.sensor.calibration_device
    assert calibration.soil_moist_cal_val == 20.0
    assert soil_doc["Calibration"]["Device"]["SOIL_MOIST_CAL_VAL"] == 20.0
    assert transport.published_messages[1].payload["applied"] is True
    assert transport.published_messages[1].payload["updated"] == 1
    assert transport.published_messages[2].payload["updates"] == [
        {
            "section": "Calibration.Device",
            "key": "SOIL_MOIST_CAL_VAL",
            "value": 20.0,
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
        transport.published_messages[1].topic == "nodus/co2-ykdvea/calibration/result"
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
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()

    def raise_memory(*args, **kwargs):
        raise MemoryError("memory allocation failed")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_memory)
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
    assert results[0].errors == ("calibration_offset_persist_memory",)
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
    from cpynodus_ii.features import scalar_persistence

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()

    def raise_pystack(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_pystack)
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
    from cpynodus_ii.features import scalar_persistence
    from cpynodus_ii.features.calibration_config import (
        process_calibration_apply_message,
    )

    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()

    def raise_memory(*args, **kwargs):
        raise MemoryError("memory allocation failed")

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", raise_memory)

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
    assert result.errors == ("calibration_persist_memory",)
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


def test_process_inbound_messages_calibration_status_avoids_heavy_handler(monkeypatch):
    transport = MQTTTransport("broker.local", 1883)
    runtime_config = _sensor_switch_runtime_config()
    transport.receive(
        "nodus/co2-ykdvea/calibration/set",
        '{"message_id":"cal-status","action":"status"}',
    )
    monkeypatch.setattr(
        command_intake,
        "_handlers",
        lambda: (_ for _ in ()).throw(AssertionError("heavy handler imported")),
    )

    results = process_inbound_messages(
        transport,
        runtime_config,
        _sensor_switch_service(),
    )

    assert len(results) == 1
    assert results[0].phase == "published"
    assert results[0].published_count == 3


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
        "_process_device_config_message",
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
    assert results[0].published_count == 2
    assert results[0].errors == ("config_handler_memory",)
    assert transport.received_messages == []
    assert transport.published_messages[1].payload["applied"] is False


def test_process_inbound_messages_fast_calibration_handles_aqi_offset_batch():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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
    assert results[0].phase == "error"
    assert results[0].published_count == 2
    assert results[0].errors == ("single_update_required",)
    assert sensor_doc["Calibration"]["System"]["TEMP_OFFSET"] != 0.6
    assert transport.published_messages[0].topic == "nodus/aqi-x943fm/calibration/ack"
    assert transport.published_messages[1].payload["applied"] is False


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
