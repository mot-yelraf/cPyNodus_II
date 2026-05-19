"""Build and manage Wi-Fi network state for station and AP modes.

The network helpers in this module create the shared radio, socket pool, and
HTTP session resources used by web, MQTT, and time-sync features while keeping
station-mode and access-point behavior explicit.
"""

import time
from dataclasses import dataclass

RADIO_CYCLE_SETTLE_S = 3.0
RADIO_ENABLE_TOGGLE_SETTLE_S = 0.5


def _network_log(message, *, start_monotonic=None):
    """Print a network diagnostic line with a compact timestamp prefix."""
    try:
        now = time.localtime()
    except Exception:
        now = None
    if now is not None:
        try:
            if int(now[0]) >= 2023:
                stamp = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                    int(now[0]),
                    int(now[1]),
                    int(now[2]),
                    int(now[3]),
                    int(now[4]),
                    int(now[5]),
                )
                print("{} {}".format(stamp, message))
                return
        except Exception:
            pass
    try:
        now_mono = float(time.monotonic())
    except Exception:
        now_mono = 0.0
    if start_monotonic is None:
        elapsed = max(0, int(now_mono))
    else:
        try:
            elapsed = max(0, int(now_mono - float(start_monotonic)))
        except Exception:
            elapsed = max(0, int(now_mono))
    print("{}s {}".format(elapsed, message))


@dataclass(frozen=True)
class NetworkStack:
    """Describe the active network bootstrap state."""

    phase: str
    mode: str
    ssid: str
    hostname: str
    ip_address: str = ""
    socket_pool: object | None = None
    ssl_context: object | None = None
    wifi_radio: object | None = None
    connection_manager_module: object | None = None
    errors: tuple = ()


def build_network_stack(
    runtime_config,
    *,
    wifi_radio=None,
    connection_manager_module=None,
    max_attempts=3,
    retry_delay_s=1.0,
    log_start_monotonic=None,
):
    """Build the runtime network stack needed by MQTT and networked profiles."""
    if runtime_config.ap_mode:
        wifi_radio = _resolve_wifi_radio(wifi_radio)
        connection_manager_module = _resolve_connection_manager(
            connection_manager_module
        )
        ap_ip_address = ""
        socket_pool = None
        ssl_context = None
        if wifi_radio is not None:
            start_ap = getattr(wifi_radio, "start_ap", None)
            if callable(start_ap):
                try:
                    start_ap(
                        runtime_config.network.ap_ssid,
                        runtime_config.network.ap_password,
                        channel=int(
                            getattr(runtime_config.network, "ap_channel", 6) or 6
                        ),
                    )
                except TypeError:
                    try:
                        start_ap(
                            runtime_config.network.ap_ssid,
                            runtime_config.network.ap_password,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
            ap_ip_address = _current_ap_ip_address(wifi_radio)
            if connection_manager_module is not None:
                try:
                    socket_pool = connection_manager_module.get_radio_socketpool(
                        wifi_radio
                    )
                    ssl_context = connection_manager_module.get_radio_ssl_context(
                        wifi_radio
                    )
                except Exception:
                    socket_pool = None
                    ssl_context = None
        return NetworkStack(
            phase="ap",
            mode="ap",
            ssid=runtime_config.network.ap_ssid,
            hostname=runtime_config.network.hostname,
            ip_address=ap_ip_address,
            socket_pool=socket_pool,
            ssl_context=ssl_context,
            wifi_radio=wifi_radio,
            connection_manager_module=connection_manager_module,
            errors=(),
        )

    if not (
        runtime_config.mqtt_enabled
        or runtime_config.web_enabled
        or runtime_config.ntp_enabled
    ):
        return NetworkStack(
            phase="inactive",
            mode="inactive",
            ssid="",
            hostname=runtime_config.network.hostname,
            errors=(),
        )

    if not runtime_config.network.ssid:
        return NetworkStack(
            phase="error",
            mode="station",
            ssid="",
            hostname=runtime_config.network.hostname,
            errors=("network_ssid_missing",),
        )

    if wifi_radio is None:
        try:
            import wifi  # type: ignore
        except ImportError:
            return NetworkStack(
                phase="unavailable",
                mode="station",
                ssid=runtime_config.network.ssid,
                hostname=runtime_config.network.hostname,
                errors=("wifi_module_unavailable",),
            )
        wifi_radio = getattr(wifi, "radio", None)

    if wifi_radio is None:
        return NetworkStack(
            phase="unavailable",
            mode="station",
            ssid=runtime_config.network.ssid,
            hostname=runtime_config.network.hostname,
            errors=("wifi_radio_unavailable",),
        )

    connection_manager_module = _resolve_connection_manager(connection_manager_module)
    if connection_manager_module is None:
        return NetworkStack(
            phase="unavailable",
            mode="station",
            ssid=runtime_config.network.ssid,
            hostname=runtime_config.network.hostname,
            errors=("connection_manager_unavailable",),
        )

    connect_result = _connect_station(
        runtime_config,
        wifi_radio,
        max_attempts=max_attempts,
        retry_delay_s=retry_delay_s,
        log_start_monotonic=log_start_monotonic,
    )
    if connect_result["phase"] != "ready":
        return NetworkStack(
            phase=connect_result["phase"],
            mode="station",
            ssid=runtime_config.network.ssid,
            hostname=runtime_config.network.hostname,
            ip_address=connect_result["ip_address"],
            wifi_radio=wifi_radio,
            connection_manager_module=connection_manager_module,
            errors=connect_result["errors"],
        )

    socket_pool = connection_manager_module.get_radio_socketpool(wifi_radio)
    ssl_context = connection_manager_module.get_radio_ssl_context(wifi_radio)
    return NetworkStack(
        phase="ready",
        mode="station",
        ssid=runtime_config.network.ssid,
        hostname=runtime_config.network.hostname,
        ip_address=connect_result["ip_address"],
        socket_pool=socket_pool,
        ssl_context=ssl_context,
        wifi_radio=wifi_radio,
        connection_manager_module=connection_manager_module,
        errors=(),
    )


def reconnect_network_stack(
    runtime_config,
    network_stack,
    *,
    max_attempts=1,
    retry_delay_s=0.0,
    rebuild_socket_artifacts=False,
    reset_station=False,
    cycle_radio=False,
    log_start_monotonic=None,
):
    """Reconnect station Wi-Fi, preserving socket artifacts when allowed."""
    wifi_radio = getattr(network_stack, "wifi_radio", None)
    connection_manager_module = getattr(
        network_stack, "connection_manager_module", None
    )
    if wifi_radio is None:
        return build_network_stack(
            runtime_config,
            max_attempts=max_attempts,
            retry_delay_s=retry_delay_s,
            log_start_monotonic=log_start_monotonic,
        )

    if reset_station:
        _reset_station_mode(wifi_radio, cycle_radio=cycle_radio)
        if cycle_radio:
            try:
                time.sleep(RADIO_CYCLE_SETTLE_S)
            except Exception:
                pass

    connect_result = _connect_station(
        runtime_config,
        wifi_radio,
        max_attempts=max_attempts,
        retry_delay_s=retry_delay_s,
        log_start_monotonic=log_start_monotonic,
    )
    if connect_result["phase"] != "ready":
        return NetworkStack(
            phase=connect_result["phase"],
            mode="station",
            ssid=runtime_config.network.ssid,
            hostname=runtime_config.network.hostname,
            ip_address=connect_result["ip_address"],
            socket_pool=None if rebuild_socket_artifacts else network_stack.socket_pool,
            ssl_context=None if rebuild_socket_artifacts else network_stack.ssl_context,
            wifi_radio=wifi_radio,
            connection_manager_module=connection_manager_module,
            errors=connect_result["errors"],
        )

    socket_pool = network_stack.socket_pool
    ssl_context = network_stack.ssl_context
    if rebuild_socket_artifacts or socket_pool is None or ssl_context is None:
        connection_manager_module = _resolve_connection_manager(
            connection_manager_module
        )
        if connection_manager_module is None:
            return NetworkStack(
                phase="unavailable",
                mode="station",
                ssid=runtime_config.network.ssid,
                hostname=runtime_config.network.hostname,
                ip_address=connect_result["ip_address"],
                wifi_radio=wifi_radio,
                connection_manager_module=None,
                errors=("connection_manager_unavailable",),
            )
        socket_pool = connection_manager_module.get_radio_socketpool(wifi_radio)
        ssl_context = connection_manager_module.get_radio_ssl_context(wifi_radio)
    return NetworkStack(
        phase="ready",
        mode="station",
        ssid=runtime_config.network.ssid,
        hostname=runtime_config.network.hostname,
        ip_address=connect_result["ip_address"],
        socket_pool=socket_pool,
        ssl_context=ssl_context,
        wifi_radio=wifi_radio,
        connection_manager_module=connection_manager_module,
        errors=(),
    )


def network_link_is_ready(network_stack):
    """Return whether the active Wi-Fi link still appears usable."""
    if getattr(network_stack, "phase", "") != "ready":
        return False
    wifi_radio = getattr(network_stack, "wifi_radio", None)
    if wifi_radio is None:
        return bool(getattr(network_stack, "ip_address", ""))
    return bool(_current_ip_address(wifi_radio))


def network_error_signature(network_stack, *, had_ready_link=False):
    """Return a compact signature for the current station failure mode."""
    if getattr(network_stack, "phase", "") == "ready":
        return "none"
    errors = getattr(network_stack, "errors", ()) or ()
    text = " ".join(str(error or "") for error in errors).lower()
    if "network_auth_failed" in errors:
        return "station_auth_failed"
    if "network_ap_subnet_suspect" in errors:
        return "station_ap_subnet"
    if "no network with that ssid" in text:
        if had_ready_link:
            return "station_scan_miss_after_ready"
        return "station_scan_miss"
    if "unknown failure" in text:
        if had_ready_link:
            return "station_unknown_after_ready"
        return "station_unknown"
    if "network_not_connected" in errors:
        return "station_not_connected"
    if getattr(network_stack, "phase", "") in {"error", "unavailable"}:
        return "station_connect_failed"
    return "none"


def teardown_network_stack(network_stack, *, cycle_radio=False):
    """Disconnect station networking before a runtime reload."""
    wifi_radio = getattr(network_stack, "wifi_radio", None)
    if wifi_radio is None:
        return False
    disconnected = _call_radio_method(wifi_radio, "disconnect")
    stopped_station = _call_radio_method(wifi_radio, "stop_station")
    stopped_ap = _call_radio_method(wifi_radio, "stop_ap")
    cycled_radio = _cycle_radio_power(wifi_radio) if cycle_radio else False
    return bool(disconnected or stopped_station or stopped_ap or cycled_radio)


def refresh_network_stack(network_stack):
    """Refresh cached IP metadata without rebuilding network artifacts."""
    wifi_radio = getattr(network_stack, "wifi_radio", None)
    if wifi_radio is None:
        return network_stack
    ip_address = _current_ip_address(wifi_radio)
    if ip_address == getattr(network_stack, "ip_address", ""):
        return network_stack
    return NetworkStack(
        phase=network_stack.phase,
        mode=network_stack.mode,
        ssid=network_stack.ssid,
        hostname=network_stack.hostname,
        ip_address=ip_address,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
        wifi_radio=network_stack.wifi_radio,
        connection_manager_module=network_stack.connection_manager_module,
        errors=network_stack.errors,
    )

def _looks_auth_failure(exc):
    text = str(exc or "").strip().lower()
    return (
        "authentication failure" in text
        or "wrong password" in text
        or "bad password" in text
        or "auth" in text
        and "fail" in text
    )


def _looks_scan_miss(exc):
    text = str(exc or "").strip().lower()
    return "no network with that ssid" in text


def _scan_miss_password_suffix(runtime_config, exc):
    if not _looks_scan_miss(exc):
        return ""
    return " {}".format(_wifi_password_debug_token(runtime_config))


def _scan_miss_password_tokens(runtime_config, exc):
    if not _looks_scan_miss(exc):
        return ()
    return (_wifi_password_debug_token(runtime_config),)


def _wifi_password_debug_token(runtime_config):
    password = getattr(getattr(runtime_config, "network", None), "password", "")
    if password is None:
        password = ""
    return "password={}".format(password)


def _looks_station_join_failure(exc):
    text = str(exc or "").strip().lower()
    return (
        "no network with that ssid" in text
        or "unknown failure" in text
        or "ehostunreach" in text
        or "einprogress" in text
    )


def _resolve_wifi_radio(wifi_radio):
    if wifi_radio is not None:
        return wifi_radio
    try:
        import wifi  # type: ignore
    except ImportError:
        return None
    return getattr(wifi, "radio", None)


def _resolve_connection_manager(connection_manager_module):
    if connection_manager_module is not None:
        return connection_manager_module
    try:
        import adafruit_connection_manager as connection_manager_module  # type: ignore
    except ImportError:
        return None
    return connection_manager_module


def _connect_station(
    runtime_config,
    wifi_radio,
    *,
    max_attempts,
    retry_delay_s,
    log_start_monotonic=None,
):
    connect = getattr(wifi_radio, "connect", None)
    set_hostname = getattr(wifi_radio, "hostname", None)
    last_exc = None
    last_scan_status = ""
    last_scan_count = -1
    station_hint = None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        _network_log(
            "network connect attempt={} ssid={}".format(
                attempt,
                runtime_config.network.ssid,
            ),
            start_monotonic=log_start_monotonic,
        )
        try:
            _prepare_station_mode(runtime_config, wifi_radio)
            _reset_stale_station_link(runtime_config, wifi_radio)
            if callable(connect):
                hint_strategy = _station_hint_strategy(attempt, station_hint)
                if hint_strategy:
                    _network_log(
                        (
                            "network connect hint attempt={} strategy={} "
                            "channel={} bssid={}"
                        ).format(
                            attempt,
                            hint_strategy,
                            _station_hint_channel(station_hint) or "unknown",
                            _bssid_text(_station_hint_bssid(station_hint)),
                        ),
                        start_monotonic=log_start_monotonic,
                    )
                _connect_with_station_hint(
                    connect,
                    runtime_config,
                    station_hint,
                    hint_strategy,
                )
            if runtime_config.network.hostname and set_hostname is not None:
                try:
                    wifi_radio.hostname = runtime_config.network.hostname
                except Exception:
                    pass
            connected = _safe_radio_attr(wifi_radio, "connected")
            if connected is False:
                return {
                    "phase": "error",
                    "ip_address": _current_ip_address(wifi_radio),
                    "errors": (
                        "network_not_connected",
                        "expected={}".format(runtime_config.network.ssid),
                    )
                    + _network_diagnostic_tokens(wifi_radio),
                }
            actual_ssid = _current_station_ssid(wifi_radio)
            if actual_ssid and actual_ssid != runtime_config.network.ssid:
                return {
                    "phase": "error",
                    "ip_address": "",
                    "errors": (
                        "network_wrong_ssid",
                        actual_ssid,
                        "expected={}".format(runtime_config.network.ssid),
                    )
                    + _network_diagnostic_tokens(wifi_radio),
                }
            ip_address = _station_ip_address(wifi_radio)
            if _station_ip_is_ap_subnet(ip_address, runtime_config):
                last_exc = RuntimeError("network_ap_subnet_suspect")
                if attempt < attempts:
                    _network_log(
                        (
                            "network connect retry attempt={} "
                            "reason=ap_subnet ip={}"
                        ).format(
                            attempt,
                            ip_address or "none",
                        ),
                        start_monotonic=log_start_monotonic,
                    )
                    _reset_station_mode(wifi_radio)
                    try:
                        time.sleep(float(retry_delay_s or 0.0))
                    except Exception:
                        pass
                    continue
                return {
                    "phase": "error",
                    "ip_address": ip_address,
                    "errors": (
                        "network_ap_subnet_suspect",
                        ip_address,
                        "expected_ssid={}".format(runtime_config.network.ssid),
                    )
                    + _network_diagnostic_tokens(wifi_radio),
                }
            _network_log(
                "network connect ready attempt={} ip={}".format(
                    attempt,
                    ip_address or "none",
                ),
                start_monotonic=log_start_monotonic,
            )
            return {
                "phase": "ready",
                "ip_address": ip_address,
                "errors": (),
            }
        except Exception as exc:
            last_exc = exc
            if _looks_auth_failure(exc):
                if attempt < attempts:
                    _network_log(
                        "network connect auth_retry attempt={} error={}".format(
                            attempt,
                            str(exc),
                        ),
                        start_monotonic=log_start_monotonic,
                    )
                    _reset_station_mode(wifi_radio)
                    try:
                        time.sleep(float(retry_delay_s or 0.0))
                    except Exception:
                        pass
                    continue
                _network_log(
                    "network connect auth_error attempt={} error={}".format(
                        attempt,
                        str(exc),
                    ),
                    start_monotonic=log_start_monotonic,
                )
                return {
                    "phase": "error",
                    "ip_address": _current_ip_address(wifi_radio),
                    "errors": (
                        "network_auth_failed",
                        _exception_label(exc),
                        str(exc),
                        "attempt={}".format(attempt),
                    )
                    + _network_diagnostic_tokens(wifi_radio),
                }
            if attempt < attempts:
                scan_status = ""
                scan_count = -1
                scan_hint = None
                if _looks_station_join_failure(exc):
                    scan_status, scan_count, scan_hint = _scan_for_station_ssid(
                        wifi_radio,
                        runtime_config.network.ssid,
                    )
                    last_scan_status = scan_status
                    last_scan_count = scan_count
                    if scan_hint is not None:
                        station_hint = scan_hint
                    _network_log(
                        (
                            "network scan attempt={} ssid={} result={} count={} "
                            "channel={} bssid={}"
                        ).format(
                            attempt,
                            runtime_config.network.ssid,
                            scan_status,
                            scan_count if scan_count >= 0 else "unknown",
                            _station_hint_channel(scan_hint) or "none",
                            _bssid_text(_station_hint_bssid(scan_hint)),
                        ),
                        start_monotonic=log_start_monotonic,
                    )
                _network_log(
                    "network connect retry attempt={} error={}{}".format(
                        attempt,
                        str(exc),
                        _scan_miss_password_suffix(runtime_config, exc),
                    ),
                    start_monotonic=log_start_monotonic,
                )
                _reset_station_mode(wifi_radio)
                try:
                    settle_s = float(retry_delay_s or 0.0)
                    if scan_status:
                        settle_s = max(settle_s, 2.0)
                    time.sleep(settle_s)
                except Exception:
                    pass
    _network_log(
        "network connect error attempts={} error={}{}".format(
            attempts,
            str(last_exc or ""),
            _scan_miss_password_suffix(runtime_config, last_exc),
        ),
        start_monotonic=log_start_monotonic,
    )
    return {
        "phase": "error",
        "ip_address": _current_ip_address(wifi_radio),
        "errors": (
            "network_connect_failed",
            _exception_label(last_exc),
            str(last_exc or ""),
            "attempts={}".format(attempts),
        )
        + _network_diagnostic_tokens(wifi_radio)
        + _scan_miss_password_tokens(runtime_config, last_exc)
        + _scan_diagnostic_tokens(last_scan_status, last_scan_count),
    }


def _prepare_station_mode(runtime_config, wifi_radio):
    if runtime_config.network.ssid == runtime_config.network.ap_ssid:
        return
    _call_radio_method(wifi_radio, "stop_ap")


def _reset_stale_station_link(runtime_config, wifi_radio):
    actual_ssid = _current_station_ssid(wifi_radio)
    if not (actual_ssid and actual_ssid != runtime_config.network.ssid):
        return
    _reset_station_mode(wifi_radio)


def _reset_station_mode(wifi_radio, *, cycle_radio=False):
    _call_radio_method(wifi_radio, "disconnect")
    stopped_station = _call_radio_method(wifi_radio, "stop_station")
    cycled_radio = _cycle_radio_power(wifi_radio) if cycle_radio else False
    if stopped_station or cycled_radio:
        _call_radio_method(wifi_radio, "start_station")


def _connect_with_station_hint(connect, runtime_config, station_hint, strategy):
    ssid = runtime_config.network.ssid
    password = runtime_config.network.password
    channel = _station_hint_channel(station_hint)
    bssid = _station_hint_bssid(station_hint)
    if strategy == "bssid" and channel and bssid:
        try:
            return connect(ssid, password, channel=int(channel), bssid=bssid)
        except TypeError:
            pass
    if strategy == "channel" and channel:
        try:
            return connect(ssid, password, channel=int(channel))
        except TypeError:
            pass
    return connect(ssid, password)


def _station_hint_strategy(attempt, station_hint):
    if station_hint is None:
        return ""
    try:
        attempt_number = int(attempt or 0)
    except Exception:
        attempt_number = 0
    if attempt_number > 0 and attempt_number % 2 == 0:
        return "channel"
    return ""


def _cycle_radio_power(wifi_radio):
    changed = False
    try:
        setattr(wifi_radio, "enabled", False)
        changed = True
    except Exception:
        pass
    if changed:
        try:
            time.sleep(RADIO_ENABLE_TOGGLE_SETTLE_S)
        except Exception:
            pass
    try:
        setattr(wifi_radio, "enabled", True)
        changed = True
    except Exception:
        pass
    return changed


def _scan_for_station_ssid(wifi_radio, ssid):
    start_scan = getattr(wifi_radio, "start_scanning_networks", None)
    if not callable(start_scan):
        return "unavailable", -1, None
    count = 0
    best_hint = None
    try:
        networks = start_scan()
        for network in networks:
            count += 1
            if _network_ssid_text(_safe_radio_attr(network, "ssid")) == str(ssid):
                best_hint = _best_station_hint(best_hint, network)
            if count >= 16:
                break
    except Exception:
        return "error", count, best_hint
    finally:
        _call_radio_method(wifi_radio, "stop_scanning_networks")
    if best_hint is not None:
        return "found", count, best_hint
    return "missing", count, None


def _best_station_hint(current_hint, network):
    channel = _safe_radio_attr(network, "channel")
    bssid = _safe_radio_attr(network, "bssid")
    rssi = _rssi_value(_safe_radio_attr(network, "rssi"))
    candidate = (channel, bssid, rssi)
    if current_hint is None:
        return candidate
    if rssi > _station_hint_rssi(current_hint):
        return candidate
    return current_hint


def _station_hint_channel(station_hint):
    try:
        return station_hint[0]
    except Exception:
        return None


def _station_hint_bssid(station_hint):
    try:
        return station_hint[1]
    except Exception:
        return None


def _station_hint_rssi(station_hint):
    try:
        return int(station_hint[2])
    except Exception:
        return -999


def _rssi_value(value):
    try:
        return int(value)
    except Exception:
        return -999


def _bssid_text(value):
    if isinstance(value, (bytes, bytearray)):
        return ":".join("{:02x}".format(byte) for byte in value)
    if value:
        return str(value)
    return "none"


def _call_radio_method(wifi_radio, name):
    method = getattr(wifi_radio, name, None)
    if not callable(method):
        return False
    try:
        method()
        return True
    except Exception:
        return False


def _current_ip_address(wifi_radio):
    for attr_name in ("ipv4_address", "ipv4_address_ap"):
        value = _safe_radio_attr(wifi_radio, attr_name)
        if value:
            return str(value)
    return ""


def _station_ip_address(wifi_radio):
    value = _safe_radio_attr(wifi_radio, "ipv4_address")
    if value:
        return str(value)
    return ""


def _current_ap_ip_address(wifi_radio):
    value = _safe_radio_attr(wifi_radio, "ipv4_address_ap")
    if value:
        return str(value)
    return ""


def _current_station_ssid(wifi_radio):
    ap_info = _safe_radio_attr(wifi_radio, "ap_info")
    ssid = _safe_radio_attr(ap_info, "ssid")
    if ssid:
        return str(ssid)
    return ""


def _station_ip_is_ap_subnet(ip_address, runtime_config):
    text = str(ip_address or "").strip()
    return (
        text.startswith("192.168.4.")
        and runtime_config.network.ssid != runtime_config.network.ap_ssid
    )


def _safe_radio_attr(target, name):
    try:
        return getattr(target, name, "")
    except Exception:
        return ""


def _network_diagnostic_tokens(wifi_radio):
    tokens = ()
    connected = _safe_radio_attr(wifi_radio, "connected")
    station_ip = _station_ip_address(wifi_radio)
    ap_ip = _current_ap_ip_address(wifi_radio)
    actual_ssid = _current_station_ssid(wifi_radio)
    if isinstance(connected, bool):
        tokens += ("connected={}".format(connected),)
    if station_ip:
        tokens += ("station_ip={}".format(station_ip),)
    if ap_ip:
        tokens += ("ap_ip={}".format(ap_ip),)
    if actual_ssid:
        tokens += ("actual_ssid={}".format(actual_ssid),)
    return tokens


def _scan_diagnostic_tokens(scan_status, scan_count):
    if not scan_status:
        return ()
    count_text = str(scan_count) if int(scan_count or -1) >= 0 else "unknown"
    return (
        "scan={}".format(scan_status),
        "scan_count={}".format(count_text),
    )


def _network_ssid_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return str(value)
    return str(value or "")


def _exception_label(exc):
    if exc is None:
        return "exception=none"
    return "exception={}".format(type(exc).__name__)
