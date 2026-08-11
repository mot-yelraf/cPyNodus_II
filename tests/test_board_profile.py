"""Tests for board profile detection and normalized defaults.

The cases model supported boards and unknown hardware so pin, target, and
configuration defaults remain predictable.
"""

from types import SimpleNamespace

from cpynodus_ii.core.board_profile import (
    PICO2W_PROFILE,
    XESP32S3_PROFILE,
    BoardProfile,
    SoilChannelProfile,
    SwitchPinProfile,
    detect_board_profile_key,
    get_board_profile,
    selected_board_profile,
    soil_channel_defaults,
    switch_pin_defaults,
)


def test_board_profile_models_do_not_depend_on_dataclass_fields():
    switch = SwitchPinProfile(1, "EN", "OUT", "Label")
    soil = SoilChannelProfile("CH", "TX", "RX")
    profile = BoardProfile(key="test", switch_pins=(switch,), soil_channels=(soil,))

    assert hasattr(SwitchPinProfile, "__dataclass_fields__") is False
    assert switch.control_pin == "OUT"
    assert soil.rx_pin == "RX"
    assert profile.key == "test"
    assert profile.switch_pins == (switch,)


def test_unknown_board_defaults_to_pico2w_profile():
    profile = selected_board_profile(board_module=SimpleNamespace())

    assert profile.key == PICO2W_PROFILE
    assert profile.rw_guard_pin == "GP14"
    assert profile.factory_reset_pin == "GP17"


def test_detects_pico2w_from_gp_pin_shape():
    key = detect_board_profile_key(
        board_module=SimpleNamespace(GP0="gp0", GP28="gp28")
    )

    assert key == PICO2W_PROFILE


def test_detects_xiao_esp32s3_from_board_id():
    key = detect_board_profile_key(
        board_module=SimpleNamespace(board_id="seeed_xiao_esp32_s3_sense")
    )

    assert key == XESP32S3_PROFILE


def test_detects_xiao_esp32s3_from_pin_shape():
    key = detect_board_profile_key(
        board_module=SimpleNamespace(SDA="sda", SCL="scl", D0="d0")
    )

    assert key == XESP32S3_PROFILE


def test_xesp32s3_alias_returns_xiao_profile_defaults():
    profile = get_board_profile("xesp32s3-mpy")

    assert profile.key == XESP32S3_PROFILE
    assert profile.circuitpython_version == "10.2.1"
    assert profile.i2c_pins == (("SCL", "SDA"),)
    assert profile.factory_reset_pin == ""
    assert switch_pin_defaults(profile)[1]["enable"] == "D0"
    assert soil_channel_defaults(profile) == ()
