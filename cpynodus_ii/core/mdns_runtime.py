"""Lazy mDNS helpers for HTTP-capable runtime modes.

This module is intentionally not imported by normal MQTT profile startup.
Keep mDNS server setup here so Sensorius, WeeWX, and Home Assistant steady
state do not keep the helper code resident unless HTTP-mode discovery is used.
"""


def build_mdns_artifact(
    runtime_config,
    wifi_radio,
    *,
    mdns_module=None,
    log_start_monotonic=None,
    advertise_http=False,
    network_log=None,
):
    """Build an mDNS server artifact for an active station radio."""
    hostname = _mdns_hostname(getattr(runtime_config.network, "hostname", ""))
    if not hostname:
        return None, "", False, "skipped", ("mdns_hostname_missing",)
    mdns_module = _resolve_mdns_module(mdns_module)
    if mdns_module is None:
        return None, "", False, "unavailable", ("mdns_module_unavailable",)
    server_cls = getattr(mdns_module, "Server", None)
    if not callable(server_cls):
        return None, "", False, "unavailable", ("mdns_server_unavailable",)
    server = None
    errors = ()
    try:
        server = server_cls(wifi_radio)
    except Exception as exc:
        errors = ("mdns_server_failed:{}".format(_exception_label(exc)), str(exc))
        _log_mdns_result(
            "error",
            hostname,
            False,
            errors,
            start_monotonic=log_start_monotonic,
            network_log=network_log,
        )
        return None, "", False, "error", errors
    try:
        server.hostname = hostname
    except Exception as exc:
        errors += ("mdns_hostname_failed:{}".format(_exception_label(exc)), str(exc))
    try:
        server.instance_name = hostname
    except Exception:
        pass

    http_advertised = False
    if bool(advertise_http):
        advertise_service = getattr(server, "advertise_service", None)
        if callable(advertise_service):
            try:
                advertise_service(
                    service_type="_http",
                    protocol="_tcp",
                    port=_mdns_http_port(runtime_config),
                )
                http_advertised = True
            except Exception as exc:
                errors += (
                    "mdns_http_failed:{}".format(_exception_label(exc)),
                    str(exc),
                )
        else:
            errors += ("mdns_http_unavailable",)

    status = "ready" if not errors else "partial"
    _log_mdns_result(
        status,
        hostname,
        http_advertised,
        errors,
        start_monotonic=log_start_monotonic,
        network_log=network_log,
    )
    return server, hostname, http_advertised, status, errors


def deinit_mdns_server(network_stack):
    """Deinitialize the mDNS server held by a network stack."""
    server = getattr(network_stack, "mdns_server", None)
    if server is None:
        return False
    deinit = getattr(server, "deinit", None)
    if not callable(deinit):
        return False
    try:
        deinit()
    except Exception:
        return False
    return True


def _resolve_mdns_module(mdns_module):
    if mdns_module is not None:
        return mdns_module
    try:
        import mdns as mdns_module  # type: ignore
    except ImportError:
        return None
    return mdns_module


def _mdns_hostname(value):
    text = str(value or "").strip().strip(".").lower()
    if text.endswith(".local"):
        text = text[:-6].strip(".")
    cleaned = ""
    previous_dash = False
    for char in text:
        valid = "a" <= char <= "z" or "0" <= char <= "9"
        if valid:
            cleaned += char
            previous_dash = False
        elif char in {"-", "_", " "} and cleaned and not previous_dash:
            cleaned += "-"
            previous_dash = True
    return cleaned.strip("-")[:63]


def _mdns_http_port(runtime_config):
    try:
        return int(getattr(runtime_config.network, "http_port", 8000) or 8000)
    except Exception:
        return 8000


def _log_mdns_result(
    status,
    hostname,
    http_advertised,
    errors,
    *,
    start_monotonic=None,
    network_log=None,
):
    if not callable(network_log):
        return
    network_log(
        "network mdns phase={} host={}.local service={} errors={}".format(
            status,
            hostname or "none",
            "http" if http_advertised else "none",
            ",".join(errors) if errors else "none",
        ),
        start_monotonic=start_monotonic,
    )


def _exception_label(exc):
    if exc is None:
        return "exception=none"
    return "exception={}".format(type(exc).__name__)
