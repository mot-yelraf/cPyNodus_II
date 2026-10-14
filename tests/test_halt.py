"""Verify graceful halt command intake and resource cleanup.

Host doubles pin transaction ordering, MQTT failure bounds, and relay GPIO
ownership. Board power-loss and socket behavior require hardware validation.
"""

import json
from types import SimpleNamespace

import pytest

from cpynodus_ii import app
from cpynodus_ii.core.config import RuntimeConfig, SwitchConfig
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.command_intake import process_inbound_messages
from cpynodus_ii.features.steady_state import SteadyState, run_steady_state_iteration


def _config():
    return RuntimeConfig(
        active_profile="sensorius",
        switch=SwitchConfig(present=True, device_id="switch-halt"),
    )


def _queue(transport, message_id="halt-1", **extra):
    transport.receive(
        "nodus/switch-halt/config/set",
        json.dumps({"message_id": message_id, "payload": {}, "halt": True, **extra}),
    )


def test_halt_accepts_empty_command_and_discards_later_mutations(tmp_path):
    transport = MQTTTransport()
    _queue(transport)
    transport.receive(
        "nodus/switch-halt/config/set",
        '{"message_id":"later","payload":{"settings":{"Switch":'
        '{"SWITCH_LOCATION":"changed"}}}}',
    )
    config = _config()
    results = process_inbound_messages(transport, config, None, settings_root=tmp_path)
    assert len(results) == 1
    assert results[0].halt_requested
    assert not results[0].reboot_requested
    assert config.switch.location != "changed"
    assert len(transport.published_messages) == 1  # Accepted, not yet halted.
    assert transport.published_messages[0].payload["accepted"] is True


@pytest.mark.parametrize(
    "extra",
    [
        {"restart": True},
        {
            "payload": {
                "updates": [
                    {"section": "Switch", "key": "SWITCH_LOCATION", "value": "changed"}
                ]
            }
        },
    ],
)
def test_halt_rejects_combined_restart_or_mutation(extra):
    transport = MQTTTransport()
    _queue(transport, **extra)
    result = process_inbound_messages(transport, _config(), None)[0]
    assert not result.halt_requested
    assert not result.reboot_requested
    assert result.errors == ("halt_requires_empty_payload",)
    assert len(transport.published_messages) == 2


def test_duplicate_halt_does_not_shutdown_again():
    transport = MQTTTransport()
    _queue(transport)
    result = process_inbound_messages(
        transport, _config(), None, handled_message_ids=("halt-1",)
    )[0]
    assert result.duplicate
    assert not result.halt_requested


def test_active_config_transaction_finishes_before_halt(tmp_path, monkeypatch):
    from cpynodus_ii.features import scalar_persistence

    path = tmp_path / "switch.toml"
    path.write_text('[Switch]\nSWITCH_LOCATION = "before"\n')
    transport = MQTTTransport()
    transport.receive(
        "nodus/switch-halt/config/set",
        '{"message_id":"write","payload":{"settings":{"Switch":'
        '{"SWITCH_LOCATION":"after"}}}}',
    )
    original = scalar_persistence.write_toml_scalar
    finished = []

    def write_and_receive_halt(*args):
        # Simulate a halt arriving during a synchronous transaction. Intake
        # cannot handle it until the complete writer (including renames) returns.
        _queue(transport)
        errors = original(*args)
        finished.append(path.read_text())
        return errors

    monkeypatch.setattr(scalar_persistence, "write_toml_scalar", write_and_receive_halt)
    first = process_inbound_messages(transport, _config(), None, settings_root=tmp_path)
    assert not first[0].halt_requested
    assert finished == ['[Switch]\nSWITCH_LOCATION = "after"\n']
    assert not (tmp_path / "switch.toml.tmp").exists()
    second = process_inbound_messages(
        transport, _config(), None, settings_root=tmp_path
    )
    assert second[0].halt_requested


def test_halt_skips_background_calibration_and_sensor_work(monkeypatch):
    from cpynodus_ii.features import steady_state

    def forbidden(*args, **kwargs):
        pytest.fail("background work ran after halt request")

    monkeypatch.setattr(steady_state, "_process_soil_calibration_session", forbidden)
    monkeypatch.setattr(steady_state, "_process_log_transfer_session", forbidden)
    transport = MQTTTransport()
    transport.mark_connected()
    _queue(transport)
    result = run_steady_state_iteration(
        transport,
        _config(),
        None,
        object(),
        state=SteadyState(
            connection_generation=transport.connection_generation,
            switch_meta_generation=transport.connection_generation,
        ),
    )
    assert result.command_results[0].halt_requested
    assert result.sensor_publish_phase == "skipped"
    assert result.calibration_session_phase == "skipped"


@pytest.mark.parametrize(
    "mqtt_failure,sync_failure", [(False, False), (True, False), (False, True)]
)
def test_halt_syncs_then_closes_network_without_stopping_relays(
    monkeypatch, mqtt_failure, sync_failure
):
    events = []
    sent = []
    transport = MQTTTransport()
    transport.mark_connected()
    _queue(transport)
    process_inbound_messages(transport, _config(), None)
    adapter = object()
    transport.subscriptions.append("pending/command")

    monkeypatch.setattr(
        app, "_stop_web_runtime_for_shutdown", lambda *a, **k: events.append("web_stop")
    )

    def stop_services(**kwargs):
        assert kwargs.get("switch_service") is None
        events.append("sensor_stop")

    monkeypatch.setattr(app, "_stop_feature_services_for_shutdown", stop_services)

    def sync():
        events.append("fs_sync")
        if sync_failure:
            raise OSError("flash failure")

    monkeypatch.setattr(app.os, "sync", sync)

    def send(adapter, transport):
        assert transport.subscriptions == []
        events.append("mqtt_send")
        if mqtt_failure:
            raise OSError("broker unavailable")
        sent.append(transport.published_messages.pop(0))
        return SimpleNamespace(adapter=adapter, phase="synced", published_count=1)

    monkeypatch.setattr(app, "sync_transport_to_client", send)
    monkeypatch.setattr(
        app, "close_mqtt_client", lambda *a: events.append("mqtt_close")
    )
    monkeypatch.setattr(
        app,
        "_teardown_network_for_shutdown",
        lambda *a, **k: events.append("network_stop"),
    )
    logs = []
    monkeypatch.setattr(app, "_print_log", lambda *a, **k: logs.append(a))
    app._prepare_runtime_halt(
        runtime_config=_config(),
        network_stack=object(),
        sensor_service=object(),
        mqtt_adapter=adapter,
        transport=transport,
        message_id="halt-1",
        start_monotonic=0,
    )
    assert events[:3] == ["web_stop", "sensor_stop", "fs_sync"]
    assert events[-2:] == ["mqtt_close", "network_stop"]
    assert "phase=halted" in logs[-1][1]
    if not mqtt_failure:
        result = next(m.payload for m in sent if m.topic.endswith("config/result"))
        assert result["applied"] is not sync_failure
        assert result["phase"] == "halted"
        assert result["relays"] == "held"
        assert any(m.retain and m.topic.endswith("status/heartbeat") for m in sent)


def test_halt_wait_has_no_application_scheduler(monkeypatch):
    sleeps = []

    class StopWait(BaseException):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopWait

    monkeypatch.setattr(app.time, "sleep", sleep)
    with pytest.raises(StopWait):
        app._wait_for_power_cycle()
    assert sleeps == [1.0, 1.0, 1.0]


def test_halt_mqtt_drain_is_bounded_even_if_queue_does_not_progress(monkeypatch):
    transport = MQTTTransport()
    transport.mark_connected()
    sends = []
    monkeypatch.setattr(app.os, "sync", lambda: None)
    monkeypatch.setattr(app, "_stop_web_runtime_for_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(app, "_stop_feature_services_for_shutdown", lambda **k: None)
    monkeypatch.setattr(app, "_teardown_network_for_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(app.time, "monotonic", lambda: 0)
    monkeypatch.setattr(app, "_print_log", lambda *a, **k: None)

    def send(adapter, transport):
        sends.append(True)
        return SimpleNamespace(adapter=adapter, phase="synced", published_count=1)

    monkeypatch.setattr(app, "sync_transport_to_client", send)
    closed = []
    monkeypatch.setattr(app, "close_mqtt_client", lambda *a: closed.append(True))
    app._prepare_runtime_halt(
        runtime_config=_config(),
        network_stack=None,
        mqtt_adapter=object(),
        transport=transport,
        message_id="halt-1",
        start_monotonic=0,
    )
    assert len(sends) == 32
    assert closed == [True]
