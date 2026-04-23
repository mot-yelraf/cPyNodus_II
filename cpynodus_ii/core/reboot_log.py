"""Helpers for persisting traceback records before a soft reboot."""

import os
import sys

REBOOT_LOG_MAX_BYTES = 10 * 1024


def _trim_reboot_log(path, *, max_bytes=REBOOT_LOG_MAX_BYTES):
    """Keep only the newest log content when the reboot log grows too large."""
    try:
        stat_result = os.stat(path)
    except OSError:
        return False
    if int(getattr(stat_result, "st_size", 0) or 0) <= int(max_bytes or 0):
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


def append_reboot_traceback(
    exc,
    *,
    path="/_reboot.log",
    header="fatal traceback",
    max_bytes=REBOOT_LOG_MAX_BYTES,
):
    """Append a traceback entry and return True when the write succeeds."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("=== {} ===\n".format(str(header or "fatal traceback")))
            print_exception = getattr(sys, "print_exception", None)
            if callable(print_exception):
                print_exception(exc, handle)
            else:
                import traceback

                traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)
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
            max_bytes=max_bytes,
        )
