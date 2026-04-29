"""Configure filesystem and USB access before ``code.py`` starts.

This boot hook uses the GP14 guard pin to choose between a runtime/debug mode
and a host-edit mode. Runtime mode enables application writes while hiding the
USB mass-storage drive. Edit mode exposes ``CIRCUITPY`` to the host and keeps
the application filesystem read-only so files can be updated safely.
"""

import board
import digitalio
import microcontroller
import storage
import time
import usb_cdc

# ---------- user-configurable pins ----------
RW_GUARD_PIN_NAME = "GP14"  # pull to GND for app R/W + REPL (no USB drive)
ENABLE_USB_DATA_CDC = True  # keep secondary CDC channel behavior unchanged

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


# ---------- read the guard pin ----------
guard_pin = None
is_guard_low = False
try:
    guard = getattr(board, RW_GUARD_PIN_NAME)
    guard_pin = digitalio.DigitalInOut(guard)
    guard_pin.direction = digitalio.Direction.INPUT
    guard_pin.pull = digitalio.Pull.UP
    is_guard_low = guard_pin.value is False  # low when grounded
except Exception as exc:
    # Fail safe to edit mode so CIRCUITPY remains visible for recovery.
    _warn("Guard pin setup failed; using edit mode: {err}".format(err=exc))

# ---------- remember intent in NVM ----------
# 1 = writable by app, 0 = read-only for app
desired_nvm_flag = 1 if is_guard_low else 0
try:
    if microcontroller.nvm[0] != desired_nvm_flag:
        microcontroller.nvm[0] = desired_nvm_flag
except Exception as exc:
    _warn("NVM update failed: {err}".format(err=exc))

# Clear the app-level cold-boot bounce marker only on true power events.
try:
    reset_reason = getattr(getattr(microcontroller, "cpu", None), "reset_reason", None)
    reset_text = "" if reset_reason is None else str(reset_reason).strip().lower()
    if ("power_on" in reset_text) or ("power" in reset_text) or ("brownout" in reset_text):
        if len(microcontroller.nvm) > 1 and microcontroller.nvm[1] != 0:
            microcontroller.nvm[1] = 0
except Exception as exc:
    _warn("cold-boot bounce marker reset failed: {err}".format(err=exc))

# ---------- configure USB + filesystem mode ----------
if is_guard_low:
    # Debug/run mode: allow app writes, keep REPL, but disable mass storage.
    try:
        storage.disable_usb_drive()
    except Exception as exc:
        _warn("disable_usb_drive failed: {err}".format(err=exc))
    usb_cdc.enable(console=True, data=ENABLE_USB_DATA_CDC)
    try:
        storage.remount("/", readonly=False)
    except Exception as exc:
        _warn("remount rw failed: {err}".format(err=exc))
else:
    # Edit mode: expose CIRCUITPY to the host; app should treat FS as read-only.
    usb_cdc.enable(console=True, data=ENABLE_USB_DATA_CDC)
    try:
        storage.remount("/", readonly=True)
    except Exception as exc:
        _warn("remount ro failed: {err}".format(err=exc))

if guard_pin is not None:
    try:
        guard_pin.deinit()
    except Exception as exc:
        _warn("guard pin deinit failed: {err}".format(err=exc))

if _boot_warnings:
    for msg in _boot_warnings:
        print("{} [boot.py] {msg}".format(_stamp(), msg=msg))
