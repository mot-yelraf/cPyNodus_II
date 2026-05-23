"""Standalone CircuitPython platform probe for Wi-Fi, NTP, and MQTT round trip.

Run from the CircuitPython REPL while the normal app is stopped:

    from testApparatus import platform_test
    platform_test.run()

Or copy this file to the CIRCUITPY root and run:

    import platform_test
    platform_test.run()

Warm-start MQTT diagnostics should avoid resetting station state or touching NTP:

    import gc; gc.collect(); import platform_test
    platform_test.run(
        mqtt_mode="warm_diagnostics",
        scans=0,
        reset_station=False,
        sync_ntp=False,
        mqtt_rounds=3,
    )

To compare startup parameters from the current state without a pre-cleanup
connect, run the warm-start matrix:

    import gc; gc.collect(); import platform_test
    platform_test.run(mqtt_mode="warm_start_matrix")
"""

import time

PLATFORM_TEST_VERSION = "v0.26.141.2"


def run(
    *,
    settings_root=".",
    ssid=None,
    password=None,
    scans=1,
    scan_limit=24,
    connect_attempts=3,
    mqtt_rounds=3,
    mqtt_mode="direct",
    round_timeout_s=8.0,
    poll_interval_s=0.25,
    reset_station=True,
    sync_ntp=True,
):
    """Run one platform smoke test and print detailed serial diagnostics."""
    started = _monotonic()
    _log("platform phase=start version={}".format(PLATFORM_TEST_VERSION))

    runtime_config, errors = _load_runtime_config(settings_root)
    if runtime_config is None:
        _log("settings phase=error errors={}".format(_join_errors(errors)))
        return False

    network = runtime_config.network
    mqtt = runtime_config.mqtt
    time_config = runtime_config.time
    ssid = str(ssid if ssid is not None else network.ssid or "").strip()
    password = str(password if password is not None else network.password or "")
    hostname = str(network.hostname or "").strip()
    broker = str(mqtt.broker or "").strip()
    broker_ip = str(mqtt.broker_ip or "").strip()
    broker_target = broker_ip or broker
    port = int(mqtt.port or 1883)
    base_topic = str(mqtt.base_topic or "nodus").strip() or "nodus"
    device_id = hostname or "platform-test"
    ntp_server = str(time_config.ntp_server or "").strip() or "us.pool.ntp.org"
    ntp_ip = str(time_config.ntp_server_ip or "").strip()

    _log(
        (
            "settings ssid={} password_present={} hostname={} broker={} "
            "broker_ip={} port={} base_topic={} ntp_server={} ntp_ip={}"
        ).format(
            ssid or "none",
            1 if password else 0,
            hostname or "none",
            broker or "none",
            broker_ip or "none",
            port,
            base_topic,
            ntp_server or "none",
            ntp_ip or "none",
        )
    )
    if not ssid:
        _log("settings phase=error errors=ssid_missing")
        return False
    if not broker_target:
        _log("settings phase=error errors=mqtt_broker_target_missing")
        return False

    radio = _get_wifi_radio()
    if radio is None:
        _log("wifi phase=error errors=wifi_radio_unavailable")
        return False

    if hostname:
        _set_hostname(radio, hostname)
    _log_radio_state(radio, "initial")

    mode = str(mqtt_mode or "direct").strip().lower()
    if mode in ("warm_start_matrix", "warm_trigger_matrix"):
        matrix_ok = _mqtt_warm_start_matrix(
            radio,
            runtime_config,
            ssid,
            password,
            broker,
            broker_ip,
            broker_target,
            port,
            device_id,
            time_config,
            ntp_server,
            ntp_ip,
            scan_limit,
            connect_attempts,
        )
        result = "pass" if matrix_ok else "fail"
        _log(
            "platform phase=done result={} elapsed_s={:.1f}".format(
                result,
                _monotonic() - started,
            )
        )
        return matrix_ok

    hint = None
    scan_count = max(0, int(scans or 0))
    for index in range(scan_count):
        networks = _scan(radio, ssid, scan_limit, "preconnect_{}".format(index + 1))
        hint = _best_hint(hint, networks, ssid)
    if scan_count <= 0:
        _log("scan phase=skipped reason=scans_disabled")
    if scan_count > 0 and hint is None:
        _log("scan target={} phase=error errors=target_ssid_not_found".format(ssid))
        return False

    if reset_station:
        _reset_station(radio)
        _sleep(0.5)
        _log_radio_state(radio, "after_reset")

    if not reset_station and _radio_has_ip(radio):
        _log("wifi connect phase=skipped reason=already_connected")
        _log_radio_state(radio, "connected_existing")
    elif not _connect_wifi(radio, ssid, password, hint, connect_attempts):
        _log("platform phase=done result=fail step=wifi elapsed_s={:.1f}".format(
            _monotonic() - started
        ))
        return False

    pool = _build_socket_pool(radio)
    if pool is None:
        _log("socketpool phase=error errors=socketpool_unavailable")
        return False

    _resolve(pool, broker, port, label="broker_hostname")
    if broker_ip:
        _resolve(pool, broker_ip, port, label="broker_ip")
    _tcp_probe(pool, broker_target, port)

    if not sync_ntp:
        _log("ntp phase=skipped reason=disabled")
    elif not _sync_ntp(pool, time_config, ntp_server, ntp_ip):
        _log("ntp phase=warning result=unsynced")

    mqtt_ok = _mqtt_platform_round_trip(
        pool,
        runtime_config,
        broker_target,
        port,
        base_topic,
        device_id,
        mqtt_mode=mode,
        rounds=max(1, int(mqtt_rounds or 1)),
        timeout_s=float(round_timeout_s or 8.0),
        poll_interval_s=float(poll_interval_s or 0.25),
        radio=radio,
    )
    result = "pass" if mqtt_ok else "fail"
    _log(
        "platform phase=done result={} elapsed_s={:.1f}".format(
            result,
            _monotonic() - started,
        )
    )
    return mqtt_ok


def _load_runtime_config(settings_root):
    try:
        from cpynodus_ii.core.settings import Settings
    except Exception as exc:
        return None, ("settings_import_failed:{}".format(exc),)
    try:
        settings = Settings.from_directory(settings_root)
        return settings.runtime_config(), ()
    except Exception as exc:
        return None, ("settings_load_failed:{}".format(exc),)


def _collect():
    try:
        import gc

        gc.collect()
    except Exception:
        pass


def _log_memory(label):
    try:
        import gc

        free = gc.mem_free()
        allocated = gc.mem_alloc()
    except Exception:
        free = "unknown"
        allocated = "unknown"
    _log("memory label={} free_mem={} mem_alloc={}".format(label, free, allocated))


def _get_wifi_radio():
    try:
        import wifi  # type: ignore
    except Exception as exc:
        _log("wifi phase=error errors=wifi_import_failed:{}".format(exc))
        return None
    return getattr(wifi, "radio", None)


def _scan(radio, target_ssid, limit, label):
    _log("scan label={} phase=start target={}".format(label, target_ssid))
    networks = []
    found = 0
    best_rssi = None
    try:
        scan_iter = radio.start_scanning_networks()
        try:
            count = 0
            for network in scan_iter:
                count += 1
                ssid = _text(_safe_attr(network, "ssid"))
                rssi = _safe_attr(network, "rssi")
                channel = _safe_attr(network, "channel")
                bssid = _safe_attr(network, "bssid")
                authmode = _safe_attr(network, "authmode")
                if ssid == target_ssid:
                    found += 1
                    rssi_value = _rssi_value(rssi)
                    if best_rssi is None or rssi_value > _rssi_value(best_rssi):
                        best_rssi = rssi
                    networks.append((channel, bssid, rssi_value))
                _log(
                    "scan network index={} ssid={} rssi={} channel={} "
                    "bssid={} auth={}".format(
                        count,
                        ssid or "hidden",
                        rssi if rssi != "" else "unknown",
                        channel if channel != "" else "unknown",
                        _bssid_text(bssid),
                        _auth_text(authmode),
                    )
                )
                if int(limit or 0) > 0 and count >= int(limit or 0):
                    break
        finally:
            _call(radio, "stop_scanning_networks")
    except Exception as exc:
        _log("scan label={} phase=error type={} error={}".format(
            label,
            type(exc).__name__,
            exc,
        ))
        return ()
    _log(
        "scan label={} phase=done found={} best_rssi={}".format(
            label,
            found,
            best_rssi if best_rssi is not None else "none",
        )
    )
    return tuple(networks)


def _connect_wifi(radio, ssid, password, hint, attempts):
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        strategy = "plain"
        if attempt == 2 and _hint_channel(hint):
            strategy = "channel"
        elif attempt >= 3 and _hint_channel(hint) and _hint_bssid(hint):
            strategy = "channel_bssid"
        started = _monotonic()
        _log(
            "wifi connect phase=start attempt={} strategy={} ssid={}".format(
                attempt,
                strategy,
                ssid,
            )
        )
        try:
            _connect_with_strategy(radio, ssid, password, hint, strategy)
            connected = bool(_safe_attr(radio, "ipv4_address"))
            _log(
                "wifi connect phase=ready attempt={} elapsed_s={:.1f}".format(
                    attempt,
                    _monotonic() - started,
                )
            )
            _log_radio_state(radio, "connected")
            return connected
        except Exception as exc:
            _log(
                "wifi connect phase=error attempt={} elapsed_s={:.1f} "
                "type={} error={}".format(
                    attempt,
                    _monotonic() - started,
                    type(exc).__name__,
                    exc,
                )
            )
            _log_radio_state(radio, "after_connect_error")
            _reset_station(radio)
            _sleep(0.75)
    return False


def _connect_with_strategy(radio, ssid, password, hint, strategy):
    channel = _hint_channel(hint)
    bssid = _hint_bssid(hint)
    if strategy == "channel_bssid" and channel and bssid:
        try:
            return radio.connect(ssid, password, channel=int(channel), bssid=bssid)
        except TypeError:
            pass
    if strategy in ("channel", "channel_bssid") and channel:
        try:
            return radio.connect(ssid, password, channel=int(channel))
        except TypeError:
            pass
    return radio.connect(ssid, password)


def _build_socket_pool(radio):
    try:
        import socketpool  # type: ignore

        return socketpool.SocketPool(radio)
    except Exception as exc:
        _log("socketpool phase=error type={} error={}".format(type(exc).__name__, exc))
    return None


def _resolve(pool, target, port, *, label):
    target = str(target or "").strip()
    if not target:
        _log("resolve label={} phase=skipped reason=target_missing".format(label))
        return ""
    if _looks_ip_literal(target):
        _log("resolve label={} target={} phase=literal".format(label, target))
        return target
    started = _monotonic()
    try:
        result = pool.getaddrinfo(target, int(port or 1883))
        ip_address = _ip_from_getaddrinfo(result)
        _log(
            "resolve label={} target={} phase=ok ip={} elapsed_s={:.1f}".format(
                label,
                target,
                ip_address or "unknown",
                _monotonic() - started,
            )
        )
        return ip_address
    except Exception as exc:
        _log(
            "resolve label={} target={} phase=error elapsed_s={:.1f} "
            "type={} error={}".format(
                label,
                target,
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
    return ""


def _tcp_probe(pool, target, port, label="tcp"):
    started = _monotonic()
    sock = None
    prefix = "tcp" if label == "tcp" else "tcp label={}".format(label)
    _log("{} phase=start target={} port={}".format(
        prefix,
        target,
        int(port or 1883),
    ))
    try:
        sock = pool.socket()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(3)
        sock.connect((target, int(port or 1883)))
        _log("{} phase=ok elapsed_s={:.1f}".format(
            prefix,
            _monotonic() - started,
        ))
        return True
    except Exception as exc:
        _log(
            "{} phase=error elapsed_s={:.1f} type={} error={}".format(
                prefix,
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        if sock is not None:
            _call(sock, "close")
    return False


def _sync_ntp(pool, time_config, server, ntp_ip):
    targets = [str(server or "").strip()]
    if ntp_ip and ntp_ip not in targets:
        targets.append(ntp_ip)
    for target in targets:
        if not target:
            continue
        _resolve(pool, target, 123, label="ntp")
        started = _monotonic()
        _log("ntp phase=start server={}".format(target))
        try:
            current_datetime = _ntp_datetime(
                pool,
                target,
                int(getattr(time_config, "tz_offset", 0) or 0),
            )
            _set_rtc(current_datetime)
            _log(
                "ntp phase=synced server={} rtc={} elapsed_s={:.1f}".format(
                    target,
                    _datetime_text(current_datetime),
                    _monotonic() - started,
                )
            )
            return True
        except Exception as exc:
            _log(
                "ntp phase=error server={} elapsed_s={:.1f} type={} error={}".format(
                    target,
                    _monotonic() - started,
                    type(exc).__name__,
                    exc,
                )
            )
    return False


def _ntp_datetime(pool, server, tz_offset_seconds):
    import adafruit_ntp  # type: ignore

    tz_offset_hours = float(tz_offset_seconds or 0) / 3600.0
    try:
        client = adafruit_ntp.NTP(
            pool,
            server=server,
            tz_offset=tz_offset_hours,
            socket_timeout=2.0,
        )
    except TypeError:
        client = adafruit_ntp.NTP(
            pool,
            server=server,
            tz_offset=tz_offset_hours,
        )
    current_datetime = getattr(client, "datetime", None)
    if callable(current_datetime):
        current_datetime = current_datetime()
    if current_datetime is None:
        raise RuntimeError("ntp_datetime_unavailable")
    return current_datetime


def _set_rtc(current_datetime):
    import rtc  # type: ignore

    rtc.RTC().datetime = current_datetime


def _mqtt_platform_round_trip(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    mqtt_mode,
    rounds,
    timeout_s,
    poll_interval_s,
    radio=None,
):
    mode = mqtt_mode or "direct"
    if mode in ("warm_diagnostics", "warm_diag"):
        return _mqtt_warm_diagnostics(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            radio=radio,
        )
    if mode == "matrix":
        return _mqtt_mode_matrix(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
    if mode == "connect_matrix":
        return _mqtt_connect_matrix(
            pool,
            runtime_config,
            broker_target,
            port,
            device_id,
        )
    if mode == "both":
        direct_ok = _mqtt_round_trip_direct(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=False,
        )
        if not direct_ok:
            return False
        return _mqtt_round_trip_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=False,
        )
    if mode == "retained":
        return _mqtt_retained_probe_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            wrap_before_connect=False,
        )
    if mode == "retained_wrapped":
        return _mqtt_retained_probe_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            wrap_before_connect=True,
        )
    if mode == "direct_retained":
        return _mqtt_retained_probe_direct(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            wrap_before_connect=False,
        )
    if mode == "direct_retained_wrapped":
        return _mqtt_retained_probe_direct(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            wrap_before_connect=True,
        )
    if mode == "raw_mqtt":
        return _mqtt_raw_connect_probe(pool, broker_target, port, device_id)
    if mode == "firmware":
        return _mqtt_round_trip_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=False,
        )
    if mode == "firmware_direct_loop":
        return _mqtt_round_trip_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=False,
            direct_loop=True,
        )
    if mode == "firmware_wrapped":
        return _mqtt_round_trip_firmware(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=True,
        )
    if mode == "direct_wrapped":
        return _mqtt_round_trip_direct(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=True,
        )
    if mode == "direct_plain":
        return _mqtt_round_trip_direct(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            rounds=rounds,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            wrap_before_connect=False,
            optional_kwargs=False,
        )
    return _mqtt_round_trip_direct(
        pool,
        runtime_config,
        broker_target,
        port,
        base_topic,
        device_id,
        rounds=rounds,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        wrap_before_connect=False,
    )


def _mqtt_mode_matrix(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    rounds,
    timeout_s,
    poll_interval_s,
):
    tests = (
        (
            "raw_mqtt",
            lambda: _mqtt_raw_connect_probe(pool, broker_target, port, device_id),
        ),
        (
            "direct_plain",
            lambda: _mqtt_round_trip_direct(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=1,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=False,
                optional_kwargs=False,
            ),
        ),
        (
            "direct",
            lambda: _mqtt_round_trip_direct(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=1,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=False,
            ),
        ),
        (
            "firmware",
            lambda: _mqtt_round_trip_firmware(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=1,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=False,
            ),
        ),
        (
            "firmware_direct_loop",
            lambda: _mqtt_round_trip_firmware(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=1,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=False,
                direct_loop=True,
            ),
        ),
        (
            "direct_wrapped",
            lambda: _mqtt_round_trip_direct(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=1,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=True,
            ),
        ),
        (
            "firmware_wrapped",
            lambda: _mqtt_round_trip_firmware(
                pool,
                runtime_config,
                broker_target,
                port,
                base_topic,
                device_id,
                rounds=max(1, int(rounds or 1)),
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                wrap_before_connect=True,
            ),
        ),
    )
    passed = 0
    failed = 0
    for name, test in tests:
        _log("mqtt matrix mode={} phase=start".format(name))
        try:
            ok = bool(test())
        except Exception as exc:
            ok = False
            _log(
                "mqtt matrix mode={} phase=error type={} error={}".format(
                    name,
                    type(exc).__name__,
                    exc,
                )
            )
        if ok:
            passed += 1
        else:
            failed += 1
        _log("mqtt matrix mode={} phase=done result={}".format(
            name,
            "pass" if ok else "fail",
        ))
        _sleep(0.5)
    _log("mqtt matrix phase=summary passed={} failed={}".format(passed, failed))
    return failed == 0


def _mqtt_connect_matrix(pool, runtime_config, broker_target, port, device_id):
    tests = (
        (
            "raw_mqtt",
            lambda: _mqtt_raw_connect_probe(pool, broker_target, port, device_id),
        ),
        (
            "direct_plain_connect",
            lambda: _mqtt_connect_direct_probe(
                pool,
                runtime_config,
                broker_target,
                port,
                wrap_before_connect=False,
                optional_kwargs=False,
            ),
        ),
        (
            "direct_connect",
            lambda: _mqtt_connect_direct_probe(
                pool,
                runtime_config,
                broker_target,
                port,
                wrap_before_connect=False,
                optional_kwargs=True,
            ),
        ),
        (
            "direct_wrapped_connect",
            lambda: _mqtt_connect_direct_probe(
                pool,
                runtime_config,
                broker_target,
                port,
                wrap_before_connect=True,
                optional_kwargs=True,
            ),
        ),
        (
            "firmware_connect",
            lambda: _mqtt_connect_firmware_probe(
                pool,
                runtime_config,
                broker_target,
                port,
                wrap_before_connect=False,
            ),
        ),
        (
            "firmware_wrapped_connect",
            lambda: _mqtt_connect_firmware_probe(
                pool,
                runtime_config,
                broker_target,
                port,
                wrap_before_connect=True,
            ),
        ),
    )
    passed = 0
    failed = 0
    _log_memory("connect_matrix_start")
    for name, test in tests:
        _collect()
        _log_memory("{}_before".format(name))
        _log("mqtt connect_matrix mode={} phase=start".format(name))
        try:
            ok = bool(test())
        except Exception as exc:
            ok = False
            _log(
                "mqtt connect_matrix mode={} phase=error type={} error={}".format(
                    name,
                    type(exc).__name__,
                    exc,
                )
            )
        if ok:
            passed += 1
        else:
            failed += 1
        _collect()
        _log_memory("{}_after".format(name))
        _log("mqtt connect_matrix mode={} phase=done result={}".format(
            name,
            "pass" if ok else "fail",
        ))
        _sleep(0.5)
    _log("mqtt connect_matrix phase=summary passed={} failed={}".format(
        passed,
        failed,
    ))
    return failed == 0


def _mqtt_warm_start_matrix(
    radio,
    runtime_config,
    ssid,
    password,
    broker,
    broker_ip,
    broker_target,
    port,
    device_id,
    time_config,
    ntp_server,
    ntp_ip,
    scan_limit,
    connect_attempts,
):
    """Compare app-like and platform-like warm-start MQTT setup paths."""
    scenarios = (
        ("app_like", 1, 1, 0),
        ("app_like_ntp_first", 1, 1, 1),
        ("reset_no_scan", 0, 1, 0),
        ("platform_plain", 0, 0, 0),
    )
    passed = 0
    failed = 0
    _log(
        "warm_start_matrix phase=start broker={} port={} scenarios={}".format(
            broker_target,
            port,
            len(scenarios),
        )
    )
    for name, scan_count, reset_station, ntp_first in scenarios:
        ok = _mqtt_warm_start_scenario(
            radio,
            runtime_config,
            ssid,
            password,
            broker,
            broker_ip,
            broker_target,
            port,
            device_id,
            time_config,
            ntp_server,
            ntp_ip,
            scan_limit,
            connect_attempts,
            name,
            scan_count,
            reset_station,
            ntp_first,
        )
        if ok:
            passed += 1
        else:
            failed += 1
        _sleep(0.5)
    _log("warm_start_matrix phase=summary passed={} failed={}".format(
        passed,
        failed,
    ))
    return failed == 0


def _mqtt_warm_start_scenario(
    radio,
    runtime_config,
    ssid,
    password,
    broker,
    broker_ip,
    broker_target,
    port,
    device_id,
    time_config,
    ntp_server,
    ntp_ip,
    scan_limit,
    connect_attempts,
    name,
    scan_count,
    reset_station,
    ntp_first,
):
    _collect()
    _log_memory("{}_before".format(name))
    _log_radio_state(radio, "{}_before".format(name))
    _log(
        (
            "warm_start_matrix scenario={} phase=start scan={} "
            "reset_station={} ntp_first={}"
        ).format(
            name,
            scan_count,
            reset_station,
            ntp_first,
        )
    )

    hint = None
    if scan_count:
        networks = _scan(radio, ssid, scan_limit, "{}_preconnect".format(name))
        hint = _best_hint(None, networks, ssid)
        if hint is None:
            _log(
                "warm_start_matrix scenario={} phase=error "
                "errors=target_ssid_not_found".format(name)
            )
            return False
    else:
        _log("warm_start_matrix scenario={} scan phase=skipped".format(name))

    if reset_station:
        _reset_station(radio)
        _sleep(0.5)
        _log_radio_state(radio, "{}_after_reset".format(name))

    if not reset_station and _radio_has_ip(radio):
        _log(
            "warm_start_matrix scenario={} wifi phase=skipped "
            "reason=already_connected".format(name)
        )
    elif not _connect_wifi(radio, ssid, password, hint, connect_attempts):
        _log("warm_start_matrix scenario={} phase=fail step=wifi".format(name))
        return False

    pool = _build_socket_pool(radio)
    if pool is None:
        _log(
            "warm_start_matrix scenario={} phase=fail "
            "step=socketpool".format(name)
        )
        return False

    ok = True
    _resolve(pool, broker, port, label="{}_broker_hostname".format(name))
    if broker_ip:
        _resolve(pool, broker_ip, port, label="{}_broker_ip".format(name))
    if ntp_first:
        if not _sync_ntp(pool, time_config, ntp_server, ntp_ip):
            ok = False
            _log("warm_start_matrix scenario={} ntp phase=warning".format(name))
    else:
        _log("warm_start_matrix scenario={} ntp phase=skipped".format(name))

    if not _tcp_probe(
        pool,
        broker_target,
        port,
        label="{}_tcp".format(name),
    ):
        ok = False
    if not _mqtt_raw_connect_probe(
        pool,
        broker_target,
        port,
        device_id,
        label="{}_raw_mqtt".format(name),
    ):
        ok = False
    if not _mqtt_warm_firmware_connect_probe(
        pool,
        runtime_config,
        broker_target,
        port,
        cycle=name,
    ):
        ok = False
    if not _mqtt_connect_direct_probe(
        pool,
        runtime_config,
        broker_target,
        port,
        wrap_before_connect=False,
        optional_kwargs=False,
    ):
        ok = False

    _collect()
    _log_memory("{}_after".format(name))
    _log_radio_state(radio, "{}_after".format(name))
    _log(
        "warm_start_matrix scenario={} phase=done result={}".format(
            name,
            "pass" if ok else "fail",
        )
    )
    return ok


def _mqtt_warm_diagnostics(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    rounds,
    timeout_s,
    poll_interval_s,
    radio=None,
):
    """Run connect-only probes while preserving current warm-start state."""
    repeat_count = max(1, int(rounds or 1))
    passed = 0
    failed = 0
    _log(
        (
            "mqtt warm_diag phase=start broker={} port={} rounds={} "
            "round_timeout_s={} note=use_scans0_reset0_ntp0_for_warm_state"
        ).format(
            broker_target,
            port,
            repeat_count,
            timeout_s,
        )
    )
    for index in range(1, repeat_count + 1):
        cycle_ok = True
        _collect()
        _log_memory("warm_diag_{}_before".format(index))
        if radio is not None:
            _log_radio_state(radio, "warm_diag_{}_before".format(index))
        _log("mqtt warm_diag cycle={} phase=start".format(index))

        if not _tcp_probe(
            pool,
            broker_target,
            port,
            label="warm_diag_{}_tcp".format(index),
        ):
            cycle_ok = False
        if not _mqtt_raw_connect_probe(
            pool,
            broker_target,
            port,
            device_id,
            label="warm_diag_{}_raw_mqtt".format(index),
        ):
            cycle_ok = False
        if not _mqtt_warm_firmware_connect_probe(
            pool,
            runtime_config,
            broker_target,
            port,
            cycle=index,
        ):
            cycle_ok = False
        if not _mqtt_connect_direct_probe(
            pool,
            runtime_config,
            broker_target,
            port,
            wrap_before_connect=False,
            optional_kwargs=False,
        ):
            cycle_ok = False
        if not _mqtt_connect_direct_probe(
            pool,
            runtime_config,
            broker_target,
            port,
            wrap_before_connect=False,
            optional_kwargs=True,
        ):
            cycle_ok = False
        if not _mqtt_warm_firmware_publish_probe(
            pool,
            runtime_config,
            broker_target,
            port,
            base_topic,
            device_id,
            cycle=index,
        ):
            cycle_ok = False

        _collect()
        _log_memory("warm_diag_{}_after".format(index))
        if radio is not None:
            _log_radio_state(radio, "warm_diag_{}_after".format(index))
        if cycle_ok:
            passed += 1
        else:
            failed += 1
        _log(
            "mqtt warm_diag cycle={} phase=done result={}".format(
                index,
                "pass" if cycle_ok else "fail",
            )
        )
        _sleep(poll_interval_s)
    _log("mqtt warm_diag phase=summary passed={} failed={}".format(
        passed,
        failed,
    ))
    return failed == 0


def _mqtt_warm_firmware_publish_probe(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    cycle,
):
    """Publish one broker-visible platform-test message in warm diagnostics."""
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            close_mqtt_client,
            connect_mqtt_client,
            sync_transport_to_client,
        )
    except Exception as exc:
        _log(
            "mqtt warm_diag cycle={} publish phase=error "
            "error=import_failed:{}".format(
                cycle,
                exc,
            )
        )
        return False

    transport = MQTTTransport(broker_target, port)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=None,
        modules={"wrap_socket_pool_before_connect": False},
    )
    _log(
        (
            "mqtt warm_diag cycle={} publish_adapter phase={} broker={} "
            "targets={} wrapped=0 compat={} callback={} errors={}"
        ).format(
            cycle,
            adapter.phase,
            adapter.broker or "none",
            _join_values(adapter.broker_targets) or "none",
            1 if getattr(adapter, "socket_compat_enabled", False) else 0,
            "flex" if getattr(adapter, "flexible_callback_enabled", False) else "fixed",
            _join_errors(adapter.errors),
        )
    )
    if adapter.phase != "ready":
        return False

    topic = "{}/{}/platform_test/warm_diagnostics".format(
        base_topic,
        device_id,
    )
    payload = "platform_test:warm_diagnostics:{}:{}:{:.3f}".format(
        device_id,
        cycle,
        _monotonic(),
    )
    try:
        transport.mark_connect_requested()
        started = _monotonic()
        connect_result = connect_mqtt_client(adapter, transport, preflight=True)
        adapter = connect_result.adapter
        _log(
            (
                "mqtt warm_diag cycle={} publish_connect phase={} broker={} "
                "elapsed_s={:.1f} connected={} errors={}"
            ).format(
                cycle,
                connect_result.phase,
                adapter.active_broker or adapter.broker or "none",
                _monotonic() - started,
                1 if getattr(transport, "connected", False) else 0,
                _join_errors(connect_result.errors),
            )
        )
        if connect_result.phase != "connected":
            return False

        _log(
            (
                "mqtt warm_diag cycle={} publish phase=start topic={} "
                "payload={}"
            ).format(
                cycle,
                topic,
                payload,
            )
        )
        started = _monotonic()
        transport.publish(topic, payload, retain=False)
        sync_result = sync_transport_to_client(adapter, transport)
        adapter = sync_result.adapter
        _log(
            (
                "mqtt warm_diag cycle={} publish phase={} elapsed_s={:.1f} "
                "published={} errors={}"
            ).format(
                cycle,
                sync_result.phase,
                _monotonic() - started,
                sync_result.published_count,
                _join_errors(sync_result.errors),
            )
        )
        return sync_result.phase == "synced" and not sync_result.errors
    except Exception as exc:
        _log(
            "mqtt warm_diag cycle={} publish phase=exception type={} "
            "error={}".format(
                cycle,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        close_result = close_mqtt_client(adapter, transport)
        _log(
            "mqtt warm_diag cycle={} publish_disconnect phase={} errors={}".format(
                cycle,
                close_result.phase,
                _join_errors(close_result.errors),
            )
        )
    return False


def _mqtt_warm_firmware_connect_probe(
    pool,
    runtime_config,
    broker_target,
    port,
    *,
    cycle,
):
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            close_mqtt_client,
            connect_mqtt_client,
            preflight_mqtt_broker_connect,
            preflight_mqtt_broker_tcp,
        )
    except Exception as exc:
        _log(
            "mqtt warm_diag cycle={} firmware phase=error "
            "error=import_failed:{}".format(
                cycle,
                exc,
            )
        )
        return False

    transport = MQTTTransport(broker_target, port)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=None,
        modules={"wrap_socket_pool_before_connect": False},
    )
    _log(
        (
            "mqtt warm_diag cycle={} firmware_adapter phase={} broker={} "
            "targets={} wrapped=0 compat={} callback={} errors={}"
        ).format(
            cycle,
            adapter.phase,
            adapter.broker or "none",
            _join_values(adapter.broker_targets) or "none",
            1 if getattr(adapter, "socket_compat_enabled", False) else 0,
            "flex" if getattr(adapter, "flexible_callback_enabled", False) else "fixed",
            _join_errors(adapter.errors),
        )
    )
    if adapter.phase != "ready":
        return False

    ok = True
    _log_client_state(
        adapter.client,
        "mqtt warm_diag cycle={} firmware_client phase=built".format(cycle),
    )
    try:
        started = _monotonic()
        tcp_error, tcp_target = preflight_mqtt_broker_tcp(adapter)
        _log(
            (
                "mqtt warm_diag cycle={} firmware_preflight phase={} "
                "broker={} target={} port={} elapsed_s={:.1f} errors={}"
            ).format(
                cycle,
                "tcp_error" if tcp_error else "tcp_ok",
                adapter.active_broker or adapter.broker or "none",
                tcp_target or "none",
                adapter.port,
                _monotonic() - started,
                tcp_error or "none",
            )
        )
        if tcp_error:
            ok = False

        started = _monotonic()
        probe_error, probe_target, probe_client_id, connack = (
            preflight_mqtt_broker_connect(adapter)
        )
        _log(
            (
                "mqtt warm_diag cycle={} firmware_connect_probe phase={} "
                "broker={} target={} port={} elapsed_s={:.1f} client_id={} "
                "connack={} errors={}"
            ).format(
                cycle,
                "connack_error" if probe_error else "connack_ok",
                adapter.active_broker or adapter.broker or "none",
                probe_target or "none",
                adapter.port,
                _monotonic() - started,
                probe_client_id or "none",
                connack,
                probe_error or "none",
            )
        )
        if probe_error:
            ok = False

        transport.mark_connect_requested()
        started = _monotonic()
        connect_result = connect_mqtt_client(adapter, transport, preflight=True)
        adapter = connect_result.adapter
        _log(
            (
                "mqtt warm_diag cycle={} firmware_connect phase={} broker={} "
                "elapsed_s={:.1f} connected={} errors={}"
            ).format(
                cycle,
                connect_result.phase,
                adapter.active_broker or adapter.broker or "none",
                _monotonic() - started,
                1 if getattr(transport, "connected", False) else 0,
                _join_errors(connect_result.errors),
            )
        )
        _log_client_state(
            adapter.client,
            "mqtt warm_diag cycle={} firmware_client phase=after_connect".format(
                cycle
            ),
        )
        if connect_result.phase != "connected":
            ok = False
    except Exception as exc:
        ok = False
        _log(
            "mqtt warm_diag cycle={} firmware phase=exception type={} "
            "error={}".format(
                cycle,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        close_result = close_mqtt_client(adapter, transport)
        _log(
            "mqtt warm_diag cycle={} firmware_disconnect phase={} errors={}".format(
                cycle,
                close_result.phase,
                _join_errors(close_result.errors),
            )
        )
        _log_client_state(
            adapter.client,
            "mqtt warm_diag cycle={} firmware_client phase=closed".format(cycle),
        )
    return ok


def _mqtt_raw_connect_probe(pool, broker_target, port, device_id, label="raw_mqtt"):
    sock = None
    started = _monotonic()
    mode_name = label or "raw_mqtt"
    _log(
        "mqtt mode={} connect phase=start broker={} port={}".format(
            mode_name,
            broker_target,
            port,
        )
    )
    try:
        sock = pool.socket()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(3)
        sock.connect((broker_target, int(port or 1883)))
        _log(
            "mqtt mode={} tcp phase=connected elapsed_s={:.1f}".format(
                mode_name,
                _monotonic() - started
            )
        )
        packet = _mqtt_connect_packet(
            "platform-{}".format(device_id or "test")
        )
        send = getattr(sock, "send", None)
        if not callable(send):
            raise RuntimeError("socket_send_unavailable")
        sent = send(packet)
        _log(
            "mqtt mode={} connect_packet phase=sent bytes={} sent={}".format(
                mode_name,
                len(packet),
                sent,
            )
        )
        received = _socket_recv_exact(sock, 4)
        _log(
            "mqtt mode={} connack phase=received bytes={} hex={}".format(
                mode_name,
                len(received),
                _bytes_hex(received),
            )
        )
        if len(received) < 4:
            _log("mqtt mode={} connack phase=error reason=short_read".format(
                mode_name
            ))
            return False
        if received[0] == 0x20 and received[1] == 0x02 and received[3] == 0x00:
            _log(
                "mqtt mode={} connack phase=ok elapsed_s={:.1f}".format(
                    mode_name,
                    _monotonic() - started
                )
            )
            return True
        _log(
            "mqtt mode={} connack phase=error reason=unexpected hex={}".format(
                mode_name,
                _bytes_hex(received)
            )
        )
    except Exception as exc:
        _log(
            "mqtt mode={} phase=error elapsed_s={:.1f} type={} error={}".format(
                mode_name,
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        if sock is not None:
            _call(sock, "close")
    return False


def _mqtt_connect_packet(client_id):
    client = str(client_id or "platform-test")
    try:
        client_bytes = client.encode("utf-8")
    except Exception:
        client_bytes = bytes(client)
    variable_header = b"\x00\x04MQTT\x04\x02\x00\x3c"
    payload = _mqtt_utf8(client_bytes)
    remaining = len(variable_header) + len(payload)
    return b"\x10" + _mqtt_remaining_length(remaining) + variable_header + payload


def _mqtt_utf8(value):
    return bytes(((len(value) >> 8) & 0xFF, len(value) & 0xFF)) + value


def _mqtt_remaining_length(value):
    encoded = bytearray()
    remaining = int(value or 0)
    while True:
        digit = remaining % 128
        remaining = remaining // 128
        if remaining > 0:
            digit = digit | 0x80
        encoded.append(digit)
        if remaining <= 0:
            break
    return bytes(encoded)


def _socket_recv_exact(sock, count):
    chunks = bytearray()
    deadline = _monotonic() + 4.0
    while len(chunks) < int(count or 0) and _monotonic() <= deadline:
        wanted = int(count or 0) - len(chunks)
        recv = getattr(sock, "recv", None)
        recv_into = getattr(sock, "recv_into", None)
        if callable(recv):
            try:
                chunk = recv(wanted)
            except TypeError:
                chunk = _socket_recv_into(sock, wanted, recv_into)
        else:
            chunk = _socket_recv_into(sock, wanted, recv_into)
        if chunk:
            chunks.extend(chunk)
            continue
        _sleep(0.05)
    return bytes(chunks)


def _socket_recv_into(sock, count, recv_into=None):
    if recv_into is None:
        recv_into = getattr(sock, "recv_into", None)
    if callable(recv_into):
        buffer = bytearray(int(count or 0))
        try:
            read = recv_into(buffer)
        except TypeError:
            read = recv_into(buffer, int(count or 0))
        return bytes(buffer[: int(read or 0)])
    raise AttributeError("socket_recv_unavailable")


def _bytes_hex(value):
    try:
        return "".join("{:02x}".format(byte) for byte in value)
    except Exception:
        return "unavailable"


def _mqtt_round_trip_direct(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    rounds,
    timeout_s,
    poll_interval_s,
    wrap_before_connect,
    optional_kwargs=True,
):
    client = None
    mode_name = "direct_wrapped" if wrap_before_connect else "direct"
    if not optional_kwargs:
        mode_name = "direct_plain"
    topic = "{}/{}/platform_test/{}".format(base_topic, device_id, mode_name)
    received = []

    def on_message(*args):
        if len(args) < 2:
            _log("mqtt callback phase=error args={}".format(len(args)))
            return
        received_topic = str(args[-2] or "")
        payload = _payload_text(args[-1])
        received.append((received_topic, payload))
        _log("mqtt rx topic={} payload={}".format(received_topic, payload))

    try:
        client = _build_mqtt_client(
            pool,
            runtime_config,
            broker_target,
            port,
            wrap_before_connect=wrap_before_connect,
            optional_kwargs=optional_kwargs,
        )
        client.on_message = on_message
        _log(
            "mqtt mode={} connect phase=start broker={} port={} wrapped={} "
            "optional_kwargs={}".format(
                mode_name,
                broker_target,
                port,
                1 if wrap_before_connect else 0,
                1 if optional_kwargs else 0,
            )
        )
        started = _monotonic()
        client.connect()
        _log("mqtt mode={} connect phase=connected elapsed_s={:.1f}".format(
            mode_name,
            _monotonic() - started,
        ))
        _mqtt_set_runtime_timeout(client, 1)
        _log("mqtt subscribe phase=start topic={}".format(topic))
        started = _monotonic()
        client.subscribe(topic)
        _log("mqtt subscribe phase=ok elapsed_s={:.1f}".format(
            _monotonic() - started
        ))
        for index in range(1, int(rounds or 1) + 1):
            payload = "platform_test:direct:{}:{}:{:.3f}".format(
                device_id,
                index,
                _monotonic(),
            )
            if not _mqtt_one_round(
                client,
                topic,
                payload,
                received,
                timeout_s,
                poll_interval_s,
                index,
            ):
                return False
        return True
    except Exception as exc:
        _log(
            "mqtt mode={} phase=error type={} error={}".format(
                mode_name,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        if client is not None:
            try:
                client.disconnect()
                _log("mqtt disconnect phase=ok")
            except Exception as exc:
                _log("mqtt disconnect phase=error error={}".format(exc))
    return False


def _mqtt_connect_direct_probe(
    pool,
    runtime_config,
    broker_target,
    port,
    *,
    wrap_before_connect,
    optional_kwargs,
):
    client = None
    if wrap_before_connect:
        mode_name = "direct_wrapped_connect"
    elif optional_kwargs:
        mode_name = "direct_connect"
    else:
        mode_name = "direct_plain_connect"
    try:
        client = _build_mqtt_client(
            pool,
            runtime_config,
            broker_target,
            port,
            wrap_before_connect=wrap_before_connect,
            optional_kwargs=optional_kwargs,
        )
        _log_client_state(client, "mqtt mode={} client phase=built".format(mode_name))
        _log(
            "mqtt mode={} connect phase=start broker={} port={} wrapped={} "
            "optional_kwargs={}".format(
                mode_name,
                broker_target,
                port,
                1 if wrap_before_connect else 0,
                1 if optional_kwargs else 0,
            )
        )
        started = _monotonic()
        client.connect()
        _log(
            "mqtt mode={} connect phase=connected elapsed_s={:.1f}".format(
                mode_name,
                _monotonic() - started,
            )
        )
        _log_client_state(
            client,
            "mqtt mode={} client phase=connected".format(mode_name),
        )
        return True
    except Exception as exc:
        _log(
            "mqtt mode={} connect phase=error type={} error={}".format(
                mode_name,
                type(exc).__name__,
                exc,
            )
        )
        if client is not None:
            _log_client_state(
                client,
                "mqtt mode={} client phase=connect_error".format(mode_name),
            )
    finally:
        if client is not None:
            try:
                client.disconnect()
                _log("mqtt mode={} disconnect phase=ok".format(mode_name))
            except Exception as exc:
                _log(
                    "mqtt mode={} disconnect phase=error error={}".format(
                        mode_name,
                        exc,
                    )
                )
            _log_client_state(
                client,
                "mqtt mode={} client phase=closed".format(mode_name),
            )
    return False


def _mqtt_retained_probe_direct(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    wrap_before_connect,
):
    client = None
    mode_name = "direct_retained_wrapped" if wrap_before_connect else "direct_retained"
    topic, payload = _retained_probe_payload(
        runtime_config,
        broker_target,
        base_topic,
        device_id,
    )
    payload_text = _serialize_payload(payload)
    try:
        client = _build_mqtt_client(
            pool,
            runtime_config,
            broker_target,
            port,
            wrap_before_connect=wrap_before_connect,
        )
        _log(
            "mqtt mode={} connect phase=start broker={} port={} wrapped={}".format(
                mode_name,
                broker_target,
                port,
                1 if wrap_before_connect else 0,
            )
        )
        started = _monotonic()
        client.connect()
        _log(
            "mqtt mode={} connect phase=connected elapsed_s={:.1f}".format(
                mode_name,
                _monotonic() - started,
            )
        )
        _mqtt_set_runtime_timeout(client, 1)
        started = _monotonic()
        _log(
            "mqtt mode={} publish phase=start topic={} retain=1 "
            "qos=1 bytes={}".format(mode_name, topic, len(payload_text))
        )
        try:
            client.publish(topic, payload_text, qos=1, retain=True)
            qos = 1
        except TypeError as exc:
            if "qos" not in str(exc).lower():
                raise
            qos = 0
            _log("mqtt mode={} publish qos=unsupported fallback=retain".format(
                mode_name
            ))
            client.publish(topic, payload_text, retain=True)
        _log(
            "mqtt mode={} publish phase=ok qos={} elapsed_s={:.1f}".format(
                mode_name,
                qos,
                _monotonic() - started,
            )
        )
        return True
    except Exception as exc:
        _log(
            "mqtt mode={} phase=error type={} error={}".format(
                mode_name,
                type(exc).__name__,
                exc,
            )
        )
    finally:
        if client is not None:
            try:
                client.disconnect()
                _log("mqtt mode={} disconnect phase=ok".format(mode_name))
            except Exception as exc:
                _log(
                    "mqtt mode={} disconnect phase=error error={}".format(
                        mode_name,
                        exc,
                    )
                )
    return False


def _mqtt_retained_probe_firmware(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    wrap_before_connect,
):
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            close_mqtt_client,
            connect_mqtt_client,
            sync_transport_to_client,
        )
    except Exception as exc:
        _log("mqtt mode=retained phase=error error=import_failed:{}".format(exc))
        return False

    mode_name = "retained_wrapped" if wrap_before_connect else "retained"
    topic, payload = _retained_probe_payload(
        runtime_config,
        broker_target,
        base_topic,
        device_id,
    )
    payload_bytes = len(_serialize_payload(payload))
    transport = MQTTTransport(broker_target, port)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=None,
        modules={"wrap_socket_pool_before_connect": wrap_before_connect},
    )
    _log(
        (
            "mqtt mode={} adapter phase={} broker={} targets={} "
            "wrapped={} compat={} callback={} errors={}"
        ).format(
            mode_name,
            adapter.phase,
            adapter.broker or "none",
            _join_values(adapter.broker_targets) or "none",
            1 if wrap_before_connect else 0,
            1 if getattr(adapter, "socket_compat_enabled", False) else 0,
            "flex" if getattr(adapter, "flexible_callback_enabled", False) else "fixed",
            _join_errors(adapter.errors),
        )
    )
    if adapter.phase != "ready":
        return False
    try:
        transport.mark_connect_requested()
        started = _monotonic()
        connect_result = connect_mqtt_client(adapter, transport, preflight=True)
        adapter = connect_result.adapter
        _log(
            "mqtt mode={} connect phase={} broker={} elapsed_s={:.1f} "
            "errors={}".format(
                mode_name,
                connect_result.phase,
                adapter.active_broker or adapter.broker or "none",
                _monotonic() - started,
                _join_errors(connect_result.errors),
            )
        )
        if connect_result.phase != "connected":
            return False
        _log(
            "mqtt mode={} publish phase=start topic={} retain=1 qos=1 "
            "bytes={}".format(mode_name, topic, payload_bytes)
        )
        started = _monotonic()
        transport.publish(topic, payload, retain=True)
        sync_result = sync_transport_to_client(adapter, transport)
        adapter = sync_result.adapter
        _log(
            "mqtt mode={} publish phase={} elapsed_s={:.1f} "
            "published={} errors={}".format(
                mode_name,
                sync_result.phase,
                _monotonic() - started,
                sync_result.published_count,
                _join_errors(sync_result.errors),
            )
        )
        return sync_result.phase == "synced" and not sync_result.errors
    finally:
        close_result = close_mqtt_client(adapter, transport)
        _log(
            "mqtt mode={} disconnect phase={} errors={}".format(
                mode_name,
                close_result.phase,
                _join_errors(close_result.errors),
            )
        )


def _retained_probe_payload(runtime_config, broker_target, base_topic, device_id):
    try:
        from cpynodus_ii.features.payloads import build_runtime_meta_payload, mqtt_topic

        topic = mqtt_topic(runtime_config, device_id, "meta")
        try:
            payload = build_runtime_meta_payload(
                runtime_config,
                version=PLATFORM_TEST_VERSION,
                active_broker=broker_target,
                include_switch_channels=False,
            )
        except TypeError:
            payload = build_runtime_meta_payload(
                runtime_config,
                version=PLATFORM_TEST_VERSION,
                active_broker=broker_target,
            )
        payload["platform_probe"] = {
            "schema": "nodus-platform-retained-probe/v1",
            "mode": "retained_qos1",
            "base_topic": base_topic,
        }
        return topic, payload
    except Exception as exc:
        _log("mqtt retained_payload phase=error error={}".format(exc))
    topic = "{}/{}/meta".format(base_topic, device_id)
    filler = "x" * 1200
    return topic, {
        "schema": "nodus-meta/v1",
        "device_id": device_id,
        "type": "nodus",
        "version": PLATFORM_TEST_VERSION,
        "platform_probe": "retained_qos1",
        "filler": filler,
    }


def _serialize_payload(payload):
    if isinstance(payload, str):
        return payload
    try:
        import json

        try:
            return json.dumps(payload, separators=(",", ":"))
        except TypeError:
            return json.dumps(payload)
    except Exception:
        return str(payload)


def _mqtt_round_trip_firmware(
    pool,
    runtime_config,
    broker_target,
    port,
    base_topic,
    device_id,
    *,
    rounds,
    timeout_s,
    poll_interval_s,
    wrap_before_connect,
    direct_loop=False,
):
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            close_mqtt_client,
            connect_mqtt_client,
            sync_transport_to_client,
        )
    except Exception as exc:
        _log("mqtt mode=firmware phase=error error=import_failed:{}".format(exc))
        return False

    if wrap_before_connect:
        mode_name = "firmware_wrapped"
    elif direct_loop:
        mode_name = "firmware_direct_loop"
    else:
        mode_name = "firmware"
    transport = MQTTTransport(broker_target, port)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=None,
        modules={"wrap_socket_pool_before_connect": wrap_before_connect},
    )
    _log(
        (
            "mqtt mode={} adapter phase={} broker={} targets={} "
            "wrapped={} compat={} callback={} errors={}"
        ).format(
            mode_name,
            adapter.phase,
            adapter.broker or "none",
            _join_values(adapter.broker_targets) or "none",
            1 if wrap_before_connect else 0,
            1 if getattr(adapter, "socket_compat_enabled", False) else 0,
            "flex" if getattr(adapter, "flexible_callback_enabled", False) else "fixed",
            _join_errors(adapter.errors),
        )
    )
    if adapter.phase != "ready":
        return False
    try:
        transport.mark_connect_requested()
        started = _monotonic()
        connect_result = connect_mqtt_client(adapter, transport, preflight=True)
        adapter = connect_result.adapter
        _log(
            "mqtt mode={} connect phase={} broker={} elapsed_s={:.1f} "
            "errors={}".format(
                mode_name,
                connect_result.phase,
                adapter.active_broker or adapter.broker or "none",
                _monotonic() - started,
                _join_errors(connect_result.errors),
            )
        )
        if connect_result.phase != "connected":
            return False
        topic = "{}/{}/platform_test/firmware".format(base_topic, device_id)
        transport.subscribe(topic)
        sync_result = sync_transport_to_client(adapter, transport)
        adapter = sync_result.adapter
        _log(
            "mqtt mode={} subscribe phase={} topic={} subscribed={} "
            "errors={}".format(
                mode_name,
                sync_result.phase,
                topic,
                sync_result.subscribed_count,
                _join_errors(sync_result.errors),
            )
        )
        if sync_result.phase != "synced" or sync_result.errors:
            return False
        for index in range(1, int(rounds or 1) + 1):
            payload = "platform_test:firmware:{}:{}:{:.3f}".format(
                device_id,
                index,
                _monotonic(),
            )
            adapter = _mqtt_one_firmware_round(
                adapter,
                transport,
                topic,
                payload,
                timeout_s,
                poll_interval_s,
                index,
                mode_name,
                direct_loop,
            ) or adapter
            if not getattr(transport, "connected", False):
                return False
        return True
    finally:
        close_result = close_mqtt_client(adapter, transport)
        _log(
            "mqtt mode={} disconnect phase={} errors={}".format(
                mode_name,
                close_result.phase,
                _join_errors(close_result.errors),
            )
        )


def _mqtt_connect_firmware_probe(
    pool,
    runtime_config,
    broker_target,
    port,
    *,
    wrap_before_connect,
):
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            close_mqtt_client,
            connect_mqtt_client,
        )
    except Exception as exc:
        _log("mqtt mode=firmware_connect phase=error error=import_failed:{}".format(
            exc
        ))
        return False

    if wrap_before_connect:
        mode_name = "firmware_wrapped_connect"
    else:
        mode_name = "firmware_connect"
    transport = MQTTTransport(broker_target, port)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=None,
        modules={"wrap_socket_pool_before_connect": wrap_before_connect},
    )
    _log(
        (
            "mqtt mode={} adapter phase={} broker={} targets={} "
            "wrapped={} compat={} callback={} errors={}"
        ).format(
            mode_name,
            adapter.phase,
            adapter.broker or "none",
            _join_values(adapter.broker_targets) or "none",
            1 if wrap_before_connect else 0,
            1 if getattr(adapter, "socket_compat_enabled", False) else 0,
            "flex" if getattr(adapter, "flexible_callback_enabled", False) else "fixed",
            _join_errors(adapter.errors),
        )
    )
    if adapter.phase != "ready":
        return False
    _log_client_state(
        adapter.client,
        "mqtt mode={} client phase=built".format(mode_name),
    )
    try:
        transport.mark_connect_requested()
        started = _monotonic()
        connect_result = connect_mqtt_client(adapter, transport, preflight=True)
        adapter = connect_result.adapter
        _log(
            "mqtt mode={} connect phase={} broker={} elapsed_s={:.1f} "
            "connected={} errors={}".format(
                mode_name,
                connect_result.phase,
                adapter.active_broker or adapter.broker or "none",
                _monotonic() - started,
                1 if getattr(transport, "connected", False) else 0,
                _join_errors(connect_result.errors),
            )
        )
        _log_client_state(
            adapter.client,
            "mqtt mode={} client phase=after_connect".format(mode_name),
        )
        return connect_result.phase == "connected"
    finally:
        close_result = close_mqtt_client(adapter, transport)
        _log(
            "mqtt mode={} disconnect phase={} errors={}".format(
                mode_name,
                close_result.phase,
                _join_errors(close_result.errors),
            )
        )
        _log_client_state(
            adapter.client,
            "mqtt mode={} client phase=closed".format(mode_name),
        )


def _build_mqtt_client(
    pool,
    runtime_config,
    broker_target,
    port,
    *,
    wrap_before_connect,
    optional_kwargs=True,
):
    from adafruit_minimqtt.adafruit_minimqtt import MQTT  # type: ignore

    mqtt = runtime_config.mqtt
    client_pool = pool
    if wrap_before_connect:
        try:
            from cpynodus_ii.core.mqtt_client import _wrap_minimqtt_socket_pool

            client_pool = _wrap_minimqtt_socket_pool(pool)
        except Exception as exc:
            _log("mqtt socket_pool wrap phase=error error={}".format(exc))
    kwargs = {
        "broker": broker_target,
        "port": int(port or 1883),
        "socket_pool": client_pool,
        "keep_alive": 60,
    }
    if bool(getattr(mqtt, "use_tls", False)) or int(port or 1883) == 8883:
        import ssl  # type: ignore

        kwargs["ssl_context"] = ssl.create_default_context()
    if str(getattr(mqtt, "username", "") or ""):
        kwargs["username"] = mqtt.username
    if str(getattr(mqtt, "password", "") or ""):
        kwargs["password"] = mqtt.password
    _log(
        "mqtt client build broker={} port={} wrapped={} optional_kwargs={}".format(
            broker_target,
            int(port or 1883),
            1 if wrap_before_connect else 0,
            1 if optional_kwargs else 0,
        )
    )
    if not optional_kwargs:
        return MQTT(**kwargs)
    try:
        kwargs["socket_timeout"] = 3
        kwargs["connect_retries"] = 1
        return MQTT(**kwargs)
    except TypeError:
        try:
            del kwargs["socket_timeout"]
            del kwargs["connect_retries"]
        except Exception:
            pass
    return MQTT(**kwargs)


def _mqtt_set_runtime_timeout(client, timeout_s):
    timeout = float(timeout_s or 1)
    for attr_name in (
        "socket_timeout",
        "_socket_timeout",
        "recv_timeout",
        "_recv_timeout",
    ):
        try:
            setattr(client, attr_name, timeout)
            _log(
                "mqtt socket_timeout phase=set attr={} value={}".format(
                    attr_name,
                    timeout_s,
                )
            )
        except Exception as exc:
            _log(
                "mqtt socket_timeout phase=error attr={} error={}".format(
                    attr_name,
                    exc,
                )
            )
    sock = _safe_attr(client, "_sock")
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        try:
            settimeout(timeout)
            _log("mqtt socket_timeout phase=set target=sock value={}".format(
                timeout_s
            ))
        except Exception as exc:
            _log("mqtt socket_timeout phase=error target=sock error={}".format(exc))


def _log_client_state(client, prefix):
    sock = _safe_attr(client, "_sock")
    connected = _safe_attr(client, "_connected")
    backcompat = _safe_attr(client, "_backwards_compatible_sock")
    _log(
        "{} connected={} backcompat={} sock={}".format(
            prefix,
            connected if connected != "" else "unknown",
            backcompat if backcompat != "" else "unknown",
            _socket_summary(sock),
        )
    )


def _socket_summary(sock):
    if not sock:
        return "none"
    inner = _inner_socket(sock)
    summary = "{}:send{} recv{} recv_into{}".format(
        _type_name(sock),
        _callable_flag(sock, "send"),
        _callable_flag(sock, "recv"),
        _callable_flag(sock, "recv_into"),
    )
    if inner is not None:
        summary = "{} inner={}:send{} recv{} recv_into{}".format(
            summary,
            _type_name(inner),
            _callable_flag(inner, "send"),
            _callable_flag(inner, "recv"),
            _callable_flag(inner, "recv_into"),
        )
    return summary


def _inner_socket(sock):
    for attr_name in ("_socket", "_sock", "_socket_obj"):
        try:
            inner = getattr(sock, attr_name, None)
        except Exception:
            inner = None
        if inner is not None and inner is not sock:
            return inner
    return None


def _callable_flag(target, name):
    try:
        return 1 if callable(getattr(target, name, None)) else 0
    except Exception:
        return 0


def _type_name(value):
    try:
        return type(value).__name__
    except Exception:
        return "unknown"


def _mqtt_loop(client):
    timeout = _mqtt_poll_timeout(client)
    try:
        client.loop(timeout=timeout)
        return True
    except ValueError as exc:
        retry_timeout = _loop_timeout_from_error(exc)
        if retry_timeout <= timeout:
            raise
        _log(
            "mqtt poll phase=retry reason=timeout_mismatch timeout={} "
            "retry_timeout={}".format(timeout, retry_timeout)
        )
        client.loop(timeout=retry_timeout)
        return True
    except TypeError:
        client.loop()
        return True


def _mqtt_poll_timeout(client):
    for attr_name in (
        "socket_timeout",
        "_socket_timeout",
        "recv_timeout",
        "_recv_timeout",
    ):
        value = _safe_attr(client, attr_name)
        if _positive_number(value):
            return max(0.1, float(value))
    return 3.0


def _loop_timeout_from_error(exc):
    text = str(exc or "")
    marker = "socket timeout ("
    index = text.find(marker)
    if index < 0:
        return 0.0
    index += len(marker)
    end = text.find(")", index)
    if end < 0:
        end = len(text)
    try:
        return max(0.1, float(text[index:end]))
    except Exception:
        return 0.0


def _mqtt_one_firmware_round(
    adapter,
    transport,
    topic,
    payload,
    timeout_s,
    poll_interval_s,
    index,
    mode_name,
    direct_loop,
):
    from cpynodus_ii.core.mqtt_client import poll_mqtt_client, sync_transport_to_client

    _drain_transport_received(transport)
    _log(
        "mqtt mode={} round={} publish phase=start topic={} payload={}".format(
            mode_name,
            index,
            topic,
            payload,
        )
    )
    started = _monotonic()
    transport.publish(topic, payload, retain=False)
    sync_result = sync_transport_to_client(adapter, transport)
    adapter = sync_result.adapter
    _log(
        "mqtt mode={} round={} publish phase={} elapsed_s={:.1f} "
        "published={} errors={}".format(
            mode_name,
            index,
            sync_result.phase,
            _monotonic() - started,
            sync_result.published_count,
            _join_errors(sync_result.errors),
        )
    )
    if sync_result.phase != "synced" or sync_result.errors:
        transport.mark_disconnected(reason="platform_test_publish_failed")
        return None

    deadline = _monotonic() + float(timeout_s or 0.0)
    poll_count = 0
    while _monotonic() <= deadline:
        for message in _drain_transport_received(transport):
            received_topic = str(getattr(message, "topic", "") or "")
            received_payload = str(getattr(message, "payload_text", "") or "")
            _log(
                "mqtt mode={} rx topic={} payload={}".format(
                    mode_name,
                    received_topic,
                    received_payload,
                )
            )
            if received_topic == topic and received_payload == payload:
                _log(
                    "mqtt mode={} round={} phase=roundtrip_ok polls={}".format(
                        mode_name,
                        index,
                        poll_count,
                    )
                )
                return adapter
        _log(
            "mqtt mode={} round={} poll phase=start polls={} method={}".format(
                mode_name,
                index,
                poll_count,
                "direct_loop" if direct_loop else "transport",
            )
        )
        _log_client_state(
            adapter.client,
            "mqtt mode={} round={} poll client phase=before".format(
                mode_name,
                index,
            ),
        )
        try:
            if direct_loop:
                _mqtt_loop(adapter.client)
                poll_count += 1
                _sleep(poll_interval_s)
                continue
            poll_result = poll_mqtt_client(adapter, transport)
        except Exception as exc:
            _log(
                "mqtt mode={} round={} poll phase=exception polls={} "
                "type={} error={} connected={} pending={}".format(
                    mode_name,
                    index,
                    poll_count,
                    type(exc).__name__,
                    exc,
                    getattr(transport, "connected", "unknown"),
                    len(getattr(transport, "received_messages", ()) or ()),
                )
            )
            transport.mark_disconnected(reason="platform_test_poll_exception")
            return None
        adapter = poll_result.adapter
        if poll_result.phase == "error" or poll_result.errors:
            _log(
                "mqtt mode={} round={} poll phase={} polls={} "
                "received={} errors={}".format(
                    mode_name,
                    index,
                    poll_result.phase,
                    poll_count,
                    poll_result.received_count,
                    _join_errors(poll_result.errors),
                )
            )
            transport.mark_disconnected(reason="platform_test_poll_failed")
            return None
        poll_count += 1
        _sleep(poll_interval_s)
    _log(
        "mqtt mode={} round={} phase=timeout timeout_s={}".format(
            mode_name,
            index,
            timeout_s,
        )
    )
    transport.mark_disconnected(reason="platform_test_round_timeout")
    return None


def _drain_transport_received(transport):
    drain = getattr(transport, "drain_received", None)
    if callable(drain):
        return tuple(drain())
    return ()


def _mqtt_one_round(
    client,
    topic,
    payload,
    received,
    timeout_s,
    poll_interval_s,
    index,
):
    _log("mqtt round={} publish phase=start topic={} payload={}".format(
        index,
        topic,
        payload,
    ))
    started = _monotonic()
    try:
        try:
            client.publish(topic, payload, retain=False)
        except TypeError:
            client.publish(topic, payload)
    except Exception as exc:
        _log(
            "mqtt round={} publish phase=error elapsed_s={:.1f} type={} "
            "error={}".format(
                index,
                _monotonic() - started,
                type(exc).__name__,
                exc,
            )
        )
        return False
    _log("mqtt round={} publish phase=ok elapsed_s={:.1f}".format(
        index,
        _monotonic() - started,
    ))

    deadline = _monotonic() + float(timeout_s or 0.0)
    poll_count = 0
    while _monotonic() <= deadline:
        if _received_payload(received, topic, payload):
            _log("mqtt round={} phase=roundtrip_ok polls={}".format(
                index,
                poll_count,
            ))
            return True
        try:
            _mqtt_loop(client)
        except Exception as exc:
            _log(
                "mqtt round={} poll phase=error polls={} type={} error={}".format(
                    index,
                    poll_count,
                    type(exc).__name__,
                    exc,
                )
            )
            return False
        poll_count += 1
        _sleep(poll_interval_s)
    _log(
        "mqtt round={} phase=timeout timeout_s={} received_count={}".format(
            index,
            timeout_s,
            len(received),
        )
    )
    return False


def _received_payload(received, topic, payload):
    for received_topic, received_payload in tuple(received or ()):
        if str(received_topic or "") == topic and str(received_payload) == payload:
            return True
    return False


def _best_hint(current, networks, target_ssid):
    best = current
    for candidate in tuple(networks or ()):
        if best is None or _hint_rssi(candidate) > _hint_rssi(best):
            best = candidate
    return best


def _hint_channel(hint):
    try:
        return hint[0]
    except Exception:
        return None


def _hint_bssid(hint):
    try:
        return hint[1]
    except Exception:
        return None


def _hint_rssi(hint):
    try:
        return int(hint[2])
    except Exception:
        return -999


def _reset_station(radio):
    _call(radio, "stop_ap")
    _call(radio, "disconnect")
    if _call(radio, "stop_station"):
        _call(radio, "start_station")


def _radio_has_ip(radio):
    return bool(_safe_attr(radio, "ipv4_address"))


def _set_hostname(radio, hostname):
    try:
        radio.hostname = hostname
        _log("wifi hostname phase=set value={}".format(hostname))
    except Exception as exc:
        _log("wifi hostname phase=error error={}".format(exc))


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


def _payload_text(value):
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


def _positive_number(value):
    try:
        return float(value) > 0.0
    except Exception:
        return False


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


def _datetime_text(current_datetime):
    try:
        parts = tuple(current_datetime)
        return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
            int(parts[0]),
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
        )
    except Exception:
        return str(current_datetime)


def _join_errors(errors):
    return ",".join(str(error) for error in tuple(errors or ())) or "none"


def _join_values(values):
    return ",".join(str(value) for value in tuple(values or ()) if value != "")


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
    print("platform_test {}".format(message))
