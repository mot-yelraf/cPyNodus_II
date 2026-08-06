"""Configure filesystem and USB access before ``code.py`` starts.

This boot hook uses a board-specific guard pin to choose between a
runtime/debug mode and a host-edit mode. Runtime mode enables application
writes while hiding the USB mass-storage drive. Edit mode exposes
``CIRCUITPY`` to the host and keeps the application filesystem read-only so
files can be updated safely.
"""

import time

import board
import digitalio
import microcontroller
import storage
import supervisor
import usb_cdc

# ---------- user-configurable pins ----------
RW_GUARD_PIN_NAME = "GP14"  # default Pico2 W guard, low = app R/W + REPL
XESP32S3_RW_GUARD_PIN_NAME = "D8"
ENABLE_USB_DATA_CDC = True  # Pico2 W keeps the secondary CDC channel.
DISABLE_RUNTIME_AUTORELOAD = True

_boot_warnings = []


def _warn(msg):
    _boot_warnings.append(msg)


def _stamp():
    try:
        now = time.localtime()
    except Exception:
        now = None
    if now is not None:
        try:
            if int(now[0]) >= 2023:
                return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                    int(now[0]),
                    int(now[1]),
                    int(now[2]),
                    int(now[3]),
                    int(now[4]),
                    int(now[5]),
                )
        except Exception:
            pass
    try:
        elapsed = max(0, int(time.monotonic()))
    except Exception:
        elapsed = 0
    return "{}s".format(elapsed)


def _disable_auto_reload():
    runtime = getattr(supervisor, "runtime", None)
    if runtime is not None:
        try:
            runtime.autoreload = False
            return True
        except Exception:
            pass
    disable = getattr(supervisor, "disable_autoreload", None)
    if callable(disable):
        try:
            disable()
            return True
        except Exception:
            pass
    return False


def _is_xesp32s3_board():
    board_id = str(getattr(board, "board_id", "") or "").strip().lower()
    normalized_board_id = board_id.replace("-", "_")
    if (
        "xiao_esp32_s3" in normalized_board_id
        or "xiao_esp32s3" in normalized_board_id
    ):
        return True
    return (
        hasattr(board, "SDA")
        and hasattr(board, "SCL")
        and hasattr(board, "D0")
        and not hasattr(board, "GP0")
    )


def _resolve_rw_guard_pin_name():
    if _is_xesp32s3_board():
        return XESP32S3_RW_GUARD_PIN_NAME
    return RW_GUARD_PIN_NAME


# ---------- read the guard pin ----------
guard_pin = None
is_guard_low = False
active_guard_pin_name = _resolve_rw_guard_pin_name()
active_is_xesp32s3_board = _is_xesp32s3_board()
active_usb_data_cdc = ENABLE_USB_DATA_CDC and not active_is_xesp32s3_board
try:
    guard = getattr(board, active_guard_pin_name)
    guard_pin = digitalio.DigitalInOut(guard)
    guard_pin.direction = digitalio.Direction.INPUT
    guard_pin.pull = digitalio.Pull.UP
    is_guard_low = guard_pin.value is False  # low when grounded
except Exception as exc:
    # Fail safe to edit mode so CIRCUITPY remains visible for recovery.
    _warn(
        "Guard pin {pin} setup failed; using edit mode: {err}".format(
            pin=active_guard_pin_name,
            err=exc,
        )
    )

# ---------- remember intent in NVM ----------
# 1 = writable by app, 0 = read-only for app
desired_nvm_flag = 1 if is_guard_low else 0
try:
    if microcontroller.nvm[0] != desired_nvm_flag:
        microcontroller.nvm[0] = desired_nvm_flag
except Exception as exc:
    _warn("NVM update failed: {err}".format(err=exc))

# Clear app-level reboot markers only on true power events. Runtime recovery can
# use these bytes to avoid repeated reset loops across warm restarts.
try:
    reset_reason = getattr(getattr(microcontroller, "cpu", None), "reset_reason", None)
    reset_text = "" if reset_reason is None else str(reset_reason).strip().lower()
    if (
        ("power_on" in reset_text)
        or ("power" in reset_text)
        or ("brownout" in reset_text)
    ):
        for marker_index in (1, 2, 3, 4):
            if (
                len(microcontroller.nvm) > marker_index
                and microcontroller.nvm[marker_index] != 0
            ):
                microcontroller.nvm[marker_index] = 0
except Exception as exc:
    _warn("cold-boot bounce marker reset failed: {err}".format(err=exc))

# ---------- configure USB + filesystem mode ----------
if is_guard_low:
    # Debug/run mode: allow app writes, keep REPL, but disable mass storage.
    try:
        storage.disable_usb_drive()
    except Exception as exc:
        _warn("disable_usb_drive failed: {err}".format(err=exc))
    usb_cdc.enable(console=True, data=active_usb_data_cdc)
    try:
        storage.remount("/", readonly=False)
    except Exception as exc:
        _warn("remount rw failed: {err}".format(err=exc))
else:
    # Edit mode: expose CIRCUITPY to the host; app should treat FS as read-only.
    usb_cdc.enable(console=True, data=active_usb_data_cdc)
    try:
        storage.remount("/", readonly=True)
    except Exception as exc:
        _warn("remount ro failed: {err}".format(err=exc))

if DISABLE_RUNTIME_AUTORELOAD:
    _disable_auto_reload()

if guard_pin is not None:
    try:
        guard_pin.deinit()
    except Exception as exc:
        _warn("guard pin deinit failed: {err}".format(err=exc))

if _boot_warnings:
    for msg in _boot_warnings:
        print("{} [boot.py] {msg}".format(_stamp(), msg=msg))
