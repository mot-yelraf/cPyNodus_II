"""Device-compatible helpers for Nodus OTA update state."""

from cpynodus_ii.ota.http import OtaHttpController, build_ota_status_payload
from cpynodus_ii.ota.runtime import OtaModeResult, run_ota_mode
from cpynodus_ii.ota.state import (
    FwUpdateState,
    build_fwupdate_topic,
    clear_ota_state,
    load_ota_state,
    save_ota_state,
)

__all__ = [
    "FwUpdateState",
    "OtaHttpController",
    "OtaModeResult",
    "build_ota_status_payload",
    "build_fwupdate_topic",
    "clear_ota_state",
    "load_ota_state",
    "run_ota_mode",
    "save_ota_state",
]
