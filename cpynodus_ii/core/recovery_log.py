"""Persist bounded recovery diagnostics for post-mortem review."""

from cpynodus_ii.core.reboot_log import (
    REBOOT_LOG_MAX_BYTES,
    _entry_header,
    _trim_reboot_log,
)

RECOVERY_LOG_MAX_BYTES = REBOOT_LOG_MAX_BYTES


def append_recovery_event(
    event,
    detail="",
    *,
    path="/_recovery.log",
    device_id=None,
    max_bytes=RECOVERY_LOG_MAX_BYTES,
):
    """Append one recovery event and return True when the write succeeds."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                "=== {} ===\n".format(
                    _entry_header(
                        "recovery event: {}".format(str(event or "unknown")),
                        device_id,
                    )
                )
            )
            if detail:
                handle.write("{}\n".format(str(detail or "")))
            handle.write("\n")
        _trim_reboot_log(path, max_bytes=max_bytes)
        return True
    except Exception:
        return False
