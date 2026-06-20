"""Tests for the CircuitPython boot hook."""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

BOOT_PATH = Path(__file__).resolve().parents[1] / "boot.py"


class _BoardProfileImportBlocker:
    def __init__(self):
        self.hit = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "cpynodus_ii.core.board_profile":
            self.hit = True
            raise ImportError("board_profile must not be imported by boot.py")
        return None


def _module(name, **values):
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _run_boot(monkeypatch, board_module, *, pin_values=None):
    state = {"pins": [], "remounts": [], "disable_usb_drive": 0, "usb_enable": []}
    pin_values = dict(pin_values or {})

    class _DigitalInOut:
        def __init__(self, pin):
            self.pin = pin
            self.direction = None
            self.pull = None
            self.value = pin_values.get(pin, True)
            state["pins"].append(pin)

        def deinit(self):
            return

    def _disable_usb_drive():
        state["disable_usb_drive"] += 1

    def _remount(path, *, readonly):
        state["remounts"].append((path, readonly))

    def _usb_enable(*, console, data):
        state["usb_enable"].append((console, data))

    digitalio_module = _module(
        "digitalio",
        DigitalInOut=_DigitalInOut,
        Direction=SimpleNamespace(INPUT="input"),
        Pull=SimpleNamespace(UP="up"),
    )
    storage_module = _module(
        "storage",
        disable_usb_drive=_disable_usb_drive,
        remount=_remount,
    )
    microcontroller_module = _module(
        "microcontroller",
        nvm=bytearray(4),
        cpu=SimpleNamespace(reset_reason="POWER_ON"),
    )
    supervisor_module = _module("supervisor", runtime=SimpleNamespace(autoreload=True))
    usb_cdc_module = _module("usb_cdc", enable=_usb_enable)

    monkeypatch.setitem(sys.modules, "board", board_module)
    monkeypatch.setitem(sys.modules, "digitalio", digitalio_module)
    monkeypatch.setitem(sys.modules, "storage", storage_module)
    monkeypatch.setitem(sys.modules, "microcontroller", microcontroller_module)
    monkeypatch.setitem(sys.modules, "supervisor", supervisor_module)
    monkeypatch.setitem(sys.modules, "usb_cdc", usb_cdc_module)
    monkeypatch.delitem(sys.modules, "cpynodus_ii.core.board_profile", raising=False)

    blocker = _BoardProfileImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        result = runpy.run_path(str(BOOT_PATH))
    finally:
        sys.meta_path.remove(blocker)
    return result, state, blocker


def test_boot_uses_pico_guard_pin_without_board_profile_import(monkeypatch):
    board_module = _module(
        "board",
        board_id="raspberry_pi_pico2_w",
        GP0="pin-gp0",
        GP14="pin-gp14",
        GP28="pin-gp28",
    )

    result, state, blocker = _run_boot(monkeypatch, board_module)

    assert blocker.hit is False
    assert result["active_guard_pin_name"] == "GP14"
    assert result["force_xesp32s3_edit_mode"] is False
    assert result["active_usb_data_cdc"] is True
    assert state["pins"] == ["pin-gp14"]
    assert state["usb_enable"] == [(True, True)]
    assert state["remounts"] == [("/", True)]


def test_boot_honors_pico_grounded_guard(monkeypatch):
    board_module = _module(
        "board",
        board_id="raspberry_pi_pico2_w",
        GP0="pin-gp0",
        GP14="pin-gp14",
        GP28="pin-gp28",
    )

    result, state, blocker = _run_boot(
        monkeypatch,
        board_module,
        pin_values={"pin-gp14": False},
    )

    assert blocker.hit is False
    assert result["active_guard_pin_name"] == "GP14"
    assert result["force_xesp32s3_edit_mode"] is False
    assert result["is_guard_low"] is True
    assert state["disable_usb_drive"] == 1
    assert state["usb_enable"] == [(True, True)]
    assert state["remounts"] == [("/", False)]


def test_boot_uses_xiao_guard_pin_without_board_profile_import(monkeypatch):
    board_module = _module(
        "board",
        board_id="seeed_xiao_esp32_s3_sense",
        D0="pin-d0",
        D8="pin-d8",
        SCL="pin-scl",
        SDA="pin-sda",
    )

    result, state, blocker = _run_boot(monkeypatch, board_module)

    assert blocker.hit is False
    assert result["active_guard_pin_name"] == "D8"
    assert result["force_xesp32s3_edit_mode"] is True
    assert result["active_usb_data_cdc"] is False
    assert state["pins"] == ["pin-d8"]
    assert state["usb_enable"] == [(True, False)]
    assert state["remounts"] == [("/", True)]


def test_boot_forces_xiao_edit_mode_when_guard_reads_low(monkeypatch):
    board_module = _module(
        "board",
        board_id="seeed_xiao_esp32_s3_sense",
        D0="pin-d0",
        D8="pin-d8",
        SCL="pin-scl",
        SDA="pin-sda",
    )

    result, state, blocker = _run_boot(
        monkeypatch,
        board_module,
        pin_values={"pin-d8": False},
    )

    assert blocker.hit is False
    assert result["active_guard_pin_name"] == "D8"
    assert result["force_xesp32s3_edit_mode"] is True
    assert result["is_guard_low"] is False
    assert result["desired_nvm_flag"] == 0
    assert state["disable_usb_drive"] == 0
    assert state["usb_enable"] == [(True, False)]
    assert state["remounts"] == [("/", True)]
