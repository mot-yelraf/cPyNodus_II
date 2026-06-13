"""Define board-specific pin defaults for supported Nodus targets."""

import os

PICO2W_PROFILE = "pico2w"
XESP32S3_PROFILE = "xesp32s3"
DEFAULT_PROFILE = PICO2W_PROFILE


class SwitchPinProfile:
    """Describe default switch enable/control pins for one channel."""

    def __init__(self, index=0, enable_pin="", control_pin="", label=""):
        self.index = int(index or 0)
        self.enable_pin = str(enable_pin or "")
        self.control_pin = str(control_pin or "")
        self.label = str(label or "")


class SoilChannelProfile:
    """Describe default RS485 UART pins for one soil channel."""

    def __init__(self, name="", tx_pin="", rx_pin=""):
        self.name = str(name or "")
        self.tx_pin = str(tx_pin or "")
        self.rx_pin = str(rx_pin or "")


class BoardProfile:
    """Describe board-specific defaults used during boot and bootstrap."""

    def __init__(
        self,
        key="",
        display_name="",
        board_id_tokens=(),
        circuitpython_version="",
        rw_guard_pin="",
        factory_reset_pin="",
        i2c_pins=(),
        switch_pins=(),
        soil_channels=(),
    ):
        self.key = str(key or "")
        self.display_name = str(display_name or "")
        self.board_id_tokens = tuple(board_id_tokens or ())
        self.circuitpython_version = str(circuitpython_version or "")
        self.rw_guard_pin = str(rw_guard_pin or "")
        self.factory_reset_pin = str(factory_reset_pin or "")
        self.i2c_pins = tuple(i2c_pins or ())
        self.switch_pins = tuple(switch_pins or ())
        self.soil_channels = tuple(soil_channels or ())


BOARD_PROFILES = (
    BoardProfile(
        key=PICO2W_PROFILE,
        display_name="Raspberry Pi Pico2 W",
        board_id_tokens=("raspberry_pi_pico2_w", "pico2_w", "pico2w"),
        circuitpython_version="9.2.8",
        rw_guard_pin="GP14",
        factory_reset_pin="GP17",
        i2c_pins=(("GP1", "GP0"), ("GP3", "GP2")),
        switch_pins=(
            SwitchPinProfile(1, "GP5", "GP28", "Fan"),
            SwitchPinProfile(2, "GP10", "GP21", "Light"),
        ),
        soil_channels=(
            SoilChannelProfile("CH1", "GP0", "GP1"),
            SoilChannelProfile("CH2", "GP4", "GP5"),
        ),
    ),
    BoardProfile(
        key=XESP32S3_PROFILE,
        display_name="Seeed Studio XIAO ESP32-S3 Sense",
        board_id_tokens=(
            "seeed_xiao_esp32_s3_sense",
            "seeed_xiao_esp32s3_sense",
            "xiao_esp32_s3",
            "xiao_esp32s3",
        ),
        circuitpython_version="10.2.1",
        rw_guard_pin="D8",
        factory_reset_pin="",
        i2c_pins=(("SCL", "SDA"),),
        switch_pins=(
            SwitchPinProfile(1, "D0", "D1", "Fan"),
            SwitchPinProfile(2, "D2", "D3", "Light"),
        ),
        soil_channels=(),
    ),
)

_PROFILE_BY_KEY = {profile.key: profile for profile in BOARD_PROFILES}
_PROFILE_ALIASES = {
    "pico": PICO2W_PROFILE,
    "pico2": PICO2W_PROFILE,
    "pico2w": PICO2W_PROFILE,
    "pico2w-cp928": PICO2W_PROFILE,
    "pico2w-mpy": PICO2W_PROFILE,
    "raspberry-pi-pico2-w": PICO2W_PROFILE,
    "xesp32s3": XESP32S3_PROFILE,
    "xesp32s3-cp1021": XESP32S3_PROFILE,
    "xesp32s3-mpy": XESP32S3_PROFILE,
    "xiao": XESP32S3_PROFILE,
    "xiao-esp32s3": XESP32S3_PROFILE,
    "seeed-xiao-esp32-s3-sense": XESP32S3_PROFILE,
}


def normalize_board_profile_key(value):
    """Return the canonical profile key for a user or board-provided value."""
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return ""
    if text in _PROFILE_BY_KEY:
        return text
    return _PROFILE_ALIASES.get(text, "")


def get_board_profile(profile_key):
    """Return the configured board profile, defaulting to Pico2 W."""
    key = normalize_board_profile_key(profile_key) or DEFAULT_PROFILE
    return _PROFILE_BY_KEY.get(key, _PROFILE_BY_KEY[DEFAULT_PROFILE])


def selected_board_profile(board_module=None, profile_key=""):
    """Return the best board profile from explicit, env, or board identity."""
    explicit_key = normalize_board_profile_key(profile_key)
    if explicit_key:
        return _PROFILE_BY_KEY[explicit_key]

    env_key = normalize_board_profile_key(_env_profile_key())
    if env_key:
        return _PROFILE_BY_KEY[env_key]

    detected_key = detect_board_profile_key(board_module=board_module)
    return _PROFILE_BY_KEY.get(detected_key, _PROFILE_BY_KEY[DEFAULT_PROFILE])


def detect_board_profile_key(board_module=None):
    """Infer the board profile from CircuitPython's board module."""
    board_module = board_module or _try_import_module("board")
    if board_module is None:
        return DEFAULT_PROFILE

    board_id = str(getattr(board_module, "board_id", "") or "").strip().lower()
    normalized_board_id = board_id.replace("-", "_")
    for profile in BOARD_PROFILES:
        for token in profile.board_id_tokens:
            if str(token or "").lower() in normalized_board_id:
                return profile.key

    if hasattr(board_module, "GP0") and hasattr(board_module, "GP28"):
        return PICO2W_PROFILE
    if (
        hasattr(board_module, "SDA")
        and hasattr(board_module, "SCL")
        and hasattr(board_module, "D0")
        and not hasattr(board_module, "GP0")
    ):
        return XESP32S3_PROFILE
    return DEFAULT_PROFILE


def switch_pin_defaults(profile):
    """Return switch defaults as the legacy settings bootstrap mapping."""
    defaults = {}
    for channel in tuple(getattr(profile, "switch_pins", ()) or ()):
        defaults[int(channel.index)] = {
            "enable": channel.enable_pin,
            "control": channel.control_pin,
            "label": channel.label,
        }
    return defaults


def soil_channel_defaults(profile):
    """Return soil channel defaults as tuples used by the Modbus probe."""
    return tuple(
        (channel.name, channel.tx_pin, channel.rx_pin)
        for channel in tuple(getattr(profile, "soil_channels", ()) or ())
    )


def _env_profile_key():
    try:
        getenv = getattr(os, "getenv", None)
        if callable(getenv):
            return getenv("NODUS_BOARD_PROFILE", "")
    except Exception:
        pass
    return ""


def _try_import_module(module_name):
    try:
        return __import__(module_name)
    except ImportError:
        return None
