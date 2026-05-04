"""Device-compatible helpers for Nodus OTA update state.

Normal runtime imports this package for lightweight state helpers only. Import
OTA HTTP/runtime modules directly from their submodules when entering OTA mode.
"""

from cpynodus_ii.ota.state import (
    FwUpdateState,
    build_fwupdate_topic,
    clear_ota_state,
    load_ota_state,
    save_ota_state,
)

__all__ = [
    "FwUpdateState",
    "build_fwupdate_topic",
    "clear_ota_state",
    "load_ota_state",
    "save_ota_state",
]
