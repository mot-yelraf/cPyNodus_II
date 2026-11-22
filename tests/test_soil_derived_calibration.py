"""Exercise the per-probe derived calibration contract on the host.

Verify persisted and runtime state, command ordering, bounded metadata, and
calculation behavior under probe and filesystem faults.
"""

import json
import os
from pathlib import Path

import pytest

from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.mqtt_client import _mqtt_publish_packet_size
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.command_intake import process_inbound_messages
from cpynodus_ii.features.derived_metrics import enrich_metrics
from cpynodus_ii.features.metadata_refresh import refresh_saved_metadata
from cpynodus_ii.features.payloads import (
    build_configuration_meta_payload,
    build_runtime_meta_payload,
)
from cpynodus_ii.features.soil_sensor_service import _read_soil_snapshot_metrics
from cpynodus_ii.features.web_config import apply_web_soil_calibration

SOIL_FILE = "sensor_soil.toml"
ROOT = Path(__file__).parents[1]
TOPIC = "nodus/soil-test/calibration/set"
WET = "SPD_WET_THRESHOLD_PCT"
DRY = "SPD_DRY_THRESHOLD_PCT"
DEFICIT = {WET: 43, DRY: 23}
STRESS = {
    "SSI_TEMP_LOW_CRIT_C": 10,
    "SSI_TEMP_LOW_OK_C": 18,
    "SSI_TEMP_HIGH_OK_C": 24,
    "SSI_TEMP_HIGH_CRIT_C": 30,
    "SSI_MOISTURE_WEIGHT_PCT": 0,
    "SSI_TEMP_WEIGHT_PCT": 30,
}
NPK = {"N_TARGET": 20, "P_TARGET": 30, "K_TARGET": 70, "SFI_MIN_WEIGHT_PCT": 100}
READINGS = {
    "Soil Moisture": 28,
    "Soil Temperature": 15,
    "Soil Nitrogen": 10,
    "Soil Phosphorus": 30,
    "Soil Potassium": 70,
}


def _settings(root, channels=("CH1", "CH2"), second_variant="soil_4in1"):
    (root / "settings.toml").write_text((ROOT / "boards/settings.toml.def").read_text())
    template = ROOT / "boards/pico2w/templates/sensor_soil.toml.def"
    soil = template.read_text().replace('DEVICE = ""', 'DEVICE = "soil"')
    soil = soil.replace('SENSOR_ID = ""', 'SENSOR_ID = "soil-test"')
    soil = soil.replace('SOIL_VARIANT = "canonical"', 'SOIL_VARIANT = "soil_7in1"', 1)
    for channel in channels:
        soil = soil.replace(
            f'[Modbus.{channel}]\nUART_TX = ""\nUART_RX = ""',
            f'[Modbus.{channel}]\nUART_TX = "GP4"\nUART_RX = "GP5"',
        )
        start = soil.index(f"[Modbus.{channel}]")
        variant = second_variant if channel == "CH2" else "soil_7in1"
        soil = soil[:start] + soil[start:].replace(
            'SOIL_VARIANT = "canonical"', f'SOIL_VARIANT = "{variant}"', 1
        )
    (root / SOIL_FILE).write_text(soil)
    return _reload(root)


def _reload(root):
    return Settings.from_directory(root).runtime_config()


def _send(
    transport, config, root, group, key=WET, value=43, *, channel="CH1", **envelope
):
    transport.receive(
        TOPIC,
        json.dumps(
            {
                "action": "soil_derived_apply",
                "message_id": f"{channel}-{group}-{key}",
                "sensor_id": config.sensor.sensor_id,
                "name": SOIL_FILE,
                "payload": {
                    "section": f"{group}.{channel}", "key": key, "value": value
                },
                **envelope,
            }
        ),
    )
    (result,) = process_inbound_messages(transport, config, None, settings_root=root)
    return result


def test_groups_affect_only_the_target_probe_and_survive_reload(tmp_path):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    readings = {
        f"{channel} {key}": value
        for channel in ("CH1", "CH2")
        for key, value in READINGS.items()
    }
    before = enrich_metrics("soil", readings, runtime_config=config)
    assert before["CH1 Soil Moisture Deficit"] == 50.0
    assert before["CH1 Soil Stress Index"] == 65.0
    assert before["CH1 Soil Fertility Index"] == 67
    assert "CH2 Soil Fertility Index" not in before

    for group, values in (
        ("SoilDeficit", DEFICIT),
        ("SoilStress", STRESS),
        ("NPK", NPK),
    ):
        for key, value in values.items():
            transport.published_messages.clear()
            result = _send(transport, config, tmp_path, group, key, value)
            assert result.runtime_config is config
            assert result.persistence_mode == "persisted"
            assert [
                message.topic.split("/")[-1] for message in transport.published_messages
            ] == ["ack", "result", "patch"]
            ack, reply, patch = transport.published_messages
            assert not any(message.retain for message in (ack, reply, patch))
            assert ack.payload == {"message_id": result.message_id, "accepted": True}
            section = f"{group}.CH1"
            assert reply.payload == {
                "message_id": result.message_id,
                "sensor_id": "soil-test",
                "name": SOIL_FILE,
                "section": section,
                "key": key,
                "applied": True,
                "persistence_mode": "persisted",
                "updated": 1,
                "error": "",
            }
            assert patch.payload["message_id"] == result.message_id
            assert patch.payload["source"] == "calibration_set"
            assert patch.payload["sections"] == [section]
            assert patch.payload["updates"] == [{
                "sensor_id": "soil-test",
                "name": SOIL_FILE,
                "section": section,
                "key": key,
                "value": value,
            }]

    after = enrich_metrics("soil", readings, runtime_config=config)
    assert after["CH1 Soil Moisture Deficit"] == 75.0
    assert after["CH1 Soil Stress Index"] == 37.5
    assert after["CH1 Soil Fertility Index"] == 50
    assert {key: value for key, value in after.items() if key.startswith("CH2 ")} == {
        key: value for key, value in before.items() if key.startswith("CH2 ")
    }
    assert enrich_metrics("soil", readings, runtime_config=_reload(tmp_path)) == after


@pytest.mark.parametrize("channels, target", [((), "CH1"), (("CH2",), "CH2")])
def test_single_probe_discovery_and_unprefixed_calculation(tmp_path, channels, target):
    config = _settings(tmp_path, channels, second_variant="soil_7in1")
    assert (
        _send(
            MQTTTransport(),
            config,
            tmp_path,
            "SoilDeficit",
            channel=target,
        ).phase
        == "published"
    )
    companion = build_configuration_meta_payload(config, settings_root=tmp_path)
    discovered = companion["soil_derived"]["channels"]
    assert list(discovered) == [target]
    assert discovered[target]["metric_prefix"] == ""
    assert discovered[target]["groups"]["SoilDeficit"] == [43.0, 18.0]
    assert (
        enrich_metrics("soil", READINGS, runtime_config=config)["Soil Moisture Deficit"]
        == 60.0
    )


def test_dual_probe_with_missing_reading_keeps_physical_prefix(tmp_path, monkeypatch):
    from cpynodus_ii.features import soil_sensor_service

    config = _settings(tmp_path)
    readings = {"Soil Moisture": 28}
    monkeypatch.setattr(
        soil_sensor_service,
        "_read_soil_metrics",
        lambda transport, sensor, channel: readings if channel.name == "CH2" else {},
    )
    transport = tuple((channel, object()) for channel in config.sensor.modbus.channels)
    assert _read_soil_snapshot_metrics(transport, config.sensor) == {
        "CH2 Soil Moisture": 28
    }


@pytest.mark.parametrize(
    "group, key, value, channel, envelope, error",
    [
        ("SoilDeficit", WET, 43, "CH3", {}, "soil_channel_not_found"),
        ("SoilDeficit", WET, 43, "ch1", {}, "soil_channel_not_found"),
        ("SoilDeficit", WET, 43, "CH1", {"sensor_id": "other"},
         "invalid_soil_derived_target"),
        ("SoilDeficit", WET, 43, "CH1", {"name": "switch.toml"},
         "invalid_soil_derived_target"),
        ("Unknown", WET, 43, "CH1", {}, "unsupported_soil_group"),
        ("NPK", "N_TARGET", 20, "CH2", {}, "unsupported_soil_group"),
        ("NPK", "N_TARGET", 0, "CH1", {}, "invalid_soil_derived_values"),
        ("SoilDeficit", "UNKNOWN", 43, "CH1", {}, "invalid_soil_derived_values"),
        ("SoilStress", "SSI_TEMP_LOW_OK_C", 25, "CH1", {},
         "invalid_soil_derived_values"),
        *[
            ("SoilDeficit", DRY, value, "CH1", {}, "invalid_soil_derived_values")
            for value in (True, "20", float("nan"), float("inf"), -1, 38)
        ],
        *[
            ("SoilDeficit", WET, 43, "CH1", {"payload": payload},
             "invalid_soil_derived_values")
            for payload in (
                {"channel": "CH1", "group": "SoilDeficit", "values": DEFICIT},
                {"section": "SoilDeficit.CH1", "key": WET},
                {"section": "SoilDeficit.CH1", "key": WET, "value": 43, DRY: 23},
            )
        ],
    ],
)
def test_rejection_does_not_write_and_duplicate_replays_failure(
    tmp_path, group, key, value, channel, envelope, error
):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    before = (tmp_path / SOIL_FILE).read_text()
    result = _send(
        transport, config, tmp_path, group, key, value, channel=channel, **envelope
    )
    assert result.errors == (error,)
    assert len(transport.published_messages) == 2
    reply = transport.published_messages[-1].payload
    assert not reply["applied"]
    assert reply["updated"] == 0
    duplicate = _send(
        transport, config, tmp_path, "SoilDeficit", message_id=result.message_id,
    )
    assert duplicate.duplicate
    assert transport.published_messages[-1].payload == reply
    assert (tmp_path / SOIL_FILE).read_text() == before
    assert config.sensor.soil_derived_overrides is None


def test_partial_override_inherits_and_rejected_next_key_keeps_save(tmp_path):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    path = tmp_path / SOIL_FILE
    first = _send(transport, config, tmp_path, "SoilDeficit", WET, 31)
    assert first.phase == "published"
    saved = path.read_text()
    assert saved.rsplit("\n[SoilDeficit.CH1]\n", 1)[1].strip() == f"{WET} = 31.0"

    rejected = _send(transport, config, tmp_path, "SoilDeficit", DRY, 31)
    assert rejected.errors == ("invalid_soil_derived_values",)
    assert path.read_text() == saved
    for current in (config, _reload(tmp_path)):
        metadata = build_configuration_meta_payload(current)
        assert metadata["soil_derived"]["channels"]["CH1"]["groups"]["SoilDeficit"] == [
            31, 18
        ]

    path.write_text(saved.replace(f"{DRY} = 18.0", f"{DRY} = 20.0"))
    metadata = build_configuration_meta_payload(_reload(tmp_path))
    assert metadata["soil_derived"]["channels"]["CH1"]["groups"]["SoilDeficit"] == [
        31, 20
    ]


@pytest.mark.parametrize("fault", ["rename", "sync", "read_only", "no_writer"])
def test_failed_save_keeps_effective_and_saved_values(tmp_path, monkeypatch, fault):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    original = (tmp_path / SOIL_FILE).read_text()
    if fault == "rename":
        rename = os.rename

        def fail_install(source, target):
            if str(source).endswith(".tmp"):
                raise OSError(5, "injected install failure")
            rename(source, target)

        monkeypatch.setattr(os, "rename", fail_install)
    elif fault in ("sync", "read_only"):
        calls = []

        def fail_sync():
            calls.append(None)
            if fault == "read_only":
                raise OSError(30, "Read-only filesystem")
            if len(calls) == 3:
                raise OSError(5, "injected post-install sync failure")

        monkeypatch.setattr(os, "sync", fail_sync)
    settings_root = None if fault == "no_writer" else tmp_path
    result = _send(transport, config, settings_root, "SoilDeficit")
    assert result.phase == "error"
    assert result.published_count == 2
    assert config.sensor.soil_derived_overrides is None
    assert (tmp_path / SOIL_FILE).read_text() == original


def test_key_noop_duplicate_and_retained_refresh_after_reply_drain(tmp_path):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    transport.mark_connected()
    result = _send(transport, config, tmp_path, "SoilDeficit", WET, 38)
    assert result.phase == "published"
    assert transport.published_messages[-1].payload["updates"][0]["value"] == 38
    reply = transport.published_messages[-2].payload
    kwargs = {"settings_root": tmp_path, "version": "test"}
    assert (
        refresh_saved_metadata(transport, config, (result,), **kwargs).phase
        == "skipped"
    )
    transport.published_messages.clear()
    assert refresh_saved_metadata(transport, config, **kwargs).published_count == 1
    (message,) = transport.published_messages
    assert message.topic.endswith("/meta/config") and message.retain
    assert json.loads(message.payload)["soil_derived"]["channels"]["CH1"]["groups"][
        "SoilDeficit"
    ] == [38, 18]
    transport.published_messages.clear()
    duplicate = _send(transport, config, tmp_path, "SoilDeficit")
    assert duplicate.duplicate
    assert duplicate.published_count == 2
    assert transport.published_messages[-1].payload == reply
    assert (
        refresh_saved_metadata(transport, config, (duplicate,), **kwargs).phase
        == "skipped"
    )


def test_local_save_and_dual_7in1_metadata_packet_budget(tmp_path):
    config = _settings(tmp_path, second_variant="soil_7in1")
    error, patch = apply_web_soil_calibration(
        config,
        {"channel": "CH2", "group": "NPK", "values": NPK},
        settings_root=tmp_path,
        message_id="local-test",
    )
    assert not error
    assert patch["source"] == "calibration_local"
    compact = build_runtime_meta_payload(config, version="test", settings_root=tmp_path)
    assert "soil_derived" not in compact
    assert compact["sensor"]["config_file"] == SOIL_FILE
    companion = build_configuration_meta_payload(config, settings_root=tmp_path)
    discovered = companion["soil_derived"]["channels"]
    assert discovered["CH1"]["groups"]["NPK"] == [20, 30, 70, 50]
    assert discovered["CH2"]["groups"]["NPK"] == [20, 30, 70, 100]
    assert (
        _mqtt_publish_packet_size(
            compact["config_topic"],
            json.dumps(companion, separators=(",", ":")),
            qos=1,
        )
        <= 1460
    )


def test_retained_settings_come_from_disk_despite_volatile_shared_update(tmp_path):
    config = _settings(tmp_path, second_variant="soil_7in1")
    config.sensor.soil_npk.n_target = 999
    saved = build_configuration_meta_payload(config, settings_root=tmp_path)
    live = build_configuration_meta_payload(config)
    assert saved["soil_derived"]["channels"]["CH1"]["groups"]["NPK"][0] == 20
    assert live["soil_derived"]["channels"]["CH1"]["groups"]["NPK"][0] == 999


def test_partial_npk_override_tracks_live_shared_target_changes(tmp_path):
    config = _settings(tmp_path, ("CH1",))
    transport = MQTTTransport()
    assert _send(
        transport, config, tmp_path, "NPK", "SFI_MIN_WEIGHT_PCT", 100
    ).phase == "published"
    assert enrich_metrics("soil", READINGS, runtime_config=config)[
        "Soil Fertility Index"
    ] == 50

    transport.receive(
        "nodus/soil-test/config/set",
        json.dumps({"message_id": "shared-npk", "payload": {
            "updates": [{"section": "NPK", "key": "N_TARGET", "value": 40}]
        }}),
    )
    result, = process_inbound_messages(transport, config, None, settings_root=tmp_path)
    assert result.phase == "published"
    assert enrich_metrics("soil", READINGS, runtime_config=result.runtime_config)[
        "Soil Fertility Index"
    ] == 25
    metadata = build_configuration_meta_payload(result.runtime_config)
    assert metadata["soil_derived"]["channels"]["CH1"]["groups"]["NPK"] == [
        40, 30, 70, 100
    ]


def test_streaming_save_updates_existing_table_with_comment_and_no_final_newline(
    tmp_path,
):
    _settings(tmp_path)
    path = tmp_path / SOIL_FILE
    text = (
        path.read_text() + "\n[SoilDeficit.CH1] # bed one\nSPD_WET_THRESHOLD_PCT = 38"
    )
    path.write_text(text)
    config = _reload(tmp_path)
    result = _send(MQTTTransport(), config, tmp_path, "SoilDeficit", DRY, 23)
    assert result.phase == "published"
    assert path.read_text().count("\n[SoilDeficit.CH1]") == 1
    assert (
        _reload(tmp_path).sensor.soil_derived_overrides["CH1", "SoilDeficit"]["dry_pct"]
        == 23
    )


def test_derived_command_after_fallback_message_still_saves(tmp_path):
    config = _settings(tmp_path)
    transport = MQTTTransport()
    transport.receive("nodus/soil-test/unknown", "{}")
    assert (
        _send(transport, config, tmp_path, "SoilDeficit").phase == "published"
    )
