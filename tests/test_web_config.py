"""Tests for web-driven configuration updates and switch overrides."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.config import RuntimeConfig
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.web_config import (
    apply_web_config_updates,
    apply_web_switch_override,
    classify_web_update,
)


def _load_runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
    return runtime_config


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
                control_handle=_Handle(False),
                enable_handle=_Handle(True),
                errors=(),
            ),
        ),
    )


def test_classify_web_update_marks_operational_changes_as_live():
    decision = classify_web_update(
        {"section": "Display", "key": "METRIC_1", "value": "CO2"}
    )
    npk_decision = classify_web_update(
        {"section": "NPK", "key": "N_TARGET", "value": 20.0}
    )

    assert decision.accepted is True
    assert decision.applies_live is True
    assert decision.requires_restart is False
    assert npk_decision.accepted is True
    assert npk_decision.applies_live is True
    assert npk_decision.requires_restart is False


def test_classify_web_update_marks_network_changes_as_restart_required():
    decision = classify_web_update(
        {"section": "Network", "key": "SSID", "value": "NewWiFi"}
    )
    ap_channel_decision = classify_web_update(
        {"section": "Network", "key": "AP_CHANNEL", "value": 11}
    )

    assert decision.accepted is True
    assert decision.applies_live is False
    assert decision.requires_restart is True
    assert ap_channel_decision.accepted is True
    assert ap_channel_decision.requires_restart is True


def test_apply_web_config_updates_applies_live_display_location_and_switch_label():
    runtime_config = _load_runtime_config()

    result = apply_web_config_updates(
        runtime_config,
        (
            {"section": "Sensor", "key": "LOCATION", "value": "Bench B"},
            {"section": "Switch", "key": "SWITCH_1_LABEL", "value": "Exhaust"},
            {"section": "Display", "key": "METRIC_1", "value": "CO2"},
            {"section": "Display.Style", "key": "METRIC_1", "value": "Gauge"},
            {"section": "Calibration.Device", "key": "CO2_OFFSET", "value": -125.0},
            {
                "section": "Calibration.Device",
                "key": "ALTITUDE_METERS",
                "value": 1609.3,
            },
        ),
    )

    assert result.errors == ()
    assert len(result.live_updates) == 6
    assert result.restart_required_updates == ()
    assert result.runtime_config.sensor.location == "Bench B"
    assert result.runtime_config.switch.channels[0].label == "Exhaust"
    assert result.runtime_config.sensor.display.metrics[0] == "CO2"
    assert result.runtime_config.sensor.display.styles[0] == "Gauge"
    assert result.runtime_config.sensor.calibration_device.co2_offset == -125.0
    assert result.runtime_config.sensor.calibration_device.altitude_meters == 1609.3


def test_apply_web_config_updates_persists_restart_fields_without_live_change():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

        result = apply_web_config_updates(
            runtime_config,
            (
                {"section": "Network", "key": "SSID", "value": "NewWiFi"},
                {"section": "MQTT", "key": "BROKER", "value": "new-broker.local"},
                {"section": "MQTT", "key": "BROKER_IP", "value": "10.0.0.248"},
            ),
            settings_root=tmpdir_path,
        )
        persisted = Settings.from_directory(tmpdir_path).runtime_config()

    assert result.errors == ()
    assert result.live_updates == ()
    assert len(result.restart_required_updates) == 3
    assert result.runtime_config.network.ssid == runtime_config.network.ssid
    assert result.runtime_config.mqtt.broker == runtime_config.mqtt.broker
    assert persisted.network.ssid == "NewWiFi"
    assert persisted.mqtt.broker == "new-broker.local"
    assert persisted.mqtt.broker_ip == "10.0.0.248"


def test_apply_web_switch_override_updates_runtime_last_state():
    runtime_config = _load_runtime_config()

    updated_runtime, apply_result = apply_web_switch_override(
        runtime_config,
        _switch_service(),
        channel_id="S1-x943fm",
        state=True,
    )

    assert apply_result.phase == "ready"
    assert updated_runtime.switch.channels[0].last_state is True


def test_apply_web_config_updates_rejects_unknown_updates():
    result = apply_web_config_updates(
        RuntimeConfig(),
        ({"section": "Unknown", "key": "FIELD", "value": "x"},),
    )

    assert result.applied_updates == ()
    assert "no_supported_updates" in result.errors
    assert result.ignored_updates[0]["reason"] == "unsupported_update"
