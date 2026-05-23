"""Persist the small reboot handoff record used by OTA mode.

Nodus receives the `/fwupdate` MQTT prepare command during normal operation,
writes this state record, then reboots into a temporary HTTP-only OTA runtime.
The record is intentionally compact so it can be read and rewritten on
CircuitPython without allocating large JSON structures.
"""

import json
import os
from dataclasses import dataclass

OTA_STATE_DIR = "/_ota"
OTA_STATE_FILE = "/_ota/state.json"
FWUPDATE_TOPIC_SUFFIX = "fwupdate"


@dataclass(frozen=True)
class FwUpdateState:
    """Describe the OTA handoff state that survives reboot.

    `prior_profile` lets normal runtime resume the same profile after a
    successful update. `package_id` binds the MQTT prepare request to the
    HTTP transfer, and `phase` records whether OTA is requested, ready,
    staging, applied, aborted, or invalid.
    """

    mode: str = "ota"
    prior_profile: str = ""
    package_id: str = ""
    phase: str = "requested"
    error: str = ""

    def to_dict(self):
        """Return a compact JSON-serializable state dictionary."""
        data = {
            "mode": str(self.mode or "ota"),
            "prior_profile": str(self.prior_profile or ""),
            "package_id": str(self.package_id or ""),
            "phase": str(self.phase or "requested"),
        }
        if self.error:
            data["error"] = str(self.error)
        return data


def build_fwupdate_topic(device_id, base_topic="nodus"):
    """Return the per-device MQTT topic used to request OTA prepare mode."""
    base = _clean_topic_part(base_topic) or "nodus"
    device = _clean_topic_part(device_id)
    if not device:
        return ""
    return "{}/{}/{}".format(base, device, FWUPDATE_TOPIC_SUFFIX)


def load_ota_state(path=OTA_STATE_FILE):
    """Load OTA handoff state, returning ``None`` when no request exists."""
    try:
        with open(path, "r") as handle:
            document = json.load(handle)
    except OSError:
        return None
    except ValueError:
        return FwUpdateState(phase="invalid", error="state_json_invalid")
    if not isinstance(document, dict):
        return FwUpdateState(phase="invalid", error="state_shape_invalid")
    return FwUpdateState(
        mode=str(document.get("mode", "ota") or "ota"),
        prior_profile=str(document.get("prior_profile", "") or ""),
        package_id=str(document.get("package_id", "") or ""),
        phase=str(document.get("phase", "requested") or "requested"),
        error=str(document.get("error", "") or ""),
    )


def save_ota_state(state, path=OTA_STATE_FILE):
    """Persist OTA handoff state with a best-effort atomic replace."""
    state_obj = _coerce_state(state)
    parent = _parent_dir(path)
    if parent:
        _ensure_dir(parent)
    tmp_path = "{}.tmp".format(path)
    payload = json.dumps(state_obj.to_dict(), separators=(",", ":"))
    with open(tmp_path, "w") as handle:
        handle.write(payload)
        handle.write("\n")
    try:
        os.rename(tmp_path, path)
    except OSError:
        clear_ota_state(path)
        os.rename(tmp_path, path)
    return state_obj


def clear_ota_state(path=OTA_STATE_FILE):
    """Remove the OTA handoff record if it exists."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _coerce_state(state):
    if isinstance(state, FwUpdateState):
        return state
    if isinstance(state, dict):
        return FwUpdateState(
            mode=str(state.get("mode", "ota") or "ota"),
            prior_profile=str(state.get("prior_profile", "") or ""),
            package_id=str(state.get("package_id", "") or ""),
            phase=str(state.get("phase", "requested") or "requested"),
            error=str(state.get("error", "") or ""),
        )
    return FwUpdateState()


def _clean_topic_part(value):
    text = str(value or "").strip().strip("/")
    if "/" in text:
        return ""
    return text


def _parent_dir(path):
    text = str(path or "")
    index = text.rfind("/")
    if index <= 0:
        return ""
    return text[:index]


def _ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass
