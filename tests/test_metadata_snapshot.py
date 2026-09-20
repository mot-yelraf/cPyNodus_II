"""Verify retained configuration uses saved values and explicit presence.

These cases exercise streaming TOML reads, calibration identity and state,
and the deferred refresh boundary independently of hardware MQTT delivery.
"""

import json
from types import SimpleNamespace

import pytest

from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.features.metadata_refresh import refresh_saved_metadata
from cpynodus_ii.features.metadata_snapshot import add_configuration_snapshot
from cpynodus_ii.features.payloads import build_runtime_meta_payload


def _config():
    return RuntimeConfig(
        sensor=DetectedSensor(
            family="i2c",
            device="avpd",
            sensor_id="avpd-test",
            active_config_file="sensor_i2c.toml",
        )
    )


def _saved(root, altitude=0):
    (root / "settings.toml").write_text("""[Time]
TZ_OFFSET = 0
TZ = "Etc/UTC"
NTP_SERVER = ""
[HomeAssistant]
PUBLISH_DISCOVERY_RETAIN = false
PUBLISH_STATE_RETAIN = false
PUBLISH_LEGACY_SENSOR_TOPIC = false
""")
    (root / "sensor_i2c.toml").write_text(
        """[Calibration]
CALIBRATED = false
CALIB_STATUS = "Not Calibrated"
[Calibration.System]
TEMP_OFFSET = 0.0
REF_START_TS = 0
REF_NOTE = "reference #1"
[Calibration.Device]
ALTITUDE_METERS = {}
TEMP_OFFSET = -1.5
""".format(altitude)
    )


@pytest.mark.parametrize("altitude", [0, 1719.0])
def test_saved_calibration_preserves_presence_status_and_zero(tmp_path, altitude):
    _saved(tmp_path, altitude)
    config = _config()
    config.sensor.calibration_device.altitude_meters = 999
    payload = {"sensor": {}}
    add_configuration_snapshot(payload, config, tmp_path)
    assert payload["sensor"]["calibration"] == {
        "CALIBRATED": False,
        "CALIB_STATUS": "Not Calibrated",
        "System": {"TEMP_OFFSET": 0.0, "REF_START_TS": 0, "REF_NOTE": "reference #1"},
        "Device": {"ALTITUDE_METERS": altitude, "TEMP_OFFSET": -1.5},
    }
    assert payload["time"] == {"TZ_OFFSET": 0, "TZ": "Etc/UTC", "NTP_SERVER": ""}
    assert payload["homeassistant"] == {
        "PUBLISH_DISCOVERY_RETAIN": False,
        "PUBLISH_STATE_RETAIN": False,
        "PUBLISH_LEGACY_SENSOR_TOPIC": False,
    }


def test_missing_saved_values_do_not_advertise_defaults(tmp_path):
    payload = {"sensor": {}}
    add_configuration_snapshot(payload, _config(), tmp_path)
    assert payload["sensor"]["calibration"] == {}
    assert payload["time"] == {}
    assert payload["homeassistant"] == {}


def _result(mode="persisted", duplicate=False):
    return SimpleNamespace(
        command_type="config",
        persistence_mode=mode,
        duplicate=duplicate,
        phase="published",
    )


def test_refresh_waits_for_replies_and_coalesces_saved_edits(tmp_path):
    _saved(tmp_path)
    config = _config()
    transport = MQTTTransport()
    transport.mark_connected()
    transport.publish("nodus/avpd-test/config/result", {"applied": True})
    kwargs = dict(settings_root=tmp_path, version="test")
    first = refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    assert first.phase == "skipped"
    assert transport._metadata_refresh_pending
    _saved(tmp_path, 1719)
    refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    transport.published_messages.clear()
    refreshed = refresh_saved_metadata(transport, config, **kwargs)
    assert refreshed.phase == "published"
    assert all(message.retain for message in transport.published_messages)
    meta = next(
        m.payload for m in transport.published_messages if m.topic.endswith("/meta")
    )
    assert (
        json.loads(meta)["sensor"]["calibration"]["Device"]["ALTITUDE_METERS"] == 1719
    )
    transport.published_messages.clear()
    assert refresh_saved_metadata(transport, config, **kwargs).published_count == 1
    assert transport.published_messages[0].topic.endswith("/meta/config")
    assert not transport._metadata_refresh_pending
    transport.published_messages.clear()
    assert refresh_saved_metadata(transport, config, **kwargs).published_count == 0


@pytest.mark.parametrize("result", [_result("volatile"), _result(duplicate=True)])
def test_volatile_or_duplicate_result_does_not_request_refresh(tmp_path, result):
    transport = MQTTTransport()
    transport.mark_connected()
    refresh_saved_metadata(
        transport, _config(), (result,), settings_root=tmp_path, version="test"
    )
    assert not transport.published_messages
    assert not getattr(transport, "_metadata_refresh_pending", False)


def test_snapshot_build_failure_keeps_refresh_pending(tmp_path, monkeypatch):
    import cpynodus_ii.features.metadata_refresh as refresh

    _saved(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    builder = refresh.build_runtime_meta_payload

    def fail(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(refresh, "build_runtime_meta_payload", fail)
    kwargs = dict(settings_root=tmp_path, version="test")
    result = refresh_saved_metadata(transport, _config(), (_result(),), **kwargs)
    assert result.errors == ("metadata_snapshot_failed",)
    assert transport._metadata_refresh_pending
    monkeypatch.setattr(refresh, "build_runtime_meta_payload", builder)
    assert refresh_saved_metadata(transport, _config(), **kwargs).phase == "published"


def test_saved_snapshot_is_repeatable_except_timestamp(tmp_path):
    _saved(tmp_path)
    first = build_runtime_meta_payload(
        _config(), version="test", settings_root=tmp_path
    )
    second = build_runtime_meta_payload(
        _config(), version="test", settings_root=tmp_path
    )
    first.pop("timestamp")
    second.pop("timestamp")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_persisted_mqtt_edit_refreshes_after_reply_drain(tmp_path):
    from cpynodus_ii.features.steady_state import (
        SteadyState,
        run_steady_state_iteration,
    )

    _saved(tmp_path)
    config = _config()
    transport = MQTTTransport()
    transport.mark_connected()
    transport.receive(
        "nodus/avpd-test/calibration/set",
        json.dumps(
            {
                "message_id": "altitude-1",
                "action": "apply",
                "payload": {
                    "offsets": [
                        {
                            "key": "Calibration.Device.ALTITUDE_METERS",
                            "value": 1719.0,
                        }
                    ]
                },
            }
        ),
    )
    state = SteadyState(
        connection_generation=transport.connection_generation,
        last_availability_publish_at=0,
    )
    first = run_steady_state_iteration(
        transport,
        config,
        None,
        state=state,
        settings_root=tmp_path,
        now_monotonic=1,
    )
    assert first.command_results[0].persistence_mode == "persisted"
    assert transport._metadata_refresh_pending
    assert not any(m.topic.endswith("/meta") for m in transport.published_messages)
    assert any(
        m.topic.endswith("/meta/patch") and not m.retain
        for m in transport.published_messages
    )
    transport.published_messages.clear()
    run_steady_state_iteration(
        transport,
        first.runtime_config,
        None,
        state=first.state,
        settings_root=tmp_path,
        now_monotonic=2,
    )
    meta = next(m for m in transport.published_messages if m.topic.endswith("/meta"))
    assert meta.retain
    assert (
        json.loads(meta.payload)["sensor"]["calibration"]["Device"]["ALTITUDE_METERS"]
        == 1719
    )


def test_read_only_startup_uses_saved_metadata_root(tmp_path):
    from cpynodus_ii.features.steady_state import run_steady_state_iteration

    _saved(tmp_path, 1719)
    transport = MQTTTransport()
    transport.mark_connected()
    run_steady_state_iteration(
        transport,
        _config(),
        None,
        settings_root=None,
        metadata_root=tmp_path,
    )
    meta = next(m for m in transport.published_messages if m.topic.endswith("/meta"))
    assert meta.payload["sensor"]["calibration"]["Device"]["ALTITUDE_METERS"] == 1719


def test_reader_uses_backup_like_settings_loader(tmp_path):
    _saved(tmp_path, 1719)
    source = tmp_path / "sensor_i2c.toml"
    source.rename(tmp_path / "sensor_i2c.toml.bak")
    payload = {"sensor": {}}
    add_configuration_snapshot(payload, _config(), tmp_path)
    assert payload["sensor"]["calibration"]["Device"]["ALTITUDE_METERS"] == 1719


def test_soil_alias_prefers_explicit_canonical_zero(tmp_path):
    (tmp_path / "sensor_soil.toml").write_text("""[Calibration.Device]
SOIL_TEMP_MOIST_VAL = 8.0
SOIL_MOIST_CAL_VAL = 0.0
SOIL_PH_CAL_VAL = -0.2
""")
    config = RuntimeConfig(sensor=DetectedSensor(family="soil", device="soil"))
    payload = {"sensor": {}}
    add_configuration_snapshot(payload, config, tmp_path)
    assert payload["sensor"]["calibration"] == {
        "Device": {"SOIL_MOIST_CAL_VAL": 0.0, "SOIL_PH_CAL_VAL": -0.2},
    }


def _circuitpython_publish_result_class():
    """Load the actual board shim and erase annotations as CircuitPython does."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    shim_tree = ast.parse((root / "dataclasses.py").read_text())
    branch = next(node for node in shim_tree.body if isinstance(node, ast.If))
    namespace = {}
    exec(
        compile(
            ast.Module(body=branch.orelse, type_ignores=[]), "dataclasses.py", "exec"
        ),
        namespace,
    )
    source = root / "cpynodus_ii/features/publish_cycle.py"
    tree = ast.parse(source.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PublishCycleResult"
    )
    body = []
    for node in cls.body:
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                body.append(ast.Assign(targets=[node.target], value=node.value))
        else:
            body.append(node)
    cls.body = body
    tree = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["PublishCycleResult"]


@pytest.mark.parametrize("phase", ["skipped", "published", "error"])
def test_refresh_with_annotation_free_circuitpython_dataclass(
    tmp_path, monkeypatch, phase
):
    import cpynodus_ii.features.metadata_refresh as refresh

    result_class = _circuitpython_publish_result_class()
    # Reproduce the reported III failure before exercising the keyword-only fix.
    with pytest.raises(TypeError, match="too many positional arguments"):
        result_class("published", 1, ())
    monkeypatch.setattr(refresh, "PublishCycleResult", result_class)
    _saved(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    if phase == "error":

        def fail(*args, **kwargs):
            raise MemoryError()

        monkeypatch.setattr(refresh, "build_runtime_meta_payload", fail)
    results = () if phase == "skipped" else (_result(),)
    result = refresh.refresh_saved_metadata(
        transport,
        _config(),
        results,
        settings_root=tmp_path,
        version="test",
    )
    assert result.phase == phase
    assert result.published_count == (1 if phase == "published" else 0)
    assert result.errors == (("metadata_snapshot_failed",) if phase == "error" else ())


def test_split_snapshot_contains_full_configuration_and_saved_display(tmp_path):
    from cpynodus_ii.features.payloads import build_configuration_meta_payload

    _saved(tmp_path, 1719)
    with (tmp_path / "sensor_i2c.toml").open("a") as handle:
        handle.write(
            '[Display]\nMETRIC_1 = "Temperature"\n[Display.Style]\nMETRIC_1 = "gauge"\n'
        )
    config = _config()
    payload = build_configuration_meta_payload(config, settings_root=tmp_path)
    compact = build_runtime_meta_payload(config, version="test", settings_root=tmp_path)
    assert compact["config_topic"] == "nodus/avpd-test/meta/config"
    assert compact["sensor"]["device"] == payload["sensor"]["device"] == "avpd"
    assert compact["sensor"]["config_file"] == "sensor_i2c.toml"
    assert payload["schema"] == "nodus-meta-config/v1"
    assert payload["sensor"]["calibration"]["CALIBRATED"] is False
    assert payload["sensor"]["display_metrics"] == ["Temperature"]
    assert payload["sensor"]["display_styles"] == ["gauge"]
    assert payload["time"]["TZ_OFFSET"] == 0
    assert "AUTO_TIMEZONE" not in payload["time"]
    assert "time" not in compact
    assert "homeassistant" not in compact
    assert compact["sensor"]["calibration"] == {"Device": {"ALTITUDE_METERS": 1719}}


def test_saved_fixture_split_packets_stay_under_single_mss():
    from pathlib import Path

    from cpynodus_ii.core.mqtt_client import _mqtt_publish_packet_size
    from cpynodus_ii.core.settings import Settings
    from cpynodus_ii.features.payloads import (
        build_configuration_meta_payload,
        build_switch_meta_payload,
    )

    root = Path(__file__).parent / "fixtures/sensor_switch"
    config = Settings.from_directory(root).runtime_config()
    payloads = {
        "meta": build_runtime_meta_payload(
            config, version="v0.26.263.1", settings_root=root
        ),
        "meta/config": build_configuration_meta_payload(config, settings_root=root),
        "meta/switch": build_switch_meta_payload(config, settings_root=root),
    }
    for suffix, payload in payloads.items():
        topic = "nodus/{}/{}".format(config.sensor.sensor_id, suffix)
        assert (
            _mqtt_publish_packet_size(
                topic,
                json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            <= 1460
        ), suffix


def test_switch_snapshot_refresh_uses_saved_pins_labels_and_override(tmp_path):
    from cpynodus_ii.core.config import SwitchChannelConfig, SwitchConfig
    from cpynodus_ii.features.payloads import build_switch_meta_payload

    (tmp_path / "switch.toml").write_text("""[Switch]
SWITCH_LOCATION = "Saved location"
SWITCH_1_LABEL = "Saved label"
SWITCH_1_PIN = "GP28"
SWITCH_1_ENABLE_PIN = "GP5"
SWITCH_1_OVERRIDE_SCRIPT = false
""")
    config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-test",
            location="volatile location",
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-test",
                    label="volatile label",
                    override_script=True,
                ),
            ),
        )
    )
    payload = build_switch_meta_payload(config, settings_root=tmp_path)
    channel = payload["channels"][0]
    assert payload["location"] == "Saved location"
    assert channel["label"] == "Saved label"
    assert channel["pin"] == "GP28"
    assert channel["enable_pin"] == "GP5"
    assert channel["override_script"] is False
    assert channel["channel_id"] == "S1-test"


def test_offline_reconnect_replays_saved_edit_without_original_patch(tmp_path):
    from cpynodus_ii.features.steady_state import run_steady_state_iteration

    _saved(tmp_path)
    config = _config()
    transport = MQTTTransport()
    transport.mark_connected()
    first = run_steady_state_iteration(transport, config, None, settings_root=tmp_path)
    retained = {m.topic: m.payload for m in transport.published_messages if m.retain}
    transport.published_messages.clear()
    transport.subscriptions.clear()
    transport.mark_disconnected()
    # A local persisted edit while there is no subscriber/connection.
    _saved(tmp_path, 1719)
    transport.mark_connected()
    run_steady_state_iteration(
        transport, config, None, state=first.state, settings_root=tmp_path
    )
    retained.update(
        {m.topic: m.payload for m in transport.published_messages if m.retain}
    )
    for suffix in ("meta", "meta/config"):
        assert (
            retained["nodus/avpd-test/" + suffix]["sensor"]["calibration"]["Device"][
                "ALTITUDE_METERS"
            ]
            == 1719
        )
    assert "nodus/avpd-test/meta/patch" not in retained


@pytest.mark.parametrize("initial_state", [False, True])
def test_switch_command_refresh_reports_live_state_not_boot_state(
    tmp_path, initial_state
):
    from cpynodus_ii.core.config import SwitchChannelConfig, SwitchConfig
    from cpynodus_ii.features.steady_state import (
        SteadyState,
        run_steady_state_iteration,
    )

    (tmp_path / "switch.toml").write_text(
        "[Switch]\nSWITCH_1_LAST_STATE = {}\n".format(str(initial_state).lower())
    )
    config = RuntimeConfig(
        switch=SwitchConfig(
            present=True,
            device_id="switch-test",
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-test",
                    last_state=initial_state,
                ),
            ),
        )
    )
    pin = SimpleNamespace(value=initial_state)
    service = SimpleNamespace(
        phase="ready",
        device_id="switch-test",
        channels=(
            SimpleNamespace(
                key="SWITCH_1",
                channel_id="S1-test",
                phase="ready",
                control_handle=pin,
                enable_handle=SimpleNamespace(value=True),
                errors=(),
            ),
        ),
    )
    transport = MQTTTransport()
    transport.mark_connected()
    transport.receive(
        "nodus/S1-test/config/set",
        json.dumps(
            {
                "message_id": "switch-change",
                "payload": {
                    "updates": [
                        {
                            "section": "Switch",
                            "key": "SWITCH_1_LAST_STATE",
                            "value": not initial_state,
                            "name": "switch.toml",
                        }
                    ]
                },
            }
        ),
    )
    state = SteadyState(
        connection_generation=1,
        switch_meta_generation=1,
        last_availability_publish_at=0,
    )
    first = run_steady_state_iteration(
        transport,
        config,
        service,
        state=state,
        settings_root=tmp_path,
        now_monotonic=1,
    )
    assert first.command_results[0].persistence_mode == "persisted"
    assert pin.value is not initial_state
    # The switch handler deliberately leaves the boot config unchanged.
    assert config.switch.channels[0].last_state is initial_state
    transport.published_messages.clear()
    state = first.state
    for now in (2, 3, 4):
        transport.published_messages.clear()
        iteration = run_steady_state_iteration(
            transport,
            config,
            service,
            state=state,
            settings_root=tmp_path,
            now_monotonic=now,
        )
        state = iteration.state
        assert len(transport.published_messages) == 1
    meta = next(
        m for m in transport.published_messages if m.topic.endswith("/meta/switch")
    )
    assert meta.retain
    assert json.loads(meta.payload)["channels"][0]["state"] is not initial_state


def test_refresh_keeps_one_encoded_payload_until_queue_drains(tmp_path, monkeypatch):
    import cpynodus_ii.features.metadata_refresh as refresh

    _saved(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    calls = []
    builder = refresh.build_configuration_meta_payload

    def record(*args, **kwargs):
        calls.append("config")
        return builder(*args, **kwargs)

    monkeypatch.setattr(refresh, "build_configuration_meta_payload", record)
    config = _config()
    kwargs = dict(settings_root=tmp_path, version="test")
    first = refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    assert first.published_count == 1
    assert len(transport.published_messages) == 1
    queued = transport.published_messages[0]
    assert isinstance(queued.payload, bytes)
    assert b'": ' not in queued.payload
    # Both a slow send and a disconnected retry keep this exact buffer alive.
    for connected in (True, False, True):
        transport.connected = connected
        assert refresh_saved_metadata(transport, config, **kwargs).published_count == 0
        assert transport.published_messages == [queued]
        assert calls == []
    transport.published_messages.clear()
    assert refresh_saved_metadata(transport, config, **kwargs).published_count == 1
    assert calls == ["config"]
    assert len(transport.published_messages) == 1
    assert isinstance(transport.published_messages[0].payload, bytes)


def test_failed_later_snapshot_retries_that_topic_without_losing_work(
    tmp_path, monkeypatch
):
    import cpynodus_ii.features.metadata_refresh as refresh

    _saved(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    config = _config()
    kwargs = dict(settings_root=tmp_path, version="test")
    refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    transport.published_messages.clear()
    builder = refresh.build_configuration_meta_payload

    def fail(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(refresh, "build_configuration_meta_payload", fail)
    failed = refresh_saved_metadata(transport, config, **kwargs)
    assert failed.phase == "error"
    assert not transport.published_messages
    assert transport._metadata_refresh_pending
    monkeypatch.setattr(refresh, "build_configuration_meta_payload", builder)
    retried = refresh_saved_metadata(transport, config, **kwargs)
    assert retried.topics == ("nodus/avpd-test/meta/config",)
    assert not transport._metadata_refresh_pending


def test_new_edit_redirties_already_queued_snapshot(tmp_path):
    _saved(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    config = _config()
    kwargs = dict(settings_root=tmp_path, version="test")
    refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    old_message = transport.published_messages[0]
    _saved(tmp_path, 1719)
    refresh_saved_metadata(transport, config, (_result(),), **kwargs)
    assert transport.published_messages == [old_message]
    transport.published_messages.clear()
    refreshed = refresh_saved_metadata(transport, config, **kwargs)
    assert refreshed.topics == ("nodus/avpd-test/meta",)
    payload = json.loads(transport.published_messages[0].payload)
    assert payload["sensor"]["calibration"]["Device"]["ALTITUDE_METERS"] == 1719


def test_deferred_refresh_marks_dirty_without_building_or_collecting(
    tmp_path, monkeypatch
):
    import cpynodus_ii.features.metadata_refresh as refresh

    transport = MQTTTransport()
    transport.mark_connected()

    def forbidden(*args, **kwargs):
        pytest.fail("allocation work ran before the command coordinator unwound")

    monkeypatch.setattr(refresh, "build_runtime_meta_payload", forbidden)
    monkeypatch.setattr(refresh.gc, "collect", forbidden)
    result = refresh_saved_metadata(
        transport,
        _config(),
        (_result(),),
        settings_root=tmp_path,
        version="test",
        defer_build=True,
    )
    assert result.phase == "skipped"
    assert transport._metadata_refresh_pending
    assert not transport.published_messages
