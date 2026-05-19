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
    close_mqtt_client,
    connect_mqtt_client,
    disconnect_mqtt_client,
    maybe_sync_ntp,
    network_error_signature,
    network_link_is_ready,
    poll_mqtt_client,
    reconnect_network_stack,
    refresh_network_stack,
    sync_transport_to_client,
    teardown_network_stack,
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

MQTT_REBUILD_VERIFY_WINDOW_S = 90.0
MQTT_REBUILD_VERIFY_PHASE_S = 10.0
MQTT_LONG_RECOVERY_REBOOT_S = 900.0
MQTT_BROKER_OUTAGE_BACKOFF_AFTER_S = 300.0
MQTT_BROKER_OUTAGE_RETRY_INTERVAL_S = 30.0
MQTT_MEMORY_FAILURE_REBOOT_S = 20.0
MQTT_MEMORY_FAILURE_REBOOT_MIN_COUNT = 5
MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S = 180.0
MQTT_REPEATED_CONNECT_FAILURE_MIN_COUNT = 3
WIFI_AFTER_READY_FAILURE_SIGNATURES = (
    "station_scan_miss_after_ready",
    "station_unknown_after_ready",
)
WIFI_AFTER_READY_STATION_RESET_FAILURES = 2
WIFI_AFTER_READY_REBOOT_S = 90.0
WIFI_AFTER_READY_REBOOT_MIN_FAILURES = 3
WIFI_BEFORE_READY_RESET_SIGNATURES = (
    "station_scan_miss",
    "station_unknown",
)
WIFI_BEFORE_READY_STATION_RESET_FAILURES = 2
STATION_RESET_RECONNECT_ATTEMPTS = 3
STATION_RESET_RECONNECT_DELAY_S = 2.0
HARD_RECOVERY_REBOOT_REASONS = (
    "mqtt_memory_allocation_failures",
    "mqtt_recovery_timeout",
    "mqtt_repeated_connect_failures",
)


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


def _mqtt_connect_errors_are_repeated_failures(
    errors,
    *,
    had_mqtt_success=False,
    count_plain_connect_failures=False,
):
    for error in tuple(errors or ()):
        text = str(error or "").strip().lower()
        if "repeated connect failures" in text:
            return True
        if text.startswith("mqtt_connect_failed:") and (
            bool(had_mqtt_success) or bool(count_plain_connect_failures)
        ):
            return True
    return False


def _should_fast_reboot_mqtt_connect_failures(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S,
    min_count=MQTT_REPEATED_CONNECT_FAILURE_MIN_COUNT,
):
    elapsed_s = max(0.0, float(now_monotonic or 0.0) - float(first_failure_at))
    return (
        int(failure_count or 0) >= int(min_count or 0)
        and float(first_failure_at) >= 0.0
        and elapsed_s >= float(timeout_s or 0.0)
    )


def _mqtt_client_init_memory_failed(errors):
    """Return True when MQTT client construction failed from heap pressure."""
    text = " ".join(str(error or "") for error in tuple(errors or ())).lower()
    return (
        "mqtt_client_init_failed" in text
        and ("memory allocation failed" in text or "allocating " in text)
    )


def _update_mqtt_memory_failure_window(
    errors,
    failure_count,
    first_failure_at,
    now_monotonic,
):
    """Update consecutive MQTT client memory-init failure counters."""
    if not _mqtt_client_init_memory_failed(errors):
        return 0, -1.0
    if int(failure_count or 0) <= 0:
        first_failure_at = float(now_monotonic or 0.0)
    return int(failure_count or 0) + 1, float(first_failure_at)


def _should_reboot_mqtt_memory_failures(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_MEMORY_FAILURE_REBOOT_S,
    min_count=MQTT_MEMORY_FAILURE_REBOOT_MIN_COUNT,
):
    """Return True when repeated MQTT memory failures need reboot escalation."""
    elapsed_s = max(0.0, float(now_monotonic or 0.0) - float(first_failure_at))
    return (
        int(failure_count or 0) >= int(min_count or 0)
        and float(first_failure_at) >= 0.0
        and elapsed_s >= float(timeout_s or 0.0)
    )


def _should_reboot_long_mqtt_recovery(
    recovery_state,
    now_monotonic,
    *,
    timeout_s=MQTT_LONG_RECOVERY_REBOOT_S,
):
    """Return True when MQTT recovery has exceeded the bounded outage window."""
    return _mqtt_recovery_elapsed_s(recovery_state, now_monotonic) >= float(
        timeout_s or 0.0
    )


def _is_plain_mqtt_connect_failure(reason):
    """Return True for broker connect failures that do not imply socket poison."""
    text = str(reason or "").strip().lower()
    if not text.startswith("mqtt_connect_failed:"):
        return False
    return "repeated connect failures" not in text


def _mqtt_connect_retry_interval_s(
    recovery_state,
    now_monotonic,
    last_disconnect_reason,
):
    """Return the current MQTT connect retry interval."""
    if (
        _is_plain_mqtt_connect_failure(last_disconnect_reason)
        and _mqtt_recovery_elapsed_s(recovery_state, now_monotonic)
        >= MQTT_BROKER_OUTAGE_BACKOFF_AFTER_S
    ):
        return MQTT_BROKER_OUTAGE_RETRY_INTERVAL_S
    return 5.0


def _should_rebuild_mqtt_adapter_for_recovery(mqtt_adapter, last_disconnect_reason):
    """Return True when MQTT recovery should replace the current adapter."""
    if getattr(mqtt_adapter, "phase", "") != "ready":
        return True
    return _mqtt_disconnect_reason_requires_rebuild(last_disconnect_reason)


def _is_after_ready_wifi_failure(signature):
    """Return True for Wi-Fi failures seen after a known-good station link."""
    return str(signature or "") in WIFI_AFTER_READY_FAILURE_SIGNATURES


def _should_fast_reset_wifi_station(signature, failure_count):
    """Return True when post-ready Wi-Fi failure should reset station state."""
    return (
        _is_after_ready_wifi_failure(signature)
        and int(failure_count or 0) >= WIFI_AFTER_READY_STATION_RESET_FAILURES
    )


def _should_reset_wifi_station_before_ready(signature, failure_count, wifi_was_ready):
    """Return True when startup Wi-Fi recovery needs a station reset."""
    return (
        not bool(wifi_was_ready)
        and str(signature or "") in WIFI_BEFORE_READY_RESET_SIGNATURES
        and int(failure_count or 0) >= WIFI_BEFORE_READY_STATION_RESET_FAILURES
    )


def _wifi_station_reset_reason(fast_station_reset, pre_ready_station_reset):
    if fast_station_reset:
        return "after_ready_failure"
    if pre_ready_station_reset:
        return "before_ready_failure"
    return "backoff"


def _should_fast_reboot_wifi_after_ready(
    signature,
    failure_count,
    recovery_elapsed_s,
):
    """Return True when post-ready Wi-Fi recovery should reload runtime."""
    return (
        _is_after_ready_wifi_failure(signature)
        and int(failure_count or 0) >= WIFI_AFTER_READY_REBOOT_MIN_FAILURES
        and float(recovery_elapsed_s or 0.0) >= WIFI_AFTER_READY_REBOOT_S
    )


def _recovery_reconnect_attempts(reset_station):
    return STATION_RESET_RECONNECT_ATTEMPTS if reset_station else 1


def _recovery_reconnect_delay_s(reset_station):
    return STATION_RESET_RECONNECT_DELAY_S if reset_station else 0.0


def _looks_like_ip_literal(value):
    text = str(value or "").strip()
    if not text:
        return False
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    return ":" in text


def _wifi_recovery_elapsed_s(recovery_state, now_monotonic):
    started_at = float(getattr(recovery_state, "phase_started_at", -1.0))
    if started_at < 0.0:
        return 0.0
    return max(0.0, float(now_monotonic or 0.0) - started_at)


def _mqtt_recovery_elapsed_s(recovery_state, now_monotonic):
    if getattr(recovery_state, "phase", "") != "mqtt":
        return 0.0
    started_at = float(getattr(recovery_state, "phase_started_at", -1.0))
    if started_at < 0.0:
        return 0.0
    return max(0.0, float(now_monotonic or 0.0) - started_at)


def _should_log_wifi_failure_signature(signature, count, reset_station):
    """Return True when a Wi-Fi failure signature should be emitted."""
    if not signature or signature == "none":
        return False
    if count in (1, 3):
        return True
    return bool(reset_station and (int(count or 0) % 5) == 0)


def _should_verify_mqtt_before_rebuild(
    transport,
    recovery_state,
    now_monotonic,
    *,
    success_window_s=MQTT_REBUILD_VERIFY_WINDOW_S,
    phase_window_s=MQTT_REBUILD_VERIFY_PHASE_S,
):
    """Return True when recent MQTT success should get one reconnect attempt."""
    if _mqtt_disconnect_reason_requires_rebuild(
        getattr(transport, "last_disconnect_reason", "")
    ):
        return False
    last_success_at = float(getattr(transport, "last_success_at", -1.0))
    if last_success_at < 0.0:
        return False
    now_value = float(now_monotonic or 0.0)
    if now_value - last_success_at > float(success_window_s or 0.0):
        return False
    return _mqtt_recovery_elapsed_s(recovery_state, now_value) <= float(
        phase_window_s or 0.0
    )


def _mqtt_disconnect_reason_requires_rebuild(reason):
    """Return True when the MQTT client/socket should be rebuilt immediately."""
    text = str(reason or "").strip()
    if not text:
        return False
    if text.startswith("mqtt_connect_failed:"):
        return "repeated connect failures" in text.lower()
    hard_prefixes = (
        "mqtt_poll_failed:",
        "mqtt_publish_failed:",
        "mqtt_subscribe_failed:",
        "mqtt_poll_callback_failed:",
        "mqtt_client_on_disconnect",
    )
    return any(text.startswith(prefix) for prefix in hard_prefixes)


def _is_mqtt_subscription_failure(sync_result):
    """Return True when MQTT sync failed while subscribing."""
    if getattr(sync_result, "phase", "") != "error":
        return False
    for error in tuple(getattr(sync_result, "errors", ()) or ()):
        if "mqtt_subscribe_failed:" in str(error or ""):
            return True
    return False


def _startup_subscription_recovery_drained(sync_result, transport):
    """Return True when recovery drained queued MQTT subscriptions."""
    if getattr(sync_result, "phase", "") != "synced":
        return False
    if int(getattr(sync_result, "subscribed_count", 0) or 0) <= 0:
        return False
    try:
        return len(getattr(transport, "subscriptions", ()) or ()) == 0
    except Exception:
        return False


def _recover_mqtt_subscription_failure(
    *,
    runtime_config,
    network_stack,
    mqtt_adapter,
    transport,
    start_monotonic,
):
    """Close and rebuild MQTT after a subscribe timeout poisons the session."""
    _print_log(
        "recovery",
        "action=mqtt_rebuild reason=subscribe_failure {}".format(
            _transport_queue_summary(transport),
        ),
        start_monotonic=start_monotonic,
    )
    close_result = close_mqtt_client(mqtt_adapter, transport)
    if close_result.errors:
        _print_log(
            "recovery",
            "mqtt close errors={}".format(",".join(close_result.errors)),
            start_monotonic=start_monotonic,
        )
    rebuilt_adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
    )
    _print_log(
        "recovery",
        "mqtt action=rebuild broker={} socket_pool={} station_reset=0".format(
            rebuilt_adapter.active_broker or rebuilt_adapter.broker or "none",
            "ready" if network_stack.socket_pool is not None else "none",
        ),
        start_monotonic=start_monotonic,
    )
    return rebuilt_adapter


def _resolve_broker_ip_from_hostname(runtime_config, network_stack):
    """Resolve configured MQTT broker hostname to an IP literal."""
    broker = str(getattr(runtime_config.mqtt, "broker", "") or "").strip()
    if not broker:
        return "", ()
    if _looks_like_ip_literal(broker):
        return broker, ()
    socket_pool = getattr(network_stack, "socket_pool", None)
    if socket_pool is None:
        return "", ("mqtt_resolve_failed:{}:socket_pool_unavailable".format(broker),)
    getaddrinfo = getattr(socket_pool, "getaddrinfo", None)
    if not callable(getaddrinfo):
        return "", ("mqtt_resolve_failed:{}:getaddrinfo_unavailable".format(broker),)
    try:
        resolved = getaddrinfo(broker, runtime_config.mqtt.port)
    except Exception as exc:
        return "", ("mqtt_resolve_failed:{}:{}".format(broker, exc),)
    resolved_ip = _ip_from_getaddrinfo_result(resolved)
    if not resolved_ip:
        return "", ("mqtt_resolve_failed:{}:empty_result".format(broker),)
    return resolved_ip, ()


def _refresh_broker_ip_from_hostname(
    runtime_config,
    network_stack,
    *,
    settings_root=None,
):
    """Refresh MQTT.BROKER_IP from MQTT.BROKER before MQTT connects."""
    resolved_ip, errors = _resolve_broker_ip_from_hostname(
        runtime_config,
        network_stack,
    )
    if errors:
        return runtime_config, "error", tuple(errors)
    if not resolved_ip:
        return runtime_config, "skipped", ()
    current_ip = str(getattr(runtime_config.mqtt, "broker_ip", "") or "").strip()
    if current_ip == resolved_ip:
        return runtime_config, "unchanged", ()
    resolved_runtime = replace(
        runtime_config,
        mqtt=replace(runtime_config.mqtt, broker_ip=resolved_ip),
    )
    if not settings_root:
        return resolved_runtime, "resolved_volatile", ()
    updated_runtime, applied_updates, write_errors = (
        Settings.apply_updates_to_directory(
            settings_root,
            runtime_config,
            ({"section": "MQTT", "key": "BROKER_IP", "value": resolved_ip},),
            reload_runtime=True,
        )
    )
    if write_errors:
        return resolved_runtime, "error", tuple(write_errors)
    if applied_updates:
        return updated_runtime, "persisted", ()
    return resolved_runtime, "resolved_volatile", ()


def _broker_ip_refresh_needed(runtime_config, *, settings_root=None):
    """Return True when resolving MQTT.BROKER is worth attempting."""
    if settings_root:
        return True
    return not bool(str(getattr(runtime_config.mqtt, "broker_ip", "") or "").strip())


def _ip_from_getaddrinfo_result(resolved):
    try:
        first = resolved[0]
        sockaddr = first[-1]
        return str(sockaddr[0] or "").strip()
    except Exception:
        return ""


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


def _hard_reboot(*, reason="hard_reboot", start_monotonic=None):
    if start_monotonic is not None:
        _print_log(
            "runtime",
            "action=reset reason={}".format(str(reason or "hard_reboot")),
            start_monotonic=start_monotonic,
        )
    try:
        import microcontroller  # type: ignore
    except ImportError as exc:
        raise RuntimeError("microcontroller_unavailable") from exc
    reset = getattr(microcontroller, "reset", None)
    if not callable(reset):
        raise RuntimeError("microcontroller_reset_unavailable")
    reset()


def _recovery_reboot_kind(reboot_reason, *, fs_writable=False):
    """Return the reload depth needed for a recovery escalation."""
    if fs_writable is not True:
        return "soft"
    reason = str(reboot_reason or "").strip()
    if reason in HARD_RECOVERY_REBOOT_REASONS:
        return "hard"
    return "soft"


def _log_recovery_reboot(reboot_reason, *, fs_writable, reboot_kind="soft"):
    """Persist a recovery reboot traceback when RWFS is available."""
    if fs_writable is not True:
        return False
    kind = str(reboot_kind or "soft").strip() or "soft"
    return append_reboot_reason_traceback(
        reboot_reason,
        header="recovery {} reboot: {}".format(
            kind,
            str(reboot_reason or "unknown"),
        ),
    )


def _log_recovery_soft_reboot(reboot_reason, *, fs_writable):
    """Persist a legacy soft-reboot traceback when RWFS is available."""
    return _log_recovery_reboot(
        reboot_reason,
        fs_writable=fs_writable,
        reboot_kind="soft",
    )


def _teardown_network_for_shutdown(
    network_stack,
    *,
    start_monotonic,
    log_prefix="runtime",
):
    """Tear down Wi-Fi networking before leaving the runtime."""
    if network_stack is None:
        return False
    try:
        torn_down = teardown_network_stack(network_stack)
        _print_log(
            log_prefix,
            "network action=teardown result={}".format(1 if torn_down else 0),
            start_monotonic=start_monotonic,
        )
        return bool(torn_down)
    except Exception as exc:
        _print_log(
            log_prefix,
            "network teardown errors={}".format(str(exc)),
            start_monotonic=start_monotonic,
        )
    return False


def _prepare_soft_recovery_reboot(
    *,
    mqtt_adapter=None,
    transport=None,
    network_stack=None,
    start_monotonic,
):
    """Close MQTT and station networking before a recovery reload."""
    if mqtt_adapter is not None and transport is not None:
        try:
            close_result = close_mqtt_client(mqtt_adapter, transport)
            if close_result.errors:
                _print_log(
                    "recovery",
                    "mqtt close errors={}".format(",".join(close_result.errors)),
                    start_monotonic=start_monotonic,
                )
        except Exception as exc:
            _print_log(
                "recovery",
                "mqtt close errors={}".format(str(exc)),
                start_monotonic=start_monotonic,
            )
    _collect_garbage()
    _teardown_network_for_shutdown(
        network_stack,
        start_monotonic=start_monotonic,
        log_prefix="recovery",
    )
    _collect_garbage()


def _perform_recovery_reboot(
    reboot_reason,
    reboot_kind,
    *,
    fs_writable,
    start_monotonic,
    mqtt_adapter=None,
    transport=None,
    network_stack=None,
):
    """Persist recovery reboot context, then perform the selected reboot."""
    _log_recovery_reboot(
        reboot_reason,
        fs_writable=fs_writable,
        reboot_kind=reboot_kind,
    )
    reason = "recovery:{}".format(
        reboot_reason or "{}_reboot".format(reboot_kind or "soft")
    )
    if reboot_kind == "hard":
        _hard_reboot(reason=reason, start_monotonic=start_monotonic)
    else:
        _prepare_soft_recovery_reboot(
            mqtt_adapter=mqtt_adapter,
            transport=transport,
            network_stack=network_stack,
            start_monotonic=start_monotonic,
        )
        _soft_reboot(reason=reason, start_monotonic=start_monotonic)


def _should_log_command_result(result):
    """Return True when a command result should be emitted to the serial log."""
    if result is None:
        return False
    if getattr(result, "phase", "") == "ignored":
        return False
    return getattr(result, "command_type", "") != "switch"


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
    rtc_valid_at_boot = bool(_datetime_stamp())
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
    if plan.mqtt_enabled and _broker_ip_refresh_needed(
        runtime_config,
        settings_root=writable_settings_root,
    ):
        runtime_config, broker_ip_phase, broker_ip_errors = (
            _refresh_broker_ip_from_hostname(
                runtime_config,
                network_stack,
                settings_root=writable_settings_root,
            )
        )
        if broker_ip_phase != "skipped":
            _print_log(
                "mqtt",
                "broker_ip phase={} host={} ip={} errors={}".format(
                    broker_ip_phase,
                    runtime_config.mqtt.broker or "none",
                    runtime_config.mqtt.broker_ip or "none",
                    ",".join(broker_ip_errors) if broker_ip_errors else "none",
                ),
                start_monotonic=start_monotonic,
            )
    transport = MQTTTransport(
        runtime_config.mqtt.preferred_host,
        runtime_config.mqtt.port,
    )
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
    repeated_mqtt_connect_failure_count = 0
    repeated_mqtt_connect_failure_started_at = -1.0
    mqtt_client_memory_failure_count = 0
    mqtt_client_memory_failure_started_at = -1.0
    mqtt_subscribe_recovery_pending = False
    wifi_was_ready = network_link_is_ready(network_stack)
    last_wifi_failure_signature = ""
    wifi_failure_signature_count = 0
    wifi_after_ready_failure_count = 0
    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
        mqtt_client_memory_failure_count = 1
        mqtt_client_memory_failure_started_at = float(start_monotonic)
        _collect_garbage()

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
            if network_link_is_ready(network_stack):
                wifi_was_ready = True
                last_wifi_failure_signature = ""
                wifi_failure_signature_count = 0
                wifi_after_ready_failure_count = 0
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
                if recovery_state.phase == "wifi" and transport.connected:
                    transport.mark_disconnected(reason="wifi_link_lost")
                _print_log(
                    "recovery",
                    "phase={} wifi_ready={} mqtt_connected={}".format(
                        recovery_state.phase,
                        network_link_is_ready(network_stack),
                        transport.connected,
                    ),
                    start_monotonic=start_monotonic,
                )
                if not transport.connected and transport.last_disconnect_reason:
                    _print_log(
                        "recovery",
                        "mqtt disconnected reason={}".format(
                            transport.last_disconnect_reason
                        ),
                        start_monotonic=start_monotonic,
                    )
            if (
                plan.mqtt_enabled
                and network_link_is_ready(network_stack)
                and not transport.connected
                and _should_reboot_long_mqtt_recovery(
                    recovery_state,
                    now_monotonic,
                )
            ):
                reboot_reason = "mqtt_recovery_timeout"
                reboot_kind = _recovery_reboot_kind(
                    reboot_reason,
                    fs_writable=fs_writable,
                )
                _print_log(
                    "recovery",
                    "action={}_reboot reason={} elapsed_s={:.0f}".format(
                        reboot_kind,
                        reboot_reason,
                        _mqtt_recovery_elapsed_s(recovery_state, now_monotonic),
                    ),
                    start_monotonic=start_monotonic,
                )
                _perform_recovery_reboot(
                    reboot_reason,
                    reboot_kind,
                    fs_writable=fs_writable,
                    start_monotonic=start_monotonic,
                    mqtt_adapter=mqtt_adapter,
                    transport=transport,
                    network_stack=network_stack,
                )
            if recovery_decision.request_soft_reboot:
                reboot_kind = _recovery_reboot_kind(
                    recovery_decision.reboot_reason,
                    fs_writable=fs_writable,
                )
                _print_log(
                    "recovery",
                    "action={}_reboot reason={}".format(
                        reboot_kind,
                        recovery_decision.reboot_reason
                    ),
                    start_monotonic=start_monotonic,
                )
                _perform_recovery_reboot(
                    recovery_decision.reboot_reason,
                    reboot_kind,
                    fs_writable=fs_writable,
                    start_monotonic=start_monotonic,
                    mqtt_adapter=mqtt_adapter,
                    transport=transport,
                    network_stack=network_stack,
                )
            if recovery_decision.attempt_wifi_reconnect:
                wifi_recovery_elapsed_s = _wifi_recovery_elapsed_s(
                    recovery_state,
                    now_monotonic,
                )
                fast_station_reset = _should_fast_reset_wifi_station(
                    last_wifi_failure_signature,
                    wifi_after_ready_failure_count,
                )
                pre_ready_station_reset = _should_reset_wifi_station_before_ready(
                    last_wifi_failure_signature,
                    wifi_failure_signature_count,
                    wifi_was_ready,
                )
                reset_station = (
                    recovery_state.phase == "wifi"
                    and (
                        fast_station_reset
                        or pre_ready_station_reset
                        or wifi_recovery_elapsed_s
                        >= float(recovery_policy.wifi_backoff_after_s)
                    )
                )
                cycle_radio = bool(reset_station and recovery_state.phase == "wifi")
                if reset_station:
                    reset_count = (
                        wifi_after_ready_failure_count
                        if fast_station_reset
                        else wifi_failure_signature_count
                    )
                    _print_log(
                        "recovery",
                        (
                            "wifi action=station_reset reason={} count={} "
                            "radio_cycle={}"
                        ).format(
                            _wifi_station_reset_reason(
                                fast_station_reset,
                                pre_ready_station_reset,
                            ),
                            reset_count,
                            1 if cycle_radio else 0,
                        ),
                        start_monotonic=start_monotonic,
                    )
                reconnect_result = reconnect_network_stack(
                    runtime_config,
                    network_stack,
                    max_attempts=_recovery_reconnect_attempts(reset_station),
                    retry_delay_s=_recovery_reconnect_delay_s(reset_station),
                    rebuild_socket_artifacts=reset_station,
                    reset_station=reset_station,
                    cycle_radio=cycle_radio,
                    log_start_monotonic=start_monotonic,
                )
                network_stack = reconnect_result
                if network_link_is_ready(network_stack):
                    wifi_was_ready = True
                    last_wifi_failure_signature = ""
                    wifi_failure_signature_count = 0
                    wifi_after_ready_failure_count = 0
                    _print_log(
                        "recovery",
                        "wifi phase=recovered ipv4={}".format(
                            network_stack.ip_address or "none"
                        ),
                        start_monotonic=start_monotonic,
                    )
                else:
                    signature = network_error_signature(
                        network_stack,
                        had_ready_link=wifi_was_ready,
                    )
                    if signature != last_wifi_failure_signature:
                        last_wifi_failure_signature = signature
                        wifi_failure_signature_count = 1
                    else:
                        wifi_failure_signature_count += 1
                    if _is_after_ready_wifi_failure(signature):
                        wifi_after_ready_failure_count += 1
                    else:
                        wifi_after_ready_failure_count = 0
                    if _should_log_wifi_failure_signature(
                        signature,
                        wifi_failure_signature_count,
                        reset_station,
                    ):
                        _print_log(
                            "recovery",
                            "wifi signature={} count={} errors={}".format(
                                signature or "none",
                                wifi_failure_signature_count,
                                ",".join(network_stack.errors)
                                if network_stack.errors
                                else "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if _should_fast_reboot_wifi_after_ready(
                        signature,
                        wifi_after_ready_failure_count,
                        wifi_recovery_elapsed_s,
                    ):
                        reboot_reason = "wifi_after_ready_failure"
                        reboot_kind = _recovery_reboot_kind(
                            reboot_reason,
                            fs_writable=fs_writable,
                        )
                        _print_log(
                            "recovery",
                            (
                                "action={}_reboot reason={} signature={} "
                                "count={} elapsed_s={:.0f}"
                            ).format(
                                reboot_kind,
                                reboot_reason,
                                signature or "none",
                                wifi_after_ready_failure_count,
                                wifi_recovery_elapsed_s,
                            ),
                            start_monotonic=start_monotonic,
                        )
                        _perform_recovery_reboot(
                            reboot_reason,
                            reboot_kind,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            network_stack=network_stack,
                        )
            if recovery_decision.attempt_mqtt_rebuild and network_link_is_ready(
                network_stack
            ):
                if _should_verify_mqtt_before_rebuild(
                    transport,
                    recovery_state,
                    now_monotonic,
                ):
                    _print_log(
                        "recovery",
                        (
                            "mqtt action=verify_before_rebuild "
                            "last_success_s={:.1f}"
                        ).format(
                            max(
                                0.0,
                                float(now_monotonic)
                                - float(getattr(transport, "last_success_at", -1.0)),
                            )
                        ),
                        start_monotonic=start_monotonic,
                    )
                else:
                    mqtt_disconnect_reason = getattr(
                        transport,
                        "last_disconnect_reason",
                        "",
                    )
                    if not _should_rebuild_mqtt_adapter_for_recovery(
                        mqtt_adapter,
                        mqtt_disconnect_reason,
                    ):
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=hold_rebuild reason=broker_connect "
                                "elapsed_s={:.0f}"
                            ).format(
                                _mqtt_recovery_elapsed_s(
                                    recovery_state,
                                    now_monotonic,
                                )
                            ),
                            start_monotonic=start_monotonic,
                        )
                    else:
                        mqtt_station_reset = _mqtt_disconnect_reason_requires_rebuild(
                            mqtt_disconnect_reason
                        )
                        close_result = close_mqtt_client(mqtt_adapter, transport)
                        if mqtt_disconnect_reason:
                            transport.mark_disconnected(
                                reason=mqtt_disconnect_reason
                            )
                        if close_result.errors:
                            _print_log(
                                "recovery",
                                "mqtt close errors={}".format(
                                    ",".join(close_result.errors)
                                ),
                                start_monotonic=start_monotonic,
                            )
                        if mqtt_client_memory_failure_count > 0:
                            _collect_garbage()
                        if (
                            mqtt_station_reset
                            or getattr(network_stack, "socket_pool", None) is None
                        ):
                            network_stack = reconnect_network_stack(
                                runtime_config,
                                network_stack,
                                max_attempts=_recovery_reconnect_attempts(
                                    mqtt_station_reset
                                ),
                                retry_delay_s=_recovery_reconnect_delay_s(
                                    mqtt_station_reset
                                ),
                                rebuild_socket_artifacts=mqtt_station_reset,
                                reset_station=mqtt_station_reset,
                                log_start_monotonic=start_monotonic,
                            )
                        mqtt_adapter = build_mqtt_client_adapter(
                            runtime_config,
                            socket_pool=network_stack.socket_pool,
                            ssl_context=network_stack.ssl_context,
                        )
                        if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                            _collect_garbage()
                        (
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                        ) = _update_mqtt_memory_failure_window(
                            mqtt_adapter.errors,
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        )
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=rebuild broker={} socket_pool={} "
                                "station_reset={}"
                            ).format(
                                mqtt_adapter.active_broker
                                or mqtt_adapter.broker
                                or "none",
                                "ready"
                                if network_stack.socket_pool is not None
                                else "none",
                                1 if mqtt_station_reset else 0,
                            ),
                            start_monotonic=start_monotonic,
                        )
                        if _should_reboot_mqtt_memory_failures(
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        ):
                            reboot_reason = "mqtt_memory_allocation_failures"
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} "
                                    "elapsed_s={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    mqtt_client_memory_failure_count,
                                    int(
                                        max(
                                            0.0,
                                            float(now_monotonic)
                                            - float(
                                                mqtt_client_memory_failure_started_at
                                            ),
                                        )
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                network_stack=network_stack,
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
                and recovery_decision.allow_mqtt_connect
                and (
                    float(now_monotonic) - float(last_mqtt_connect_attempt_at)
                )
                >= _mqtt_connect_retry_interval_s(
                    recovery_state,
                    now_monotonic,
                    getattr(transport, "last_disconnect_reason", ""),
                )
            ):
                if mqtt_adapter.phase != "ready":
                    if _broker_ip_refresh_needed(
                        runtime_config,
                        settings_root=None,
                    ):
                        runtime_config, broker_ip_phase, broker_ip_errors = (
                            _refresh_broker_ip_from_hostname(
                                runtime_config,
                                network_stack,
                                settings_root=writable_settings_root,
                            )
                        )
                        if broker_ip_phase != "skipped":
                            _print_log(
                                "mqtt",
                                "broker_ip phase={} host={} ip={} errors={}".format(
                                    broker_ip_phase,
                                    runtime_config.mqtt.broker or "none",
                                    runtime_config.mqtt.broker_ip or "none",
                                    ",".join(broker_ip_errors)
                                    if broker_ip_errors
                                    else "none",
                                ),
                                start_monotonic=start_monotonic,
                            )
                    if mqtt_client_memory_failure_count > 0:
                        _collect_garbage()
                    mqtt_adapter = build_mqtt_client_adapter(
                        runtime_config,
                        socket_pool=network_stack.socket_pool,
                        ssl_context=network_stack.ssl_context,
                    )
                    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                        _collect_garbage()
                    (
                        mqtt_client_memory_failure_count,
                        mqtt_client_memory_failure_started_at,
                    ) = _update_mqtt_memory_failure_window(
                        mqtt_adapter.errors,
                        mqtt_client_memory_failure_count,
                        mqtt_client_memory_failure_started_at,
                        now_monotonic,
                    )
                    last_mqtt_connect_attempt_at = float(now_monotonic)
                    if mqtt_adapter.phase != "ready":
                        _print_log(
                            "mqtt",
                            "connect phase=deferred broker={} errors={}".format(
                                mqtt_adapter.broker or "none",
                                ",".join(mqtt_adapter.errors)
                                if mqtt_adapter.errors
                                else "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        if _should_reboot_mqtt_memory_failures(
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        ):
                            reboot_reason = "mqtt_memory_allocation_failures"
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} "
                                    "elapsed_s={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    mqtt_client_memory_failure_count,
                                    int(
                                        max(
                                            0.0,
                                            float(now_monotonic)
                                            - float(
                                                mqtt_client_memory_failure_started_at
                                            ),
                                        )
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                network_stack=network_stack,
                            )
                        await asyncio.sleep(0.05)
                        continue
                transport.mark_connect_requested()
                connect_started_at = time.monotonic()
                connect_result = connect_mqtt_client(
                    mqtt_adapter,
                    transport,
                    preflight=_should_preflight_broker(mqtt_adapter),
                )
                connect_finished_at = time.monotonic()
                mqtt_adapter = connect_result.adapter
                connect_phase = connect_result.phase
                last_mqtt_connect_attempt_at = float(connect_finished_at)
                if connect_phase == "connected":
                    repeated_mqtt_connect_failure_count = 0
                    repeated_mqtt_connect_failure_started_at = -1.0
                    mqtt_client_memory_failure_count = 0
                    mqtt_client_memory_failure_started_at = -1.0
                    _print_log(
                        "mqtt",
                        "connect phase={} broker={} elapsed_s={:.1f}".format(
                            connect_phase,
                            mqtt_adapter.active_broker or "none",
                            connect_finished_at - connect_started_at,
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
                    if (
                        mqtt_subscribe_recovery_pending
                        and _startup_subscription_recovery_drained(
                            sync_result,
                            transport,
                        )
                    ):
                        steady_state = replace(
                            steady_state,
                            connection_generation=transport.connection_generation,
                        )
                        mqtt_subscribe_recovery_pending = False
                        _print_log(
                            "mqtt",
                            (
                                "recovery phase=subscription_recovered "
                                "action=suppress_duplicate_startup_queue gen={} {}"
                            ).format(
                                transport.connection_generation,
                                _transport_queue_summary(transport),
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if _is_mqtt_subscription_failure(sync_result):
                        mqtt_subscribe_recovery_pending = True
                        mqtt_adapter = _recover_mqtt_subscription_failure(
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            start_monotonic=start_monotonic,
                        )
                        recovery_state = RecoveryState(
                            phase="mqtt",
                            phase_started_at=now_monotonic,
                            last_mqtt_rebuild_at=now_monotonic,
                        )
                        last_mqtt_connect_attempt_at = float(now_monotonic) - 5.0
                        _collect_garbage()
                        await asyncio.sleep(0.05)
                        continue
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
                    if _mqtt_connect_errors_are_repeated_failures(
                        connect_result.errors,
                        had_mqtt_success=float(
                            getattr(transport, "last_success_at", -1.0)
                        )
                        >= 0.0,
                        count_plain_connect_failures=rtc_valid_at_boot,
                    ):
                        if repeated_mqtt_connect_failure_count <= 0:
                            repeated_mqtt_connect_failure_started_at = float(
                                connect_finished_at
                            )
                        repeated_mqtt_connect_failure_count += 1
                        if _should_fast_reboot_mqtt_connect_failures(
                            repeated_mqtt_connect_failure_count,
                            repeated_mqtt_connect_failure_started_at,
                            connect_finished_at,
                        ):
                            reboot_reason = "mqtt_repeated_connect_failures"
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} "
                                    "elapsed_s={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    repeated_mqtt_connect_failure_count,
                                    int(
                                        max(
                                            0.0,
                                            float(connect_finished_at)
                                            - float(
                                                repeated_mqtt_connect_failure_started_at
                                            ),
                                        )
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                network_stack=network_stack,
                            )
                    else:
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
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
                        "dns_health={} ntp_health={} mqtt_connected={} "
                        "active_broker={} wifi_signature={}"
                    ).format(
                        network_stack.phase,
                        recovery_state.phase,
                        network_stack.ssid or "none",
                        network_stack.ip_address or "none",
                        _dns_health_text(network_stack, mqtt_adapter, ntp_state),
                        _ntp_health_text(ntp_state),
                        transport.connected,
                        mqtt_adapter.active_broker or "none",
                        network_error_signature(
                            network_stack,
                            had_ready_link=wifi_was_ready,
                        ),
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
                    if _is_mqtt_subscription_failure(sync_result):
                        mqtt_subscribe_recovery_pending = True
                        mqtt_adapter = _recover_mqtt_subscription_failure(
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            start_monotonic=start_monotonic,
                        )
                        recovery_state = RecoveryState(
                            phase="mqtt",
                            phase_started_at=now_monotonic,
                            last_mqtt_rebuild_at=now_monotonic,
                        )
                        last_mqtt_connect_attempt_at = float(now_monotonic) - 5.0
                        _collect_garbage()
                        await asyncio.sleep(0.05)
                        continue
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
        _teardown_network_for_shutdown(
            network_stack,
            start_monotonic=start_monotonic,
        )
        if loop_error is not None:
            _print_log(
                "runtime",
                "shutdown after fatal type={}".format(type(loop_error).__name__),
                start_monotonic=start_monotonic,
            )
