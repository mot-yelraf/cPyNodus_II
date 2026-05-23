"""Persist bounded reboot diagnostics before requesting a soft reboot.

These helpers append fatal tracebacks and synthetic recovery-reboot records to
``/_reboot.log`` when the filesystem is writable, then keep the log trimmed to
the configured size cap.
"""

import os
import sys
import time

REBOOT_LOG_MAX_BYTES = 10 * 1024


def _stat_size(stat_result):
    if hasattr(stat_result, "st_size"):
        return int(getattr(stat_result, "st_size", 0) or 0)
    try:
        return int(stat_result[6] or 0)
    except Exception:
        return 0


def _trim_reboot_log(path, *, max_bytes=REBOOT_LOG_MAX_BYTES):
    """Keep only the newest log content when the reboot log grows too large."""
    try:
        stat_result = os.stat(path)
    except OSError:
        return False
    if _stat_size(stat_result) <= int(max_bytes or 0):
        return True
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except Exception:
        return False
    max_chars = max(0, int(max_bytes or 0))
    trimmed = content[-max_chars:]
    marker_index = trimmed.find("=== ")
    if marker_index > 0:
        trimmed = trimmed[marker_index:]
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(trimmed)
    except Exception:
        return False
    return True


def _datetime_stamp():
    try:
        current = time.localtime()
        if int(current[0]) < 2023:
            return "unknown"
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            int(current[0]),
            int(current[1]),
            int(current[2]),
            int(current[3]),
            int(current[4]),
            int(current[5]),
        )
    except Exception:
        return "unknown"


def _firmware_version():
    try:
        from cpynodus_ii import __version__

        return str(__version__ or "unknown")
    except Exception:
        return "unknown"


def _configured_device_id():
    for path, key in (
        ("sensor_i2c.toml", "SENSOR_ID"),
        ("sensor_soil.toml", "SENSOR_ID"),
        ("switch.toml", "DEVICE_ID"),
        ("settings.toml", "HOSTNAME"),
    ):
        value = _read_toml_scalar(path, key)
        if value:
            return value
    return "unknown"


def _read_toml_scalar(path, key):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = str(raw_line or "").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() != key:
                    continue
                return _clean_header_value(value)
    except Exception:
        pass
    return ""


def _clean_header_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    text = text.replace("|", "_").replace("\n", " ").replace("\r", " ").strip()
    return text or "unknown"


def _entry_header(header, device_id=None):
    return "{} | ts={} | version={} | device={}".format(
        str(header or "fatal traceback"),
        _datetime_stamp(),
        _firmware_version(),
        _clean_header_value(device_id) if device_id else _configured_device_id(),
    )


def append_reboot_traceback(
    exc,
    *,
    path="/_reboot.log",
    header="fatal traceback",
    device_id=None,
    max_bytes=REBOOT_LOG_MAX_BYTES,
):
    """Append a traceback entry and return True when the write succeeds."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("=== {} ===\n".format(_entry_header(header, device_id)))
            print_exception = getattr(sys, "print_exception", None)
            if callable(print_exception):
                print_exception(exc, handle)
            else:
                import traceback

                traceback.print_exception(
                    type(exc), exc, exc.__traceback__, file=handle
                )
            handle.write("\n")
        _trim_reboot_log(path, max_bytes=max_bytes)
        return True
    except Exception:
        return False


def append_reboot_reason_traceback(
    reason,
    *,
    path="/_reboot.log",
    header="recovery soft reboot",
    device_id=None,
    max_bytes=REBOOT_LOG_MAX_BYTES,
):
    """Append a synthetic traceback for a recovery-triggered soft reboot."""
    try:
        raise RuntimeError("soft reboot requested: {}".format(str(reason or "unknown")))
    except RuntimeError as exc:
        return append_reboot_traceback(
            exc,
            path=path,
            header=header,
            device_id=device_id,
            max_bytes=max_bytes,
        )
