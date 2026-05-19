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
    scans=0,
    attempts=3,
    cycles=0,
    scan_limit=24,
    delay_s=2.0,
    reset_settle_s=1.0,
    radio_cycle_every=2,
    radio_cycle_settle_s=3.0,
    connect=True,
    resolve=True,
    tcp=True,
    reset_station=True,
    use_bssid=False,
):
    """Connect with settings or supplied credentials, then test MQTT reachability.

    By default cycles=0 keeps trying until the station gets an IP.
    """
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
    station_hint = None
    for index in range(max(0, int(scans or 0))):
        networks = scan_once(
            radio=radio,
            target_ssid=ssid,
            limit=scan_limit,
            label=index + 1,
        )
        station_hint = _best_network_hint(station_hint, networks, ssid)
        _sleep(delay_s)

    if not connect:
        _log("probe phase=done connected=False")
        return True

    connected = False
    cycle_limit = max(0, int(cycles or 0))
    attempt_limit = max(1, int(attempts or 1))
    cycle = 0
    while not connected:
        cycle += 1
        if cycle > 1:
            _log("connect cycle={} phase=start".format(cycle))
        if reset_station:
            _reset_station(radio)
            _log_radio_state(radio, "after_reset")
            _sleep(reset_settle_s)
        for attempt in range(1, attempt_limit + 1):
            strategy = _connect_strategy(attempt, station_hint, use_bssid)
            if strategy:
                _log(
                    (
                        "connect hint cycle={} attempt={} strategy={} "
                        "channel={} bssid={}"
                    ).format(
                        cycle,
                        attempt,
                        strategy,
                        _hint_channel(station_hint) or "unknown",
                        _bssid_text(_hint_bssid(station_hint)),
                    )
                )
            _log(
                "connect cycle={} attempt={} ssid={}".format(
                    cycle,
                    attempt,
                    ssid,
                )
            )
            started = _monotonic()
            try:
                _connect_with_hint(radio, ssid, password, station_hint, strategy)
                if hostname:
                    try:
                        radio.hostname = hostname
                    except Exception as exc:
                        _log("connect hostname_set_failed error={}".format(exc))
                connected = bool(_safe_attr(radio, "ipv4_address"))
                _log(
                    (
                        "connect phase=ready cycle={} attempt={} "
                        "elapsed_s={:.1f}"
                    ).format(
                        cycle,
                        attempt,
                        _monotonic() - started,
                    )
                )
                _log_radio_state(radio, "connected")
                break
            except Exception as exc:
                _log(
                    (
                        "connect phase=error cycle={} attempt={} "
                        "elapsed_s={:.1f} type={} error={}"
                    ).format(
                        cycle,
                        attempt,
                        _monotonic() - started,
                        type(exc).__name__,
                        exc,
                    )
                )
                _log_radio_state(radio, "after_connect_error")
                networks = scan_once(
                    radio=radio,
                    target_ssid=ssid,
                    limit=scan_limit,
                    label="after_error_{}_{}".format(cycle, attempt),
                )
                station_hint = _best_network_hint(station_hint, networks, ssid)
                if attempt < attempt_limit:
                    if reset_station:
                        _reset_station(radio)
                        _log_radio_state(radio, "after_reset")
                        _sleep(reset_settle_s)
                    else:
                        _sleep(delay_s)
        if connected:
            break
        if cycle_limit and cycle >= cycle_limit:
            break
        if int(radio_cycle_every or 0) > 0 and cycle % int(radio_cycle_every) == 0:
            _log("radio_cycle cycle={} phase=start".format(cycle))
            _cycle_radio(radio)
            _log_radio_state(radio, "after_radio_cycle")
            _sleep(radio_cycle_settle_s)
        else:
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


def _connect_with_hint(radio, ssid, password, station_hint, strategy):
    channel = _hint_channel(station_hint)
    bssid = _hint_bssid(station_hint)
    if strategy == "bssid" and channel and bssid:
        try:
            return radio.connect(ssid, password, channel=int(channel), bssid=bssid)
        except TypeError:
            pass
    if strategy == "channel" and channel:
        try:
            return radio.connect(ssid, password, channel=int(channel))
        except TypeError:
            pass
    return radio.connect(ssid, password)


def _connect_strategy(attempt, station_hint, use_bssid):
    if station_hint is None:
        return ""
    try:
        attempt_number = int(attempt or 0)
    except Exception:
        attempt_number = 0
    if use_bssid and attempt_number > 0 and attempt_number % 3 == 0:
        return "bssid"
    if attempt_number > 0 and attempt_number % 2 == 0:
        return "channel"
    return ""


def _best_network_hint(current_hint, networks, target_ssid):
    best = current_hint
    target = str(target_ssid or "")
    for network in tuple(networks or ()):
        if _text(network.get("ssid", "")) != target:
            continue
        candidate = (
            network.get("channel", None),
            network.get("bssid", None),
            _rssi_value(network.get("rssi", None)),
        )
        if best is None or _hint_rssi(candidate) > _hint_rssi(best):
            best = candidate
    return best


def _hint_channel(station_hint):
    try:
        return station_hint[0]
    except Exception:
        return None


def _hint_bssid(station_hint):
    try:
        return station_hint[1]
    except Exception:
        return None


def _hint_rssi(station_hint):
    try:
        return int(station_hint[2])
    except Exception:
        return -999


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
    targets = _broker_probe_targets(broker, broker_ip)
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


def _broker_probe_targets(broker, broker_ip):
    broker = str(broker or "").strip()
    broker_ip = str(broker_ip or "").strip()
    targets = []
    if broker_ip:
        targets.append(broker_ip)
    if broker and broker not in targets:
        if _looks_ip_literal(broker) or not broker_ip:
            targets.append(broker)
        else:
            _log(
                "broker host={} phase=skipped reason=broker_ip_primary".format(
                    broker,
                )
            )
    return tuple(targets)


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
    _call(radio, "stop_ap")
    _call(radio, "disconnect")
    if _call(radio, "stop_station"):
        _call(radio, "start_station")


def _cycle_radio(radio):
    _call(radio, "disconnect")
    _call(radio, "stop_ap")
    _call(radio, "stop_station")
    changed = False
    try:
        radio.enabled = False
        changed = True
    except Exception as exc:
        _log("radio_cycle enabled_false phase=error error={}".format(exc))
    if changed:
        _sleep(0.5)
    try:
        radio.enabled = True
        changed = True
    except Exception as exc:
        _log("radio_cycle enabled_true phase=error error={}".format(exc))
    _call(radio, "start_station")
    return changed


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
