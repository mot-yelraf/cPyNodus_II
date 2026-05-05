"""Lightweight OTA state helpers safe for normal Nodus runtime imports.

The normal firmware path imports this package only to recognize `/fwupdate`
MQTT commands and persist the small reboot handoff record. The heavier OTA
runtime and HTTP server stay in their submodules so MQTT, sensor, and switch
operation do not pay those memory costs until temporary OTA mode starts.
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
