"""REPL-run Wi-Fi and network probe for CircuitPython Nodus devices.

From the CircuitPython REPL:

    from testApparatus import wifi_network_probe
    wifi_network_probe.run()

Or, when copied to the CIRCUITPY root:

    import wifi_network_probe
    wifi_network_probe.run(ssid="PeaceHill", password="...")
"""

import time


def run(
    *,
    ssid=None,
    password=None,
    scans=3,
    attempts=3,
    scan_limit=24,
    delay_s=2.0,
    connect=True,
    resolve=True,
    tcp=True,
    reset_station=True,
):
    """Scan Wi-Fi, connect with settings or supplied credentials, and test MQTT."""
    runtime_config, errors = _load_runtime_config()
    if runtime_config is None:
        _log("settings phase=error errors={}".format(_join_errors(errors)))
        return False

    network_config = runtime_config.network
    mqtt_config = runtime_config.mqtt
    config_ssid = str(getattr(network_config, "ssid", "") or "").strip()
    config_password = str(getattr(network_config, "password", "") or "")
    ssid_source = "argument" if ssid is not None else "settings"
    password_source = "argument" if password is not None else "settings"
    ssid = str(ssid if ssid is not None else config_ssid).strip()
    password = str(password if password is not None else config_password)
    hostname = str(getattr(network_config, "hostname", "") or "").strip()

    _log("probe phase=start")
    _log(
        "settings ssid={} ssid_source={} password_present={} "
        "password_source={} hostname={} broker={} broker_ip={} port={}".format(
            ssid or "none",
            ssid_source,
            bool(password),
            password_source,
            hostname or "none",
            getattr(mqtt_config, "broker", "") or "none",
            getattr(mqtt_config, "broker_ip", "") or "none",
            getattr(mqtt_config, "port", 1883) or 1883,
        )
    )
    if not ssid:
        _log("settings phase=error errors=ssid_missing")
        return False

    try:
        import wifi  # type: ignore
    except Exception as exc:
        _log("wifi phase=error errors=wifi_import_failed:{}".format(exc))
        return False

    radio = getattr(wifi, "radio", None)
    if radio is None:
        _log("wifi phase=error errors=wifi_radio_unavailable")
        return False

    _log_radio_state(radio, "initial")
    for index in range(max(0, int(scans or 0))):
        scan_once(radio=radio, target_ssid=ssid, limit=scan_limit, label=index + 1)
        _sleep(delay_s)

    if not connect:
        _log("probe phase=done connected=False")
        return True

    connected = False
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        if reset_station:
            _reset_station(radio)
            _log_radio_state(radio, "after_reset")
        _log("connect attempt={} ssid={}".format(attempt, ssid))
        started = _monotonic()
        try:
            radio.connect(ssid, password)
            if hostname:
                try:
                    radio.hostname = hostname
                except Exception as exc:
                    _log("connect hostname_set_failed error={}".format(exc))
            connected = bool(_safe_attr(radio, "ipv4_address"))
            _log(
                "connect phase=ready attempt={} elapsed_s={:.1f}".format(
                    attempt,
                    _monotonic() - started,
                )
            )
            _log_radio_state(radio, "connected")
            break
        except Exception as exc:
            _log(
                "connect phase=error attempt={} elapsed_s={:.1f} type={} "
                "error={}".format(
                    attempt,
                    _monotonic() - started,
                    type(exc).__name__,
                    exc,
                )
            )
            _log_radio_state(radio, "after_connect_error")
            scan_once(
                radio=radio,
                target_ssid=ssid,
                limit=scan_limit,
                label="after_error_{}".format(attempt),
            )
            _sleep(delay_s)

    if not connected:
        _log("probe phase=done connected=False")
        return False

    if resolve or tcp:
        _probe_targets(
            radio,
            getattr(mqtt_config, "broker", "") or "",
            getattr(mqtt_config, "broker_ip", "") or "",
            int(getattr(mqtt_config, "port", 1883) or 1883),
            resolve=resolve,
            tcp=tcp,
        )

    _log("probe phase=done connected=True")
    return True


def scan_once(*, radio=None, target_ssid="", limit=24, label=1):
    """Scan nearby Wi-Fi networks and print compact per-network diagnostics."""
    if radio is None:
        try:
            import wifi  # type: ignore

            radio = wifi.radio
        except Exception as exc:
            _log("scan label={} phase=error error={}".format(label, exc))
            return ()

    target = str(target_ssid or "")
    _log("scan label={} phase=start limit={}".format(label, int(limit or 0)))
    try:
        result = _scan_wifi_networks(
            radio,
            target_ssid=target,
            limit=limit,
            collect=True,
        )
    except Exception as exc:
        _log(
            "scan label={} phase=error count=0 type={} error={}".format(
                label,
                type(exc).__name__,
                exc,
            )
        )
        return ()

    networks = tuple(result.get("networks", ()) or ())
    for network in networks:
        _log(_network_summary(network))
    _log(
        "scan label={} phase=done count={} target={} found={} best_rssi={} "
        "channels={}".format(
            label,
            result.get("count", 0),
            target or "none",
            result.get("found_count", 0),
            result.get("best_rssi", None)
            if result.get("best_rssi", None) is not None
            else "none",
            _join_values(result.get("channels", ())) or "none",
        )
    )
    return tuple(networks)


def _scan_wifi_networks(radio, *, target_ssid="", limit=24, collect=True):
    start_scan = getattr(radio, "start_scanning_networks", None)
    if not callable(start_scan):
        raise RuntimeError("wifi_scan_unavailable")

    target = str(target_ssid or "")
    max_count = max(0, int(limit or 0))
    scanned = []
    count = 0
    found_count = 0
    best_rssi = None
    channels = []
    try:
        networks = start_scan()
        for network in networks:
            count += 1
            ssid = _text(_safe_attr(network, "ssid"))
            rssi = _safe_attr(network, "rssi")
            channel = _safe_attr(network, "channel")
            matches = bool(target) and ssid == target
            if matches:
                found_count += 1
                if best_rssi is None or _rssi_value(rssi) > _rssi_value(best_rssi):
                    best_rssi = rssi
                if channel not in channels:
                    channels.append(channel)
            elif not target:
                if best_rssi is None or _rssi_value(rssi) > _rssi_value(best_rssi):
                    best_rssi = rssi
                if channel not in channels:
                    channels.append(channel)
            if collect:
                scanned.append(
                    {
                        "index": count,
                        "ssid": ssid,
                        "rssi": rssi,
                        "channel": channel,
                        "bssid": _safe_attr(network, "bssid"),
                        "authmode": _safe_attr(network, "authmode"),
                    }
                )
            if max_count and count >= max_count:
                break
    finally:
        _call(radio, "stop_scanning_networks")

    return {
        "networks": tuple(scanned),
        "count": count,
        "found_count": found_count,
        "best_rssi": best_rssi,
        "channels": tuple(channels),
    }


def _load_runtime_config():
    try:
        from cpynodus_ii.core.settings import Settings
    except Exception as exc:
        return None, ("settings_import_failed:{}".format(exc),)
    try:
        settings = Settings.from_working_directory()
        return settings.runtime_config(), ()
    except Exception as exc:
        return None, ("settings_load_failed:{}".format(exc),)


def _probe_targets(radio, broker, broker_ip, port, *, resolve=True, tcp=True):
    targets = []
    broker = str(broker or "").strip()
    broker_ip = str(broker_ip or "").strip()
    if broker:
        targets.append(broker)
    if broker_ip and broker_ip not in targets:
        targets.append(broker_ip)
    if not targets:
        _log("broker phase=skipped reason=no_targets")
        return
    try:
        import socketpool  # type: ignore

        pool = socketpool.SocketPool(radio)
    except Exception as exc:
        _log("broker phase=error errors=socketpool_failed:{}".format(exc))
        return

    for target in targets:
        if resolve:
            _resolve_target(pool, target, port)
        if tcp:
            _tcp_connect_target(pool, target, port)


def _resolve_target(pool, target, port):
    if _looks_ip_literal(target):
        _log("resolve target={} phase=literal ip={}".format(target, target))
        return target
    started = _monotonic()
    try:
        result = pool.getaddrinfo(target, port)
        ip_address = _ip_from_getaddrinfo(result)
        _log(
            "resolve target={} phase=ok ip={} elapsed_s={:.1f}".format(
                target,
                ip_address or "unknown",
                _monotonic() - started,
            )
        )
        return ip_address
    except Exception as exc:
        _log(
            "resolve target={} phase=error elapsed_s={:.1f} type={} error={}".format(
                target,
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
    return ""


def _tcp_connect_target(pool, target, port):
    started = _monotonic()
    sock = None
    try:
        sock = pool.socket()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(3)
        sock.connect((target, int(port or 1883)))
        _log(
            "tcp target={} port={} phase=ok elapsed_s={:.1f}".format(
                target,
                int(port or 1883),
                _monotonic() - started,
            )
        )
        return True
    except Exception as exc:
        _log(
            "tcp target={} port={} phase=error elapsed_s={:.1f} type={} "
            "error={}".format(
                target,
                int(port or 1883),
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        if sock is not None:
            _call(sock, "close")
    return False


def _network_summary(network):
    ssid = _text(network.get("ssid", ""))
    return (
        "network index={} ssid={} rssi={} channel={} bssid={} auth={}"
    ).format(
        network.get("index", 0),
        ssid or "hidden",
        network.get("rssi", "") or "unknown",
        network.get("channel", "") or "unknown",
        _bssid_text(network.get("bssid", "")),
        _auth_text(network.get("authmode", "")),
    )


def _log_radio_state(radio, label):
    ap_info = _safe_attr(radio, "ap_info")
    _log(
        "radio label={} connected={} station_ssid={} ip={} ap_ip={} "
        "hostname={}".format(
            label,
            _safe_attr(radio, "connected"),
            _safe_attr(ap_info, "ssid") or "none",
            _safe_attr(radio, "ipv4_address") or "none",
            _safe_attr(radio, "ipv4_address_ap") or "none",
            _safe_attr(radio, "hostname") or "none",
        )
    )


def _reset_station(radio):
    _call(radio, "disconnect")
    if _call(radio, "stop_station"):
        _call(radio, "start_station")


def _safe_attr(target, name):
    try:
        return getattr(target, name, "")
    except Exception:
        return ""


def _call(target, name):
    method = getattr(target, name, None)
    if not callable(method):
        return False
    try:
        method()
        return True
    except Exception as exc:
        _log("call name={} phase=error error={}".format(name, exc))
    return False


def _text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return str(value)
    return str(value or "")


def _bssid_text(value):
    if isinstance(value, (bytes, bytearray)):
        return ":".join("{:02x}".format(byte) for byte in value)
    if value:
        return str(value)
    return "unknown"


def _auth_text(value):
    if isinstance(value, (tuple, list)):
        return ",".join(str(item).split(".")[-1] for item in value) or "unknown"
    if value:
        return str(value).split(".")[-1]
    return "unknown"


def _rssi_value(value):
    try:
        return int(value)
    except Exception:
        return -999


def _ip_from_getaddrinfo(result):
    try:
        first = result[0]
        sockaddr = first[-1]
        return str(sockaddr[0] or "").strip()
    except Exception:
        return ""


def _looks_ip_literal(value):
    text = str(value or "").strip()
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except Exception:
            return False
    return ":" in text


def _join_values(values):
    return ",".join(str(value) for value in tuple(values or ()) if value != "")


def _join_errors(errors):
    return ",".join(str(error) for error in tuple(errors or ())) or "none"


def _monotonic():
    try:
        return float(time.monotonic())
    except Exception:
        return 0.0


def _sleep(seconds):
    try:
        time.sleep(float(seconds or 0.0))
    except Exception:
        pass


def _log(message):
    print("wifi_probe {}".format(message))
