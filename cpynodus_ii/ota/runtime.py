"""Run the temporary HTTP-only OTA startup path.

OTA mode is entered after normal runtime receives a `/fwupdate` MQTT prepare
command and reboots. It reconnects Wi-Fi, starts only the small OTA HTTP server,
leaves MQTT and feature services stopped, and returns to the prior profile
after a successful commit and reboot.
"""

import asyncio
import gc
from dataclasses import dataclass, replace

from cpynodus_ii.core.network import build_network_stack
from cpynodus_ii.ota.http import OtaHttpController
from cpynodus_ii.ota.state import FwUpdateState, save_ota_state


@dataclass(frozen=True)
class OtaModeResult:
    """Summarize whether temporary OTA mode reached network and HTTP ready."""

    phase: str
    package_id: str
    prior_profile: str
    network_phase: str
    ip_address: str = ""
    http_phase: str = ""
    errors: tuple = ()


async def run_ota_mode(
    runtime_config,
    ota_state,
    *,
    settings_root=".",
    version="",
    network_builder=build_network_stack,
    server_module=None,
    reboot_callback=None,
    log_fn=None,
    sleep_fn=None,
    idle_s=0,
):
    """Enter temporary OTA mode without MQTT, sensors, switches, or web UI.

    The function marks the saved state `ready`, starts the OTA HTTP endpoints,
    then polls the server until `idle_s` expires or forever when `idle_s` is
    `None`. The reduced runtime keeps heap available for staged file transfer,
    verification, backup, apply, and reboot scheduling.
    """
    state = ota_state if isinstance(ota_state, FwUpdateState) else FwUpdateState()
    _log(
        log_fn,
        "ota",
        "phase=start version={} package={} prior_profile={}".format(
            version or "unknown",
            state.package_id or "none",
            state.prior_profile or "none",
        ),
    )
    _log_memory(log_fn, "startup")
    network_stack = network_builder(runtime_config)
    _log(
        log_fn,
        "ota",
        "phase=network network_phase={} ssid={} ipv4={}".format(
            getattr(network_stack, "phase", "") or "unknown",
            getattr(network_stack, "ssid", "") or "none",
            getattr(network_stack, "ip_address", "") or "none",
        ),
    )
    ready_state = replace(state, phase="ready")
    save_ota_state(ready_state, _ota_state_path(settings_root))
    _log_memory(log_fn, "ready")
    http = OtaHttpController(
        runtime_config,
        network_stack,
        ready_state,
        settings_root=settings_root,
        version=version,
        server_module=server_module,
        reboot_callback=reboot_callback,
        log_fn=log_fn,
    ).start()
    _log(
        log_fn,
        "ota",
        "phase=http http_phase={} routes={} errors={}".format(
            http.phase,
            ",".join(http.route_paths) if http.route_paths else "none",
            ",".join(http.errors) if http.errors else "none",
        ),
    )
    _log(
        log_fn,
        "ota",
        "phase=ready package={} prior_profile={}".format(
            ready_state.package_id or "none",
            ready_state.prior_profile or "none",
        ),
    )
    if idle_s is None:
        sleep = sleep_fn or asyncio.sleep
        while True:
            http.poll()
            await sleep(1)
    if float(idle_s or 0) > 0:
        sleep = sleep_fn or asyncio.sleep
        deadline = float(idle_s)
        elapsed = 0.0
        while elapsed < deadline:
            http.poll()
            await sleep(1)
            elapsed += 1.0
    return OtaModeResult(
        phase="ready",
        package_id=ready_state.package_id,
        prior_profile=ready_state.prior_profile,
        network_phase=getattr(network_stack, "phase", "") or "",
        ip_address=getattr(network_stack, "ip_address", "") or "",
        http_phase=http.phase,
        errors=tuple(getattr(network_stack, "errors", ()) or ()) + tuple(http.errors),
    )


def _ota_state_path(root):
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _log(log_fn, prefix, message):
    if callable(log_fn):
        log_fn(prefix, message)


def _log_memory(log_fn, phase):
    try:
        gc.collect()
    except Exception:
        pass
    free_mem = "unknown"
    mem_alloc = "unknown"
    try:
        free_mem = str(gc.mem_free())
    except Exception:
        pass
    try:
        mem_alloc = str(gc.mem_alloc())
    except Exception:
        pass
    _log(
        log_fn,
        "memory",
        "phase=ota_{} free_mem={} mem_alloc={}".format(
            phase,
            free_mem,
            mem_alloc,
        ),
    )
