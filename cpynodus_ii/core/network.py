"""Build and manage Wi-Fi network state for station and AP modes.

The network helpers in this module create the shared radio, socket pool, and
HTTP session resources used by web, MQTT, and time-sync features while keeping
station-mode and access-point behavior explicit.
"""

from dataclasses import dataclass
import time


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
):
    """Build the runtime network stack needed by MQTT and networked profiles."""
    if runtime_config.ap_mode:
        wifi_radio = _resolve_wifi_radio(wifi_radio)
        connection_manager_module = _resolve_connection_manager(connection_manager_module)
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
                        channel=int(getattr(runtime_config.network, "ap_channel", 6) or 6),
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
                    socket_pool = connection_manager_module.get_radio_socketpool(wifi_radio)
                    ssl_context = connection_manager_module.get_radio_ssl_context(wifi_radio)
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

    if not (runtime_config.mqtt_enabled or runtime_config.web_enabled or runtime_config.ntp_enabled):
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
):
    """Reconnect station Wi-Fi, preserving socket artifacts when allowed."""
    wifi_radio = getattr(network_stack, "wifi_radio", None)
    connection_manager_module = getattr(network_stack, "connection_manager_module", None)
    if wifi_radio is None:
        return build_network_stack(
            runtime_config,
            max_attempts=max_attempts,
            retry_delay_s=retry_delay_s,
        )

    connect_result = _connect_station(
        runtime_config,
        wifi_radio,
        max_attempts=max_attempts,
        retry_delay_s=retry_delay_s,
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
        connection_manager_module = _resolve_connection_manager(connection_manager_module)
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

    return NetworkStack(
        phase="error",
        mode="station",
        ssid=runtime_config.network.ssid,
        hostname=runtime_config.network.hostname,
        ip_address="",
        socket_pool=None,
        ssl_context=None,
        errors=("network_connect_failed", str(last_exc or ""), "attempts={}".format(attempts)),
    )


def _looks_auth_failure(exc):
    text = str(exc or "").strip().lower()
    return (
        "authentication failure" in text
        or "wrong password" in text
        or "bad password" in text
        or "auth" in text and "fail" in text
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


def _connect_station(runtime_config, wifi_radio, *, max_attempts, retry_delay_s):
    connect = getattr(wifi_radio, "connect", None)
    set_hostname = getattr(wifi_radio, "hostname", None)
    last_exc = None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            if callable(connect):
                connect(runtime_config.network.ssid, runtime_config.network.password)
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
            ip_address = _current_ip_address(wifi_radio)
            if _looks_like_nodus_ap_station_ip(ip_address, runtime_config):
                return {
                    "phase": "error",
                    "ip_address": "",
                    "errors": (
                        "network_ap_subnet_suspect",
                        ip_address,
                        "expected_ssid={}".format(runtime_config.network.ssid),
                    )
                    + _network_diagnostic_tokens(wifi_radio),
                }
            return {
                "phase": "ready",
                "ip_address": ip_address,
                "errors": (),
            }
        except Exception as exc:
            last_exc = exc
            if _looks_auth_failure(exc):
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
                try:
                    time.sleep(float(retry_delay_s or 0.0))
                except Exception:
                    pass
    return {
        "phase": "error",
        "ip_address": _current_ip_address(wifi_radio),
        "errors": (
            "network_connect_failed",
            _exception_label(last_exc),
            str(last_exc or ""),
            "attempts={}".format(attempts),
        )
        + _network_diagnostic_tokens(wifi_radio),
    }


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


def _exception_label(exc):
    if exc is None:
        return "exception=none"
    return "exception={}".format(type(exc).__name__)


def _looks_like_nodus_ap_station_ip(ip_address, runtime_config):
    text = str(ip_address or "").strip()
    if not text.startswith("192.168.4."):
        return False
    return runtime_config.network.ssid != runtime_config.network.ap_ssid
