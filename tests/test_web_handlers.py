from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.web_handlers import (
    build_setup_payload,
    build_status_payload,
    handle_switch_state_request,
    handle_web_config_request,
)


def _runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        return Settings.from_directory(tmpdir_path).runtime_config()


def _sensor_service():
    return SimpleNamespace(
        phase="ready",
        device="aqi",
        interface="i2c",
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
                control_handle=_Handle(True),
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


def test_build_status_payload_includes_display_metrics_and_switch_state():
    payload = build_status_payload(
        _runtime_config(),
        version="v0.26.112.14",
        sensor_service=_sensor_service(),
        switch_service=_switch_service(),
        ip_address="10.0.0.44",
    )

    assert payload["schema"] == "nodus-web-status/v1"
    assert payload["network"]["ipv4addr"] == "10.0.0.44"
    assert payload["sensor"]["snapshot"]["phase"] == "ready"
    assert payload["sensor"]["display_metrics"][0]["metric"] == "Air Quality"
    assert payload["switch"]["channels"][0]["state"] is True


def test_build_setup_payload_lists_routes_and_current_values():
    payload = build_setup_payload(_runtime_config(), version="v0.26.112.14")

    assert payload["schema"] == "nodus-setup/v1"
    assert payload["network"]["hostname"] == "aqi-x943fm"
    assert payload["sensor"]["display_metrics"][0] == "Air Quality"
    assert any(route["path"] == "/setup" for route in payload["routes"])


def test_handle_web_config_request_reports_live_and_restart_required_updates():
    runtime_config = _runtime_config()

    payload = handle_web_config_request(
        runtime_config,
        (
            {"section": "Sensor", "key": "LOCATION", "value": "Bench C"},
            {"section": "Network", "key": "SSID", "value": "NewWiFi"},
        ),
    )

    assert payload["success"] is True
    assert payload["live_updated"] == 1
    assert payload["restart_required"] is True
    assert payload["runtime_config"].sensor.location == "Bench C"


def test_handle_switch_state_request_persists_last_state_when_requested():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()

        payload = handle_switch_state_request(
            runtime_config,
            _switch_service(),
            channel_id="S2-x943fm",
            state=True,
            settings_root=tmpdir_path,
        )
        persisted = Settings.from_directory(tmpdir_path).runtime_config()

    assert payload["success"] is True
    assert payload["channel_key"] == "SWITCH_2"
    assert payload["state"] is True
    assert payload["persistence_mode"] == "persisted"
    assert persisted.switch.channels[1].last_state is True
