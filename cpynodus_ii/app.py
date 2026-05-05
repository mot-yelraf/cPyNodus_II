"""Coordinate startup, recovery, and steady-state runtime behavior.

This module wires together configuration loading, feature initialization,
network setup, MQTT lifecycle management, web runtime startup, and bounded
recovery decisions for the main cPyNodus_II application loop.
"""

import asyncio
import gc
import os
import time
from dataclasses import replace

from cpynodus_ii import __version__
from cpynodus_ii.core import (
    NTPState,
    RecoveryPolicy,
    RecoveryState,
    advance_recovery_state,
    build_mqtt_client_adapter,
    build_network_stack,
    connect_mqtt_client,
    disconnect_mqtt_client,
    maybe_sync_ntp,
    network_link_is_ready,
    poll_mqtt_client,
    preflight_mqtt_broker,
    reconnect_network_stack,
    refresh_network_stack,
    sync_transport_to_client,
)
from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.ntp import DEFAULT_NTP_SERVER
from cpynodus_ii.core.plan import StartupPlan
from cpynodus_ii.core.reboot_log import append_reboot_reason_traceback
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import (
    SteadyState,
    WebRuntimeController,
    build_sensor_runtime,
    build_switch_runtime,
    plan_sensor_initialization,
    plan_switch_initialization,
    read_sensor_snapshot,
    run_steady_state_iteration,
    start_sensor_service,
    start_switch_service,
)
from cpynodus_ii.hardware import bind_sensor_hardware, bind_switch_hardware
from cpynodus_ii.ota.state import FwUpdateState, load_ota_state, save_ota_state

_STARTUP_WARM_REBOOT_NVM_INDEX = 1
_STARTUP_WARM_REBOOT_MARKER = 1


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _seconds_stamp(start_monotonic):
    try:
        elapsed = max(0, int(time.monotonic() - float(start_monotonic)))
    except Exception:
        elapsed = 0
    return "{}s".format(elapsed)


def _datetime_stamp():
    try:
        current = time.localtime()
    except Exception:
        return ""
    try:
        if int(current[0]) < 2023:
            return ""
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            int(current[0]),
            int(current[1]),
            int(current[2]),
            int(current[3]),
            int(current[4]),
            int(current[5]),
        )
    except Exception:
        return ""


def _log_stamp(start_monotonic):
    stamp = _datetime_stamp()
    if stamp:
        return stamp
    return _seconds_stamp(start_monotonic)


def _print_log(prefix, message, *, start_monotonic):
    print("{} {} {}".format(_log_stamp(start_monotonic), prefix, message))


def _memory_summary():
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
    return "free_mem={} mem_alloc={}".format(free_mem, mem_alloc)


def _transport_queue_summary(transport):
    """Return compact queue depth counters for MQTT transport buffers."""
    try:
        published = len(getattr(transport, "published_messages", ()))
    except Exception:
        published = -1
    try:
        subscriptions = len(getattr(transport, "subscriptions", ()))
    except Exception:
        subscriptions = -1
    try:
        received = len(getattr(transport, "received_messages", ()))
    except Exception:
        received = -1
    return "queues pub={} sub={} rx={}".format(published, subscriptions, received)


def _mqtt_adapter_summary(adapter):
    """Return compact MQTT adapter state for diagnostics."""
    try:
        published_index = int(getattr(adapter, "published_index", 0) or 0)
    except Exception:
        published_index = -1
    try:
        subscription_index = int(getattr(adapter, "subscription_index", 0) or 0)
    except Exception:
        subscription_index = -1
    return "adapter pub_i={} sub_i={}".format(published_index, subscription_index)


def _mqtt_sync_summary(sync_result, transport, *, source, before_queues):
    """Return a compact MQTT sync diagnostic string."""
    errors = ",".join(sync_result.errors) if sync_result.errors else "none"
    return (
        "sync source={} phase={} published={} subscribed={} before={} after={} "
        "{} errors={}"
    ).format(
        str(source or "unknown"),
        sync_result.phase,
        sync_result.published_count,
        sync_result.subscribed_count,
        before_queues,
        _transport_queue_summary(transport),
        _mqtt_adapter_summary(sync_result.adapter),
        errors,
    )


def _sensor_error_text(*parts):
    """Return a compact sensor error string for startup and poll logs."""
    errors = []
    for part in parts:
        for error in tuple(getattr(part, "errors", ()) or ()):
            text = str(error or "").strip()
            if text and text not in errors:
                errors.append(text)
    return ",".join(errors) if errors else "none"


def _sensor_target_addr(sensor_runtime):
    """Return a compact sensor target address for startup logs."""
    target = str(getattr(sensor_runtime, "transport_target", "") or "").strip()
    if not target:
        return "none"
    if "@" in target:
        address = target.rsplit("@", 1)[-1].strip()
        if address:
            return address.lower() if address.lower().startswith("0x") else address
    return target


def _sensor_issue_text(errors):
    """Return sensor-specific poll errors, omitting normal skipped cadences."""
    issue_errors = []
    for error in tuple(errors or ()):
        text = str(error or "").strip()
        if not text or text == "sensor_poll_interval_not_elapsed":
            continue
        if text.startswith("sensor_") and text not in issue_errors:
            issue_errors.append(text)
    return ",".join(issue_errors) if issue_errors else ""


def _dns_health_text(network_stack, mqtt_adapter, ntp_state):
    """Return compact DNS health from existing MQTT and NTP resolver state."""
    if getattr(network_stack, "phase", "") != "ready":
        return "unavailable"
    if getattr(network_stack, "socket_pool", None) is None:
        return "unavailable"
    errors = tuple(getattr(mqtt_adapter, "errors", ()) or ()) + tuple(
        getattr(ntp_state, "errors", ()) or ()
    )
    for error in errors:
        text = str(error or "")
        if "resolve_failed" in text or "dns_unready" in text:
            return "error"
    return "ok"


def _ntp_health_text(ntp_state):
    """Return compact NTP health for periodic runtime logs."""
    phase = str(getattr(ntp_state, "phase", "") or "").strip()
    return phase or "idle"


def _is_recoverable_mqtt_poll_error(errors):
    """Return True when poll errors match known recoverable MiniMQTT noise."""
    for error in tuple(errors or ()):
        text = str(error or "").strip()
        if text.startswith("mqtt_poll_failed:minimqtt_socket:"):
            return True
    return False


def _collect_garbage():
    """Run best-effort garbage collection for constrained heap recovery."""
    try:
        gc.collect()
    except Exception:
        return False
    return True


def _log_memory_checkpoint(start_monotonic, phase):
    """Log a compact memory checkpoint for startup and diagnostics."""
    _print_log(
        "memory",
        "phase={} {}".format(str(phase or "unknown"), _memory_summary()),
        start_monotonic=start_monotonic,
    )


def _filesystem_mode_label(fs_writable):
    """Return a compact filesystem mode label for logs."""
    if fs_writable is True:
        return "RWFS"
    if fs_writable is False:
        return "ROFS"
    return "FS?"


def _should_preflight_broker(adapter):
    targets = tuple(getattr(adapter, "broker_targets", ()) or ())
    return len(targets) > 1


def _read_startup_warm_reboot_marker(nvm=None):
    """Return the once-per-power-cycle warm reboot marker."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= _STARTUP_WARM_REBOOT_NVM_INDEX:
            return -1
        return int(nvm[_STARTUP_WARM_REBOOT_NVM_INDEX] or 0)
    except Exception:
        return -1


def _mark_startup_warm_rebooted(nvm=None):
    """Mark that startup already performed its warm reboot."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= _STARTUP_WARM_REBOOT_NVM_INDEX:
            return False
        nvm[_STARTUP_WARM_REBOOT_NVM_INDEX] = _STARTUP_WARM_REBOOT_MARKER
        return True
    except Exception:
        return False


def _startup_warm_reboot_ready_reason(
    runtime_config,
    network_stack,
    mqtt_adapter,
    ntp_state,
    plan,
):
    """Return a clean startup marker that is good enough to warm reboot before MQTT."""
    if not getattr(plan, "mqtt_enabled", False):
        return "", ()
    if not getattr(runtime_config, "mqtt_enabled", False):
        return "", ()
    if getattr(network_stack, "phase", "") != "ready":
        return "", ("network_not_ready",)
    if getattr(network_stack, "socket_pool", None) is None:
        return "", ("socket_pool_unavailable",)
    if str(getattr(ntp_state, "phase", "") or "") == "synced":
        return "ntp_synced", ()

    resolve_error, resolved_ip = preflight_mqtt_broker(mqtt_adapter)
    if resolve_error:
        return "", (resolve_error,)
    if resolved_ip:
        return "broker_resolved", ()
    return "broker_preflight_ready", ()


def _should_fallback_to_ap(runtime_config, network_stack):
    if runtime_config.ap_mode:
        return False
    if getattr(network_stack, "phase", "") == "ap":
        return False
    if not runtime_config.network.ssid or not runtime_config.network.password:
        return True
    if runtime_config.active_profile != "nodusweb":
        return False
    if runtime_config.network.ssid:
        if _station_ip_looks_recoverable(runtime_config, network_stack):
            return False
        return getattr(network_stack, "phase", "") in {"error", "unavailable"}
    return True


def _station_ip_looks_recoverable(runtime_config, network_stack):
    """Return True when station has a non-AP IP and recovery should proceed."""
    if getattr(network_stack, "phase", "") not in {"error", "unavailable"}:
        return False
    ip_address = str(getattr(network_stack, "ip_address", "") or "").strip()
    if not ip_address:
        return False
    if (
        ip_address.startswith("192.168.4.")
        and runtime_config.network.ssid != runtime_config.network.ap_ssid
    ):
        return False
    return True


def _persist_learned_broker_ip(runtime_config, mqtt_adapter, *, settings_root=None):
    """Persist a resolved MQTT broker IP when the hostname connection succeeds."""
    learned_ip = str(getattr(mqtt_adapter, "resolved_broker_ip", "") or "").strip()
    broker = str(getattr(runtime_config.mqtt, "broker", "") or "").strip()
    active_broker = str(getattr(mqtt_adapter, "active_broker", "") or "").strip()
    if not settings_root or not learned_ip or not broker or active_broker != broker:
        return runtime_config, "skipped", ()
    if learned_ip == str(getattr(runtime_config.mqtt, "broker_ip", "") or "").strip():
        return runtime_config, "unchanged", ()
    updated_runtime, applied_updates, errors = Settings.apply_updates_to_directory(
        settings_root,
        runtime_config,
        ({"section": "MQTT", "key": "BROKER_IP", "value": learned_ip},),
        reload_runtime=True,
    )
    if errors:
        return runtime_config, "error", tuple(errors)
    if applied_updates:
        return updated_runtime, "persisted", ()
    return runtime_config, "skipped", ()


def _startup_ap_fallback_reason(station_errors):
    """Return the original station failure reason for AP fallback logs."""
    errors = tuple(station_errors or ())
    if errors:
        return ",".join(str(error) for error in errors)
    return "network_startup_failed"


def _enter_ap_recovery_mode(runtime_config):
    return replace(runtime_config, active_profile="nodusweb", ap_mode=True)


def _soft_reboot(*, reason="soft_reboot", start_monotonic=None):
    if start_monotonic is not None:
        _print_log(
            "runtime",
            "action=reload reason={}".format(str(reason or "soft_reboot")),
            start_monotonic=start_monotonic,
        )
    try:
        import supervisor  # type: ignore
    except ImportError as exc:
        raise RuntimeError("supervisor_unavailable") from exc
    reload_runtime = getattr(supervisor, "reload", None)
    if not callable(reload_runtime):
        raise RuntimeError("supervisor_reload_unavailable")
    reload_runtime()


def _log_recovery_soft_reboot(reboot_reason, *, fs_writable):
    """Persist a recovery reboot traceback when RWFS is available."""
    if fs_writable is not True:
        return False
    return append_reboot_reason_traceback(
        reboot_reason,
        header="recovery soft reboot: {}".format(str(reboot_reason or "unknown")),
    )


def _should_log_command_result(result):
    """Return True when a command result should be emitted to the serial log."""
    if result is None:
        return False
    if getattr(result, "phase", "") == "ignored":
        return False
    return getattr(result, "command_type", "") != "switch"


def _hard_reboot():
    try:
        import microcontroller  # type: ignore
    except ImportError as exc:
        raise RuntimeError("microcontroller_unavailable") from exc
    reset = getattr(microcontroller, "reset", None)
    if not callable(reset):
        raise RuntimeError("microcontroller_reset_unavailable")
    reset()


def _resolve_startup_plan(runtime_config, startup_plan_override=None):
    """Build the startup plan, applying an optional test override."""
    plan = StartupPlan.from_runtime_config(runtime_config)
    if callable(startup_plan_override):
        overridden = startup_plan_override(plan)
        if overridden is not None:
            return overridden
    return plan


def _load_settings_for_startup(settings_root):
    """Load settings while skipping write-based bootstrap on ROFS."""
    fs_writable = Settings.filesystem_writable(settings_root)
    if fs_writable is not False:
        if Settings.apply_factory_profile_reset_if_requested(settings_root):
            return None, fs_writable, True
        Settings.bootstrap_factory_defaults(settings_root)
    settings = Settings.from_working_directory()
    return settings, fs_writable, False


def _ota_state_path(root):
    """Return the private OTA state path under the settings root."""
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _load_startup_ota_state(settings_root):
    """Load private OTA state for startup branch selection."""
    if not settings_root:
        return None
    return load_ota_state(_ota_state_path(settings_root))


def _should_enter_ota_mode(ota_state, fs_writable):
    """Return True when startup should use the temporary OTA runtime."""
    if fs_writable is not True or ota_state is None:
        return False
    if getattr(ota_state, "mode", "") != "ota":
        return False
    return getattr(ota_state, "phase", "") in {"requested", "ready"}


def _mark_ota_applied_after_boot(ota_state, settings_root, fs_writable):
    """Mark an OTA update applied once normal startup resumes."""
    if fs_writable is not True or ota_state is None or not settings_root:
        return ota_state
    if getattr(ota_state, "mode", "") != "ota":
        return ota_state
    if getattr(ota_state, "phase", "") != "applied_pending_boot":
        return ota_state
    applied_state = FwUpdateState(
        prior_profile=getattr(ota_state, "prior_profile", "") or "",
        package_id=getattr(ota_state, "package_id", "") or "",
        phase="applied",
    )
    return save_ota_state(applied_state, _ota_state_path(settings_root))


async def main(*, startup_plan_override=None):
    """Run the current scaffold runtime."""
    start_monotonic = time.monotonic()
    settings_root = "."
    settings, fs_writable, profile_reset_requested = _load_settings_for_startup(
        settings_root
    )
    if profile_reset_requested:
        _print_log(
            "factory_reset",
            "phase=profile_reset profile=nodusweb action=hard_reboot",
            start_monotonic=start_monotonic,
        )
        _hard_reboot()
        return
    fs_mode = _filesystem_mode_label(fs_writable)
    persistence_mode = (
        "persisted"
        if fs_writable
        else "volatile"
        if fs_writable is False
        else "unknown"
    )
    writable_settings_root = (
        settings_root
        if fs_writable is True and _path_exists(Settings.SETTINGS_FILE)
        else None
    )
    runtime_config = settings.runtime_config()
    ota_state = _load_startup_ota_state(writable_settings_root)
    ota_state = _mark_ota_applied_after_boot(
        ota_state,
        writable_settings_root,
        fs_writable,
    )
    if _should_enter_ota_mode(ota_state, fs_writable):
        from cpynodus_ii.ota.runtime import run_ota_mode

        await run_ota_mode(
            runtime_config,
            ota_state,
            settings_root=writable_settings_root,
            version=__version__,
            log_fn=lambda prefix, message: _print_log(
                prefix,
                message,
                start_monotonic=start_monotonic,
            ),
            reboot_callback=lambda: _soft_reboot(
                reason="ota:applied_pending_boot",
                start_monotonic=start_monotonic,
            ),
            idle_s=None,
        )
        return
    network_stack = build_network_stack(
        runtime_config, log_start_monotonic=start_monotonic
    )
    startup_ap_fallback = _should_fallback_to_ap(runtime_config, network_stack)
    startup_ap_fallback_errors = tuple(network_stack.errors)
    if startup_ap_fallback:
        runtime_config = _enter_ap_recovery_mode(runtime_config)
        network_stack = build_network_stack(
            runtime_config, log_start_monotonic=start_monotonic
        )
    plan = _resolve_startup_plan(
        runtime_config,
        startup_plan_override=startup_plan_override,
    )
    transport = MQTTTransport.from_settings(settings)
    mqtt_adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
    )
    sensor_init = plan_sensor_initialization(runtime_config)
    switch_init = plan_switch_initialization(runtime_config)
    sensor_runtime = build_sensor_runtime(sensor_init, runtime_config)
    switch_runtime = build_switch_runtime(switch_init)
    sensor_adapter = bind_sensor_hardware(sensor_runtime, runtime_config)
    switch_adapter = bind_switch_hardware(switch_runtime)
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )
    switch_service = start_switch_service(switch_runtime, switch_adapter)
    sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
    steady_state = SteadyState()
    ntp_state = NTPState()
    recovery_policy = RecoveryPolicy()
    recovery_state = RecoveryState(
        phase="ap" if network_stack.phase == "ap" else "idle",
        phase_started_at=float(start_monotonic)
        if network_stack.phase == "ap"
        else -1.0,
    )
    web_runtime = None
    next_health_at = float(start_monotonic) + 300.0
    next_periodic_gc_at = float(start_monotonic) + 60.0
    periodic_gc_count = 0
    last_mqtt_connect_attempt_at = float(start_monotonic)
    startup_warm_reboot_done = (
        _read_startup_warm_reboot_marker() == _STARTUP_WARM_REBOOT_MARKER
    )
    startup_warm_reboot_wait_logged = False

    connect_phase = "deferred" if plan.mqtt_enabled else "skipped"

    _print_log(
        "cPyNodus_II",
        "boot version={} profile={} ap_mode={} fs={} persistence_mode={}".format(
            __version__,
            plan.profile,
            plan.ap_mode,
            fs_mode,
            persistence_mode,
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "runtime network_phase={} network_errors={} "
            "mqtt_client={} mqtt_connect={}"
        ).format(
            network_stack.phase,
            ",".join(network_stack.errors) if network_stack.errors else "none",
            mqtt_adapter.phase,
            "{}:{}".format(connect_phase, mqtt_adapter.active_broker or "none"),
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "sensor enabled={} family={} interface={} file={} phase={} target_addr={} "
            "adapter={} service={} metrics={} errors={}"
        ).format(
            plan.sensor_enabled,
            plan.sensor_family or "none",
            plan.sensor_interface or "none",
            plan.active_sensor_file or "none",
            sensor_runtime.phase,
            _sensor_target_addr(sensor_runtime),
            sensor_adapter.phase,
            sensor_service.phase,
            len((sensor_snapshot.metrics or {})),
            _sensor_error_text(
                sensor_runtime,
                sensor_adapter,
                sensor_service,
                sensor_snapshot,
            ),
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "switch enabled={} channels={} phase={} adapter={} "
            "service={} mqtt={} web={} ntp={}"
        ).format(
            plan.switch_enabled,
            switch_init.channel_count,
            switch_runtime.phase,
            switch_adapter.phase,
            switch_service.phase,
            plan.mqtt_enabled,
            plan.web_enabled,
            plan.ntp_enabled,
        ),
        start_monotonic=start_monotonic,
    )
    _collect_garbage()
    _print_log(
        "cPyNodus_II",
        "network ssid={} ipv4={} hostname={}".format(
            network_stack.ssid or "none",
            network_stack.ip_address or "none",
            network_stack.hostname or "none",
        ),
        start_monotonic=start_monotonic,
    )
    if plan.web_enabled:
        web_runtime = WebRuntimeController(
            runtime_config,
            network_stack,
            version=__version__,
            sensor_service=sensor_service,
            switch_service=switch_service,
            settings_root=writable_settings_root,
            reboot_callbacks={
                "soft": _soft_reboot,
                "hard": _hard_reboot,
            },
        ).start()
        _print_log(
            "web",
            "phase={} routes={} port={} errors={}".format(
                web_runtime.phase,
                ",".join(web_runtime.route_paths)
                if web_runtime.route_paths
                else "none",
                runtime_config.network.http_port,
                ",".join(web_runtime.errors) if web_runtime.errors else "none",
            ),
            start_monotonic=start_monotonic,
        )
        _collect_garbage()
    if startup_ap_fallback:
        _print_log(
            "recovery",
            "startup action=ap_fallback reason={}".format(
                _startup_ap_fallback_reason(startup_ap_fallback_errors),
            ),
            start_monotonic=start_monotonic,
        )
    _print_log(
        "cPyNodus_II",
        "switch_channels ids={} labels={}".format(
            ",".join(
                channel.channel_id or "none"
                for channel in runtime_config.switch.channels
            )
            or "none",
            ",".join(
                channel.label or channel.key or "none"
                for channel in runtime_config.switch.channels
            )
            or "none",
        ),
        start_monotonic=start_monotonic,
    )

    loop_error = None
    try:
        while True:
            now_monotonic = time.monotonic()
            network_stack = refresh_network_stack(network_stack)
            if web_runtime is not None:
                web_runtime.update_context(
                    runtime_config=runtime_config,
                    network_stack=network_stack,
                    sensor_service=sensor_service,
                    switch_service=switch_service,
                    version=__version__,
                )
                web_runtime.poll()
                runtime_config = web_runtime.runtime_config
            previous_phase = recovery_state.phase
            recovery_decision = advance_recovery_state(
                recovery_state,
                now_monotonic=now_monotonic,
                policy=recovery_policy,
                ap_mode=(network_stack.phase == "ap"),
                mqtt_enabled=bool(plan.mqtt_enabled),
                wifi_link_ready=network_link_is_ready(network_stack),
                transport_connected=transport.connected,
            )
            recovery_state = recovery_decision.state
            if recovery_state.phase != previous_phase:
                _print_log(
                    "recovery",
                    "phase={} wifi_ready={} mqtt_connected={}".format(
                        recovery_state.phase,
                        network_link_is_ready(network_stack),
                        transport.connected,
                    ),
                    start_monotonic=start_monotonic,
                )
            if recovery_decision.request_soft_reboot:
                _print_log(
                    "recovery",
                    "action=soft_reboot reason={}".format(
                        recovery_decision.reboot_reason
                    ),
                    start_monotonic=start_monotonic,
                )
                _log_recovery_soft_reboot(
                    recovery_decision.reboot_reason,
                    fs_writable=fs_writable,
                )
                _soft_reboot(
                    reason="recovery:{}".format(
                        recovery_decision.reboot_reason or "soft_reboot"
                    ),
                    start_monotonic=start_monotonic,
                )
            if recovery_decision.attempt_wifi_reconnect:
                reconnect_result = reconnect_network_stack(
                    runtime_config,
                    network_stack,
                    max_attempts=1,
                    retry_delay_s=0.0,
                    rebuild_socket_artifacts=False,
                    log_start_monotonic=start_monotonic,
                )
                network_stack = reconnect_result
                if network_link_is_ready(network_stack):
                    _print_log(
                        "recovery",
                        "wifi phase=recovered ipv4={}".format(
                            network_stack.ip_address or "none"
                        ),
                        start_monotonic=start_monotonic,
                    )
            if recovery_decision.attempt_mqtt_rebuild and network_link_is_ready(
                network_stack
            ):
                network_stack = reconnect_network_stack(
                    runtime_config,
                    network_stack,
                    max_attempts=1,
                    retry_delay_s=0.0,
                    rebuild_socket_artifacts=True,
                    log_start_monotonic=start_monotonic,
                )
                mqtt_adapter = build_mqtt_client_adapter(
                    runtime_config,
                    socket_pool=network_stack.socket_pool,
                    ssl_context=network_stack.ssl_context,
                )
                _print_log(
                    "recovery",
                    "mqtt action=rebuild broker={} socket_pool={}".format(
                        mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
                        "ready" if network_stack.socket_pool is not None else "none",
                    ),
                    start_monotonic=start_monotonic,
                )
            ntp_result = maybe_sync_ntp(
                runtime_config,
                network_stack,
                state=ntp_state,
                now_monotonic=now_monotonic,
            )
            ntp_state = ntp_result.state
            if ntp_result.phase != "skipped":
                _print_log(
                    "ntp",
                    "phase={} server={} rtc={} errors={}".format(
                        ntp_result.phase,
                        ntp_state.server or DEFAULT_NTP_SERVER,
                        ntp_state.datetime_text or "unknown",
                        ",".join(ntp_result.errors) if ntp_result.errors else "none",
                    ),
                    start_monotonic=start_monotonic,
                )
                if ntp_result.phase == "cooldown":
                    _print_log(
                        "ntp",
                        "marker=cooldown_entered server={} retry_after_s={}".format(
                            ntp_state.server or DEFAULT_NTP_SERVER,
                            int(
                                max(
                                    float(ntp_state.cooldown_until)
                                    - float(now_monotonic),
                                    0.0,
                                )
                            ),
                        ),
                        start_monotonic=start_monotonic,
                    )
                elif ntp_result.phase == "disabled":
                    _print_log(
                        "ntp",
                        (
                            "marker=disabled server={} "
                            "reason=attempt_limit_exhausted"
                        ).format(ntp_state.server or DEFAULT_NTP_SERVER),
                        start_monotonic=start_monotonic,
                    )
                if ntp_result.phase == "synced":
                    _collect_garbage()
            if (
                plan.mqtt_enabled
                and not transport.connected
                and mqtt_adapter.phase == "ready"
                and recovery_decision.allow_mqtt_connect
                and (float(now_monotonic) - float(last_mqtt_connect_attempt_at)) >= 5.0
            ):
                if not startup_warm_reboot_done:
                    warm_reason, warm_errors = _startup_warm_reboot_ready_reason(
                        runtime_config,
                        network_stack,
                        mqtt_adapter,
                        ntp_state,
                        plan,
                    )
                    if warm_reason:
                        startup_warm_reboot_done = True
                        if _mark_startup_warm_rebooted():
                            _print_log(
                                "recovery",
                                (
                                    "startup action=warm_start reason={} broker={} "
                                    "broker_ip={}"
                                ).format(
                                    warm_reason,
                                    mqtt_adapter.active_broker
                                    or mqtt_adapter.broker
                                    or "none",
                                    mqtt_adapter.resolved_broker_ip
                                    or runtime_config.mqtt.broker_ip
                                    or "none",
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _soft_reboot(
                                reason="startup:{}".format(warm_reason),
                                start_monotonic=start_monotonic,
                            )
                            return
                        _print_log(
                            "recovery",
                            (
                                "startup action=warm_start_skipped "
                                "reason=nvm_unavailable marker={}"
                            ).format(warm_reason),
                            start_monotonic=start_monotonic,
                        )
                    else:
                        if not startup_warm_reboot_wait_logged:
                            _print_log(
                                "recovery",
                                "startup action=warm_start_wait errors={}".format(
                                    ",".join(warm_errors)
                                    if warm_errors
                                    else "not_ready"
                                ),
                                start_monotonic=start_monotonic,
                            )
                            startup_warm_reboot_wait_logged = True
                        await asyncio.sleep(1)
                        continue
                transport.mark_connect_requested()
                connect_started_at = time.monotonic()
                connect_result = connect_mqtt_client(
                    mqtt_adapter,
                    transport,
                    preflight=_should_preflight_broker(mqtt_adapter),
                )
                mqtt_adapter = connect_result.adapter
                connect_phase = connect_result.phase
                last_mqtt_connect_attempt_at = float(now_monotonic)
                if connect_phase == "connected":
                    runtime_config, broker_ip_phase, broker_ip_errors = (
                        _persist_learned_broker_ip(
                            runtime_config,
                            mqtt_adapter,
                            settings_root=writable_settings_root,
                        )
                    )
                    if broker_ip_phase in {"persisted", "error"}:
                        _print_log(
                            "mqtt",
                            "broker_ip phase={} host={} ip={} errors={}".format(
                                broker_ip_phase,
                                runtime_config.mqtt.broker or "none",
                                mqtt_adapter.resolved_broker_ip or "none",
                                ",".join(broker_ip_errors)
                                if broker_ip_errors
                                else "none",
                            ),
                            start_monotonic=start_monotonic,
                    )
                    _print_log(
                        "mqtt",
                        "connect phase={} broker={} elapsed_s={:.1f}".format(
                            connect_phase,
                            mqtt_adapter.active_broker or "none",
                            time.monotonic() - connect_started_at,
                        ),
                        start_monotonic=start_monotonic,
                    )
                    sync_before = _transport_queue_summary(transport)
                    sync_result = sync_transport_to_client(mqtt_adapter, transport)
                    mqtt_adapter = sync_result.adapter
                    if sync_result.phase == "error":
                        _print_log(
                            "mqtt",
                            _mqtt_sync_summary(
                                sync_result,
                                transport,
                                source="post_connect",
                                before_queues=sync_before,
                            ),
                            start_monotonic=start_monotonic,
                        )
                    _collect_garbage()
                    _log_memory_checkpoint(start_monotonic, "post_mqtt_connect")
                elif connect_phase == "error":
                    _print_log(
                        "mqtt",
                        "connect phase=error broker={} errors={}".format(
                            mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
                            ",".join(connect_result.errors)
                            if connect_result.errors
                            else "none",
                        ),
                        start_monotonic=start_monotonic,
                    )
            iteration = run_steady_state_iteration(
                transport,
                runtime_config,
                switch_service,
                sensor_service,
                state=steady_state,
                version=__version__,
                now_monotonic=now_monotonic,
                active_broker=mqtt_adapter.active_broker,
                settings_root=writable_settings_root,
            )
            steady_state = iteration.state
            runtime_config = iteration.runtime_config
            sensor_issue = _sensor_issue_text(iteration.errors)
            if sensor_issue:
                _print_log(
                    "sensor",
                    "poll phase={} published={} errors={}".format(
                        iteration.sensor_publish_phase,
                        iteration.sensor_published_count,
                        sensor_issue,
                    ),
                    start_monotonic=start_monotonic,
                )
            if iteration.subscribed_topics:
                _print_log(
                    "mqtt",
                    "subscriptions queued topics={} gen={} {}".format(
                        ",".join(iteration.subscribed_topics),
                        transport.connection_generation,
                        _transport_queue_summary(transport),
                    ),
                    start_monotonic=start_monotonic,
                )
            for result in iteration.command_results:
                if not _should_log_command_result(result):
                    continue
                _print_log(
                    "mqtt",
                    (
                        "command type={} phase={} topic={} requested={} "
                        "published={} persistence_mode={} errors={}"
                    ).format(
                        result.command_type,
                        result.phase,
                        result.topic,
                        result.requested_state or "none",
                        result.published_count,
                        result.persistence_mode or persistence_mode,
                        ",".join(result.errors) if result.errors else "none",
                    ),
                    start_monotonic=start_monotonic,
                )
            ota_reboot_requested = any(
                bool(getattr(result, "reboot_requested", False))
                for result in iteration.command_results
            )
            if float(now_monotonic) >= float(next_health_at):
                _print_log(
                    "cPyNodus_II",
                    (
                        "health network_phase={} recovery_phase={} ssid={} ipv4={} "
                        "dns_health={} ntp_health={} mqtt_connected={} active_broker={}"
                    ).format(
                        network_stack.phase,
                        recovery_state.phase,
                        network_stack.ssid or "none",
                        network_stack.ip_address or "none",
                        _dns_health_text(network_stack, mqtt_adapter, ntp_state),
                        _ntp_health_text(ntp_state),
                        transport.connected,
                        mqtt_adapter.active_broker or "none",
                    ),
                    start_monotonic=start_monotonic,
                )
                while float(next_health_at) <= float(now_monotonic):
                    next_health_at += 300.0
            if float(now_monotonic) >= float(next_periodic_gc_at):
                before = _memory_summary()
                _collect_garbage()
                after = _memory_summary()
                periodic_gc_count += 1
                if (periodic_gc_count % 5) == 0:
                    _print_log(
                        "memory",
                        "phase=periodic_gc before={} after={} {}".format(
                            before,
                            after,
                            _transport_queue_summary(transport),
                        ),
                        start_monotonic=start_monotonic,
                    )
                while float(next_periodic_gc_at) <= float(now_monotonic):
                    next_periodic_gc_at += 60.0
            if transport.connected:
                sync_before = _transport_queue_summary(transport)
                sync_result = sync_transport_to_client(mqtt_adapter, transport)
                mqtt_adapter = sync_result.adapter
                if sync_result.phase == "error":
                    _print_log(
                        "mqtt",
                        _mqtt_sync_summary(
                            sync_result,
                            transport,
                            source="main_loop",
                            before_queues=sync_before,
                        ),
                        start_monotonic=start_monotonic,
                    )
                if ota_reboot_requested:
                    _print_log(
                        "ota",
                        "action=soft_reboot reason=fwupdate_prepare",
                        start_monotonic=start_monotonic,
                    )
                    _soft_reboot(
                        reason="ota:fwupdate_prepare",
                        start_monotonic=start_monotonic,
                    )
                poll_result = poll_mqtt_client(mqtt_adapter, transport)
                mqtt_adapter = poll_result.adapter
                if poll_result.phase == "error" and not _is_recoverable_mqtt_poll_error(
                    poll_result.errors
                ):
                    _print_log(
                        "mqtt",
                        "poll phase={} received={} errors={}".format(
                            poll_result.phase,
                            poll_result.received_count,
                            ",".join(poll_result.errors)
                            if poll_result.errors
                            else "none",
                        ),
                        start_monotonic=start_monotonic,
                    )
            await asyncio.sleep(0.05)
    except Exception as exc:
        loop_error = exc
        _print_log(
            "runtime",
            "fatal error={} type={}".format(str(exc), type(exc).__name__),
            start_monotonic=start_monotonic,
        )
        raise
    finally:
        if transport.connected:
            try:
                disconnect_result = disconnect_mqtt_client(
                    mqtt_adapter,
                    transport,
                    runtime_config,
                )
                mqtt_adapter = disconnect_result.adapter
                if disconnect_result.errors:
                    _print_log(
                        "mqtt",
                        (
                            "disconnect phase={} published={} subscribed={} "
                            "errors={}"
                        ).format(
                            disconnect_result.phase,
                            disconnect_result.published_count,
                            disconnect_result.subscribed_count,
                            ",".join(disconnect_result.errors),
                        ),
                        start_monotonic=start_monotonic,
                    )
            except Exception as exc:
                _print_log(
                    "mqtt",
                    "disconnect phase=error errors={}".format(str(exc)),
                    start_monotonic=start_monotonic,
                )
        if loop_error is not None:
            _print_log(
                "runtime",
                "shutdown after fatal type={}".format(type(loop_error).__name__),
                start_monotonic=start_monotonic,
            )
