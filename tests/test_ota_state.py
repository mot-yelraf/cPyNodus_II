"""Tests for device-compatible OTA state helpers."""

from cpynodus_ii.ota.state import (
    FwUpdateState,
    build_fwupdate_topic,
    clear_ota_state,
    load_ota_state,
    save_ota_state,
)


def test_build_fwupdate_topic_uses_device_scoped_topic():
    assert build_fwupdate_topic("co2-ykdvea") == "nodus/co2-ykdvea/fwupdate"
    assert build_fwupdate_topic("co2-ykdvea", "greenhouse") == (
        "greenhouse/co2-ykdvea/fwupdate"
    )


def test_build_fwupdate_topic_rejects_nested_device_id():
    assert build_fwupdate_topic("co2/bad") == ""


def test_save_and_load_ota_state_round_trip(tmp_path):
    path = tmp_path / "_ota" / "state.json"
    state = FwUpdateState(
        prior_profile="sensorius",
        package_id="ota-tagA-to-tagB",
        session_id="s" * 32,
        manifest_sha256="a" * 64,
        key_id="test-key",
        phase="requested",
    )

    saved = save_ota_state(state, str(path))
    loaded = load_ota_state(str(path))

    assert saved == state
    assert loaded == state


def test_clear_ota_state_removes_existing_state(tmp_path):
    path = tmp_path / "_ota" / "state.json"
    save_ota_state({"prior_profile": "homeassistant"}, str(path))

    assert clear_ota_state(str(path)) is True
    assert load_ota_state(str(path)) is None


def test_load_ota_state_reports_invalid_json(tmp_path):
    path = tmp_path / "_ota" / "state.json"
    path.parent.mkdir()
    path.write_text("{bad json", encoding="utf-8")

    state = load_ota_state(str(path))

    assert state.phase == "invalid"
    assert state.error == "state_json_invalid"
