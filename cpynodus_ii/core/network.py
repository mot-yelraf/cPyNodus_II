"""Network stack bootstrap for station and AP runtime modes."""

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
        return NetworkStack(
            phase="ap",
            mode="ap",
            ssid=runtime_config.network.ap_ssid,
            hostname=runtime_config.network.hostname,
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

    if connection_manager_module is None:
        try:
            import adafruit_connection_manager as connection_manager_module  # type: ignore
        except ImportError:
            return NetworkStack(
                phase="unavailable",
                mode="station",
                ssid=runtime_config.network.ssid,
                hostname=runtime_config.network.hostname,
                errors=("connection_manager_unavailable",),
            )

    connect = getattr(wifi_radio, "connect", None)
    set_hostname = getattr(wifi_radio, "hostname", None)
    last_exc = None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            if callable(connect) and runtime_config.network.ssid:
                connect(runtime_config.network.ssid, runtime_config.network.password)
            if runtime_config.network.hostname and set_hostname is not None:
                try:
                    wifi_radio.hostname = runtime_config.network.hostname
                except Exception:
                    pass
            socket_pool = connection_manager_module.get_radio_socketpool(wifi_radio)
            ssl_context = connection_manager_module.get_radio_ssl_context(wifi_radio)
            ip_address = str(getattr(wifi_radio, "ipv4_address", "") or "")
            return NetworkStack(
                phase="ready",
                mode="station",
                ssid=runtime_config.network.ssid,
                hostname=runtime_config.network.hostname,
                ip_address=ip_address,
                socket_pool=socket_pool,
                ssl_context=ssl_context,
                errors=(),
            )
        except Exception as exc:
            last_exc = exc
            if _looks_auth_failure(exc):
                return NetworkStack(
                    phase="error",
                    mode="station",
                    ssid=runtime_config.network.ssid,
                    hostname=runtime_config.network.hostname,
                    errors=("network_auth_failed", str(exc), "attempt={}".format(attempt)),
                )
            if attempt < attempts:
                try:
                    time.sleep(float(retry_delay_s or 0.0))
                except Exception:
                    pass

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
