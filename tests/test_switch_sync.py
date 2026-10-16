"""Verify device-owned switch state across commands, HTTP, and snapshots.

Host fakes cover durability reporting, per-channel isolation, and shallow
command depth. GPIO and MQTT delivery still require hardware validation.
"""

import sys
from types import SimpleNamespace

import pytest

from cpynodus_ii.core.config import RuntimeConfig, SwitchChannelConfig, SwitchConfig
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.command_intake import process_inbound_messages
from cpynodus_ii.features.metadata_refresh import refresh_saved_metadata
from cpynodus_ii.features.payloads import build_switch_meta_payload
from cpynodus_ii.features.publish_cycle import publish_startup_cycle
from cpynodus_ii.features.web_handlers import handle_switch_state_request


def _config():
    return RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-test",
            channel_count=2,
            channels=tuple(
                SwitchChannelConfig(
                    key="SWITCH_{}".format(n),
                    channel_id="S{}-test".format(n),
                    label="Relay {}".format(n),
                    last_state=False,
                )
                for n in (1, 2)
            ),
        )
    )


def _service():
    return SimpleNamespace(
        phase="ready",
        device_id="switch-test",
        errors=(),
        channels=tuple(
            SimpleNamespace(
                key="SWITCH_{}".format(n),
                channel_id="S{}-test".format(n),
                phase="ready",
                errors=(),
                enable_handle=SimpleNamespace(value=True),
                control_handle=SimpleNamespace(value=False),
            )
            for n in (1, 2)
        ),
    )


def _command(transport, channel=1, state=True, message_id="action-1"):
    import json

    transport.receive(
        "nodus/S{}-test/config/set".format(channel),
        json.dumps(
            {
                "message_id": message_id,
                "payload": {
                    "updates": [
                        {
                            "section": "Switch",
                            "key": "SWITCH_{}_LAST_STATE".format(channel),
                            "value": state,
                            "name": "switch.toml",
                        }
                    ]
                },
            }
        ),
    )


def test_batch_switch_updates_preserve_both_channels_and_report_durability(tmp_path):
    path = tmp_path / "switch.toml"
    path.write_text(
        "[Switch]\nSWITCH_1_LAST_STATE = false\nSWITCH_2_LAST_STATE = false\n"
    )
    transport = MQTTTransport()
    service = _service()
    _command(transport)
    _command(transport, channel=2, message_id="action-2")
    _command(transport, channel=1, state=False, message_id="action-3")
    results = process_inbound_messages(
        transport, _config(), service, settings_root=tmp_path
    )
    config = results[-1].runtime_config
    assert [c.last_state for c in config.switch.channels] == [False, True]
    assert [c.control_handle.value for c in service.channels] == [False, True]
    assert "SWITCH_1_LAST_STATE = false" in path.read_text()
    assert "SWITCH_2_LAST_STATE = true" in path.read_text()
    replies = [
        m.payload
        for m in transport.published_messages
        if m.topic.endswith("/config/result")
    ]
    assert [r["message_id"] for r in replies] == ["action-1", "action-2", "action-3"]
    assert all(r["applied"] and r["persistence_mode"] == "persisted" for r in replies)
    assert all(r["persistence_errors"] == [] for r in replies)


@pytest.mark.parametrize("origin", ["mqtt", "web"])
def test_switch_action_reuses_config_without_dataclass_reconstruction(
    monkeypatch, origin
):
    import dataclasses

    config = _config()
    switch = config.switch
    channels = switch.channels
    service = _service()

    def forbidden(*args, **kwargs):
        raise MemoryError("no room to reconstruct configuration")

    monkeypatch.setattr(dataclasses, "replace", forbidden)
    if origin == "mqtt":
        transport = MQTTTransport()
        _command(transport)
        result = process_inbound_messages(transport, config, service)[0]
        assert result.phase == "published"
        updated = result.runtime_config
    else:
        result = handle_switch_state_request(
            config, service, channel_id="S1-test", state=True
        )
        assert result["success"]
        updated = result["runtime_config"]
    assert updated is config
    assert updated.switch is switch
    assert updated.switch.channels is channels
    assert [c.last_state for c in channels] == [True, False]
    assert service.channels[0].control_handle.value is True


@pytest.mark.parametrize("failure", [MemoryError, RuntimeError])
def test_switch_helper_import_failure_is_reported_without_applying_output(
    monkeypatch, failure
):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "cpynodus_ii.features.switch_state":
            raise failure("pystack exhausted")
        return original_import(name, *args, **kwargs)

    transport = MQTTTransport()
    service = _service()
    config = _config()
    _command(transport)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = process_inbound_messages(transport, config, service)[0]
    assert result.phase == "error"
    assert not config.switch.channels[0].last_state
    assert not service.channels[0].control_handle.value
    assert len(transport.published_messages) == 2
    reply = transport.published_messages[-1].payload
    assert reply["message_id"] == "action-1"
    assert reply["applied"] is False
    assert reply["error"] == (
        "switch_command_memory" if failure is MemoryError else "switch_command_pystack"
    )


@pytest.mark.parametrize("failure", ["readonly", "memory", "pystack", "unwritable"])
def test_both_origins_preserve_applied_state_when_persistence_fails(
    monkeypatch, tmp_path, failure
):
    def fail(*args):
        if failure == "memory":
            raise MemoryError()
        if failure == "pystack":
            raise RuntimeError("pystack exhausted")
        return ("read_only_filesystem",)

    monkeypatch.setattr(
        "cpynodus_ii.features.scalar_persistence.write_toml_scalar", fail
    )
    root = None if failure == "unwritable" else tmp_path
    transport = MQTTTransport()
    _command(transport)
    result = process_inbound_messages(
        transport, _config(), _service(), settings_root=root
    )[0]
    reply = transport.published_messages[1].payload
    assert result.runtime_config.switch.channels[0].last_state is True
    assert reply["applied"] is True
    assert reply["persistence_mode"] == "volatile"
    assert reply["persistence_errors"] == list(result.errors)
    web = handle_switch_state_request(
        _config(), _service(), channel_id="S1-test", state=True, settings_root=root
    )
    assert web["success"] is True
    assert web["runtime_config"].switch.channels[0].last_state is True
    assert web["persistence_mode"] == "volatile"
    assert web["persistence_errors"] == reply["persistence_errors"]


@pytest.mark.parametrize("origin", ["mqtt", "web"])
def test_failed_application_does_not_persist_or_emit_state(
    monkeypatch, tmp_path, origin
):
    def forbidden(*args):
        raise AssertionError("failed output must not persist")

    monkeypatch.setattr(
        "cpynodus_ii.features.scalar_persistence.write_toml_scalar", forbidden
    )
    service = _service()
    service.channels[0].phase = "error"
    service.channels[0].errors = ("gpio_unavailable",)
    config = _config()
    if origin == "web":
        result = handle_switch_state_request(
            config, service, channel_id="S1-test", state=True, settings_root=tmp_path
        )
        assert result["success"] is False
        assert config.switch.channels[0].last_state is False
        assert service.channels[0].control_handle.value is False
        return
    transport = MQTTTransport()
    _command(transport)
    result = process_inbound_messages(
        transport, config, service, settings_root=tmp_path
    )[0]
    assert result.phase == "error"
    assert result.runtime_config.switch.channels[0].last_state is False
    assert len(transport.published_messages) == 2
    assert transport.published_messages[1].payload["applied"] is False


def test_refresh_and_reconnect_keep_live_state_without_replaying_events(tmp_path):
    (tmp_path / "switch.toml").write_text(
        "[Switch]\nSWITCH_1_LAST_STATE = false\nSWITCH_2_LAST_STATE = false\n"
    )
    service = _service()
    web = handle_switch_state_request(
        _config(), service, channel_id="S1-test", state=True
    )
    config = web["runtime_config"]
    transport = MQTTTransport()
    transport.mark_connected()
    transport._metadata_refresh_pending = 4
    for _ in range(2):
        refresh_saved_metadata(
            transport,
            config,
            settings_root=tmp_path,
            version="test",
            switch_service=service,
        )
        message = transport.published_messages.pop()
        import json

        payload = json.loads(message.payload)
        assert [c["state"] for c in payload["channels"]] == [True, False]
        transport._metadata_refresh_pending = 4
    # Even without a live snapshot, updated runtime state survives saved-file overlays.
    assert (
        build_switch_meta_payload(config, settings_root=tmp_path)["channels"][0][
            "state"
        ]
        is True
    )
    transport.mark_disconnected()
    transport.mark_connected()
    publish_startup_cycle(transport, config, version="test")
    states = [m for m in transport.published_messages if m.topic.endswith("/state")]
    assert [m.payload["state"] for m in states] == ["ON", "OFF"]
    assert not any(m.topic.endswith("/event") for m in transport.published_messages)


def test_switch_commit_stays_within_six_firmware_frames(tmp_path):
    (tmp_path / "switch.toml").write_text("[Switch]\nSWITCH_1_LAST_STATE = false\n")
    transport = MQTTTransport()
    _command(transport)
    depths = []

    def profile(frame, event, arg):
        if event != "call" or "/cpynodus_ii/" not in frame.f_code.co_filename:
            return
        depth = 0
        while frame is not None:
            if "/cpynodus_ii/" in frame.f_code.co_filename:
                depth += 1
            frame = frame.f_back
        depths.append(depth)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        result = process_inbound_messages(
            transport, _config(), _service(), settings_root=tmp_path
        )[0]
    finally:
        sys.setprofile(previous)
    assert result.persistence_mode == "persisted"
    assert max(depths) <= 6
