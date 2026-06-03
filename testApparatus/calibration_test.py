"""CircuitPython calibration command probe for Nodus firmware.

Copy this file to the CIRCUITPY root and run it from the REPL while the normal
app is stopped:

    import calibration_test
    calibration_test.run(
        device_id="co2-ykdvea",
        offsets=(("Calibration.Device.CO2_OFFSET", -100.0),),
    )

Run the same command path under increasing heap pressure:

    calibration_test.pressure_sweep(
        offsets=(("Calibration.Device.CO2_OFFSET", -100.0),),
        step_bytes=50 * 1024,
        stop_on_failure=True,
    )

By default ``pressure_sweep()`` keeps sweeping after a command failure so the
log shows the failure shape from maximum free heap down to the allocation limit.
Pass ``stop_sweep_on_failure=True`` to stop at the first failed pressure point.

Hold unused network sockets during each command:

    calibration_test.run(
        offsets=(("Calibration.Device.CO2_OFFSET", -100.0),),
        socket_count=2,
    )

Exercise the real MQTT round-trip path with the same command intake:

    calibration_test.mqtt_round_trip(
        offsets=(("Calibration.Device.CO2_OFFSET", -100.0),),
        self_publish=True,
    )

By default the probe loads the app's real runtime config, injects commands
through ``cpynodus_ii.features.command_intake.process_inbound_messages()``, and
does not publish to MQTT or persist to TOML. To test the current firmware
persistence path too, pass ``persist=True``.
"""

import gc
import json
import time

DEFAULT_BASE_TOPIC = "nodus"
DEFAULT_SENSOR_FILE = "sensor_i2c.toml"


def run(
    *,
    device_id="",
    offsets=(),
    co2_offset=None,
    altitude=None,
    temp_offset=None,
    rh_offset=None,
    base_topic="",
    root=".",
    persist=False,
    repeat=1,
    runtime_mode="app",
    action="apply",
    preimport=False,
    import_each=False,
    collect_before=False,
    collect_between=True,
    stop_on_failure=True,
    allow_volatile_persistence=True,
    socket_count=0,
    socket_connect_host="",
    socket_connect_port=1883,
    socket_timeout=1.0,
):
    """Inject serialized one-offset calibration/set messages locally."""
    _log("calibration_test phase=start")
    settings = _read_settings(root)
    offset_list = _offsets_from_args(
        offsets,
        co2_offset=co2_offset,
        altitude=altitude,
        temp_offset=temp_offset,
        rh_offset=rh_offset,
    )
    if not offset_list:
        _log("calibration_test phase=error errors=offsets_missing")
        return False

    runtime_config = _load_runtime_config(
        root,
        settings,
        runtime_mode=runtime_mode,
        device_id=device_id,
        base_topic=base_topic,
    )
    if runtime_config is None:
        return False
    device_id = _config_device_id(runtime_config)
    if not device_id:
        _log("calibration_test phase=error errors=device_id_missing")
        return False
    base_topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "")
    base_topic = base_topic or DEFAULT_BASE_TOPIC
    settings_root = root if persist else None
    _log(
        (
            "calibration_test target={} base_topic={} persist={} "
            "offsets={} repeat={} runtime_mode={} action={} "
            "collect_before={} collect_between={} socket_count={} "
            "allow_volatile_persistence={}"
        ).format(
            device_id,
            base_topic,
            1 if persist else 0,
            len(offset_list),
            int(repeat or 1),
            runtime_mode,
            action,
            1 if collect_before else 0,
            1 if collect_between else 0,
            int(socket_count or 0),
            1 if allow_volatile_persistence else 0,
        )
    )
    _memory("start", collect=True)

    if preimport:
        if not probe_imports():
            return False

    held_sockets = _open_socket_holds(
        socket_count,
        connect_host=socket_connect_host,
        connect_port=socket_connect_port,
        timeout=socket_timeout,
    )
    if int(socket_count or 0) and len(held_sockets) < int(socket_count or 0):
        _close_socket_holds(held_sockets)
        _log("calibration_test phase=done result=fail errors=socket_hold_incomplete")
        return False

    try:
        ok = True
        command_index = 0
        total = max(1, int(repeat or 1)) * len(offset_list)
        for repeat_index in range(1, max(1, int(repeat or 1)) + 1):
            for offset in offset_list:
                command_index += 1
                result_ok = _run_one(
                    runtime_config,
                    offset,
                    settings_root=settings_root,
                    command_index=command_index,
                    repeat_index=repeat_index,
                    total=total,
                    action=action,
                    import_each=import_each,
                    collect_before=collect_before,
                    allow_volatile_persistence=allow_volatile_persistence,
                )
                if not result_ok:
                    ok = False
                    if stop_on_failure:
                        _log("calibration_test phase=done result=fail")
                        return False
                if collect_between:
                    _memory("between_commands", collect=True)
        _log("calibration_test phase=done result={}".format("pass" if ok else "fail"))
        return ok
    finally:
        _close_socket_holds(held_sockets)


def pressure_sweep(
    *,
    device_id="",
    offsets=(),
    co2_offset=None,
    altitude=None,
    temp_offset=None,
    rh_offset=None,
    base_topic="",
    root=".",
    persist=False,
    repeat=1,
    runtime_mode="app",
    action="apply",
    preimport=False,
    import_each=False,
    collect_before=False,
    collect_between=True,
    stop_on_failure=True,
    stop_sweep_on_failure=False,
    allow_volatile_persistence=True,
    step_bytes=50 * 1024,
    max_pressure_bytes=0,
    min_free_bytes=24 * 1024,
    block_bytes=1024,
    socket_count=0,
    socket_connect_host="",
    socket_connect_port=1883,
    socket_timeout=1.0,
):
    """Run calibration tests while holding progressively more heap."""
    _log(
        (
            "pressure_sweep phase=start step_bytes={} max_pressure_bytes={} "
            "min_free_bytes={} block_bytes={} socket_count={} "
            "stop_sweep_on_failure={} allow_volatile_persistence={}"
        ).format(
            int(step_bytes or 0),
            int(max_pressure_bytes or 0),
            int(min_free_bytes or 0),
            int(block_bytes or 0),
            int(socket_count or 0),
            1 if stop_sweep_on_failure else 0,
            1 if allow_volatile_persistence else 0,
        )
    )
    _memory("pressure_sweep_start", collect=True)
    settings = _read_settings(root)
    offset_list = _offsets_from_args(
        offsets,
        co2_offset=co2_offset,
        altitude=altitude,
        temp_offset=temp_offset,
        rh_offset=rh_offset,
    )
    if not offset_list:
        _log("pressure_sweep phase=error errors=offsets_missing")
        return False

    runtime_config = _load_runtime_config(
        root,
        settings,
        runtime_mode=runtime_mode,
        device_id=device_id,
        base_topic=base_topic,
    )
    if runtime_config is None:
        _log("pressure_sweep phase=done result=fail errors=runtime_config")
        return False
    device_id = _config_device_id(runtime_config)
    if not device_id:
        _log("pressure_sweep phase=error errors=device_id_missing")
        return False
    base_topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "")
    base_topic = base_topic or DEFAULT_BASE_TOPIC
    settings_root = root if persist else None
    _log(
        (
            "pressure_sweep target={} base_topic={} persist={} offsets={} "
            "repeat={} runtime_mode={} action={} collect_before={} "
            "collect_between={}"
        ).format(
            device_id,
            base_topic,
            1 if persist else 0,
            len(offset_list),
            int(repeat or 1),
            runtime_mode,
            action,
            1 if collect_before else 0,
            1 if collect_between else 0,
        )
    )

    if preimport:
        if not probe_imports():
            _log("pressure_sweep phase=done result=fail errors=preimport")
            return False

    pressure = 0
    results = []
    ok = True
    total = max(1, int(repeat or 1)) * len(offset_list)
    while True:
        blocks = []
        pressure_ok = _allocate_pressure(
            blocks,
            pressure,
            block_bytes=block_bytes,
            min_free_bytes=min_free_bytes,
        )
        _memory("pressure_ready target_bytes={}".format(pressure), collect=False)
        ready_free = _free_mem()
        if not pressure_ok and pressure > 0:
            results.append((pressure, ready_free, "allocation_limit"))
            _log(
                (
                    "pressure_sweep phase=allocation_limit target_bytes={} "
                    "ready_free_mem={}"
                ).format(
                    pressure,
                    ready_free,
                )
            )
            blocks = None
            _memory("pressure_released", collect=True)
            break
        held_sockets = ()
        try:
            held_sockets = _open_socket_holds(
                socket_count,
                connect_host=socket_connect_host,
                connect_port=socket_connect_port,
                timeout=socket_timeout,
            )
            if int(socket_count or 0) and len(held_sockets) < int(socket_count or 0):
                result = False
                _log(
                    (
                        "pressure_sweep phase=socket_hold_incomplete "
                        "target_bytes={} requested={} opened={}"
                    ).format(
                        pressure,
                        int(socket_count or 0),
                        len(held_sockets),
                    )
                )
            else:
                result = True
                command_index = 0
                stop_commands = False
                for repeat_index in range(1, max(1, int(repeat or 1)) + 1):
                    for offset in offset_list:
                        command_index += 1
                        result_ok = _run_one(
                            runtime_config,
                            offset,
                            settings_root=settings_root,
                            command_index=command_index,
                            repeat_index=repeat_index,
                            total=total,
                            action=action,
                            import_each=import_each,
                            collect_before=collect_before,
                            allow_volatile_persistence=allow_volatile_persistence,
                        )
                        if not result_ok:
                            result = False
                            if stop_on_failure:
                                stop_commands = True
                                break
                        if collect_between:
                            _memory("between_commands", collect=True)
                    if stop_commands:
                        break
        except MemoryError:
            _memory("pressure_command_memory_error", collect=True)
            result = False
        finally:
            _close_socket_holds(held_sockets)
        results.append((pressure, ready_free, "pass" if result else "fail"))
        _log(
            "pressure_sweep pass target_bytes={} ready_free_mem={} result={}".format(
                pressure,
                ready_free,
                "pass" if result else "fail",
            )
        )
        blocks = None
        _memory("pressure_released", collect=True)
        if not result:
            ok = False
            if stop_sweep_on_failure:
                break
        pressure += max(1, int(step_bytes or 1))
        if max_pressure_bytes and pressure > int(max_pressure_bytes):
            break
    _log("pressure_sweep phase=summary results={}".format(_format_sweep(results)))
    _log("pressure_sweep phase=done result={}".format("pass" if ok else "fail"))
    return ok


def mqtt_round_trip(
    *,
    device_id="",
    offsets=(),
    co2_offset=None,
    altitude=None,
    temp_offset=None,
    rh_offset=None,
    base_topic="",
    root=".",
    persist=False,
    repeat=1,
    runtime_mode="app",
    action="apply",
    command_source="broker",
    self_publish=True,
    wait_external=False,
    wifi_timeout_s=20.0,
    wifi_attempts=3,
    wifi_retry_delay_s=2.0,
    mqtt_timeout_s=20.0,
    command_timeout_s=20.0,
    output_timeout_s=6.0,
    poll_interval_s=0.20,
    pressure_bytes=0,
    min_free_bytes=24 * 1024,
    block_bytes=1024,
    mqtt_host="",
    mqtt_port=0,
    preflight=True,
    preconnect_probe=True,
    preflight_retries=3,
    preflight_retry_delay_s=0.5,
    connect_delay_s=5.0,
    subscribe_outputs=True,
    collect_before=False,
    collect_between=True,
    stop_on_failure=True,
    allow_volatile_persistence=True,
):
    """Run calibration/set through the real MQTT client and broker."""
    _log(
        (
            "mqtt_round phase=start self_publish={} wait_external={} repeat={} "
            "pressure_bytes={} subscribe_outputs={} command_source={} "
            "allow_volatile_persistence={}"
        ).format(
            1 if self_publish else 0,
            1 if wait_external else 0,
            int(repeat or 1),
            int(pressure_bytes or 0),
            1 if subscribe_outputs else 0,
            command_source or "broker",
            1 if allow_volatile_persistence else 0,
        )
    )
    _memory("mqtt_round_start", collect=True)
    settings = _read_settings(root)
    offset_list = _offsets_from_args(
        offsets,
        co2_offset=co2_offset,
        altitude=altitude,
        temp_offset=temp_offset,
        rh_offset=rh_offset,
    )
    if not offset_list:
        _log("mqtt_round phase=error errors=offsets_missing")
        return False

    runtime_config = _load_runtime_config(
        root,
        settings,
        runtime_mode=runtime_mode,
        device_id=device_id,
        base_topic=base_topic,
    )
    if runtime_config is None:
        _log("mqtt_round phase=done result=fail errors=runtime_config")
        return False
    _override_mqtt_target(runtime_config, mqtt_host=mqtt_host, mqtt_port=mqtt_port)
    device_id = _config_device_id(runtime_config)
    if not device_id:
        _log("mqtt_round phase=error errors=device_id_missing")
        return False
    base_topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "")
    base_topic = base_topic or DEFAULT_BASE_TOPIC
    settings_root = root if persist else None
    _log(
        (
            "mqtt_round target={} base_topic={} persist={} offsets={} "
            "runtime_mode={} action={} collect_before={} collect_between={}"
        ).format(
            device_id,
            base_topic,
            1 if persist else 0,
            len(offset_list),
            runtime_mode,
            action,
            1 if collect_before else 0,
            1 if collect_between else 0,
        )
    )

    if not _ensure_wifi_connected(
        runtime_config,
        timeout_s=wifi_timeout_s,
        attempts=wifi_attempts,
        retry_delay_s=wifi_retry_delay_s,
    ):
        _log("mqtt_round phase=done result=fail errors=wifi")
        return False

    adapter = None
    transport = None
    pressure_blocks = []
    try:
        adapter, transport = _open_mqtt_session(
            runtime_config,
            timeout_s=mqtt_timeout_s,
            preflight=preflight,
            preconnect_probe=preconnect_probe,
            preflight_retries=preflight_retries,
            preflight_retry_delay_s=preflight_retry_delay_s,
            connect_delay_s=connect_delay_s,
        )
        if adapter is None or transport is None:
            _log("mqtt_round phase=done result=fail errors=mqtt_session")
            return False

        topics = _calibration_topics(runtime_config)
        source_mode = str(command_source or "broker").strip().lower()
        if source_mode != "direct":
            _subscribe_mqtt_topics(
                transport,
                topics,
                include_outputs=subscribe_outputs,
            )
            adapter, ok = _sync_mqtt_until_idle(
                adapter,
                transport,
                label="subscriptions",
                timeout_s=mqtt_timeout_s,
                poll_interval_s=poll_interval_s,
            )
            if not ok:
                _log("mqtt_round phase=done result=fail errors=subscribe")
                return False
        else:
            _log("mqtt_round subscribe phase=skipped reason=direct_command_source")
        if self_publish and source_mode != "direct":
            adapter = _drain_mqtt_for_settle(
                adapter,
                transport,
                label="post_subscribe",
                duration_s=1.0,
                poll_interval_s=poll_interval_s,
            )

        if int(pressure_bytes or 0) > 0:
            pressure_ok = _allocate_pressure(
                pressure_blocks,
                pressure_bytes,
                block_bytes=block_bytes,
                min_free_bytes=min_free_bytes,
            )
            _memory("mqtt_round_pressure_ready", collect=False)
            if not pressure_ok:
                _log("mqtt_round phase=done result=fail errors=pressure")
                return False

        ok = True
        command_index = 0
        total = max(1, int(repeat or 1)) * len(offset_list)
        for repeat_index in range(1, max(1, int(repeat or 1)) + 1):
            for offset in offset_list:
                command_index += 1
                result = _mqtt_round_one_command(
                    adapter,
                    transport,
                    runtime_config,
                    offset,
                    settings_root=settings_root,
                    command_index=command_index,
                    repeat_index=repeat_index,
                    total=total,
                    action=action,
                    command_source=source_mode,
                    self_publish=self_publish,
                    wait_external=wait_external,
                    command_timeout_s=command_timeout_s,
                    output_timeout_s=output_timeout_s,
                    poll_interval_s=poll_interval_s,
                    collect_before=collect_before,
                    collect_between=collect_between,
                    subscribe_outputs=subscribe_outputs,
                    topics=topics,
                    allow_volatile_persistence=allow_volatile_persistence,
                )
                adapter = result[0]
                if not result[1]:
                    ok = False
                    if stop_on_failure:
                        _log("mqtt_round phase=done result=fail")
                        return False
        _log("mqtt_round phase=done result={}".format("pass" if ok else "fail"))
        return ok
    finally:
        pressure_blocks = None
        _memory("mqtt_round_pressure_released", collect=True)
        _close_mqtt_session(adapter, transport, runtime_config)


def probe_imports():
    """Import calibration modules one by one and log memory after each step."""
    modules = (
        "cpynodus_ii.features.command_intake",
        "cpynodus_ii.features.calibration_offsets",
        "cpynodus_ii.features.calibration_offset_parse",
        "cpynodus_ii.features.calibration_offset_persistence",
        "cpynodus_ii.features.calibration_config",
        "cpynodus_ii.features.calibration_persistence",
    )
    _log("probe_imports phase=start")
    _memory("before_imports")
    for name in modules:
        try:
            __import__(name)
            _memory("import_ok module={}".format(name), collect=True)
        except MemoryError:
            _memory("import_memory_error module={}".format(name), collect=True)
            return False
        except Exception as exc:
            _log(
                "probe_imports phase=error module={} type={} error={}".format(
                    name,
                    type(exc).__name__,
                    exc,
                )
            )
            return False
    _log("probe_imports phase=done result=pass")
    return True


def _run_one(
    runtime_config,
    offset,
    *,
    settings_root,
    command_index,
    repeat_index,
    total,
    action,
    import_each,
    collect_before,
    allow_volatile_persistence,
):
    topic = "{}/{}/calibration/set".format(
        runtime_config.mqtt.base_topic,
        runtime_config.sensor.sensor_id,
    )
    message_id = "caltest-{}-r{}-{}".format(
        int(time.monotonic()),
        int(repeat_index or 1),
        int(command_index or 0),
    )
    payload = {
        "message_id": message_id,
        "action": str(action or "apply"),
        "payload": {
            "offsets": [
                {
                    "key": offset[0],
                    "value": offset[1],
                }
            ]
        },
    }
    payload_text = json.dumps(payload, separators=(",", ":"))
    _log(
        "command {}/{} phase=start message_id={} key={} value={}".format(
            command_index,
            total,
            message_id,
            offset[0],
            offset[1],
        )
    )
    if import_each:
        probe_imports()
    _memory("before_command", collect=collect_before)
    transport = _Transport((topic, payload_text))
    try:
        from cpynodus_ii.features.command_intake import process_inbound_messages

        results = process_inbound_messages(
            transport,
            runtime_config,
            None,
            handled_message_ids=(),
            settings_root=settings_root,
        )
    except MemoryError:
        _memory("command_memory_error_uncaught", collect=True)
        return False
    except Exception as exc:
        _memory("command_exception", collect=True)
        _log(
            "command phase=exception type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return False
    _memory("after_command", collect=True)
    _log_results(results)
    _log_published(transport.published)
    return _results_ok(
        results,
        allow_volatile_persistence=allow_volatile_persistence,
    )


class _RuntimeConfig:
    pass


class _Object:
    pass


def _load_runtime_config(root, settings, *, runtime_mode, device_id, base_topic):
    mode = str(runtime_mode or "app").strip().lower()
    if mode in ("app", "settings", "real"):
        try:
            from cpynodus_ii.core.settings import Settings

            config = Settings.from_directory(root or ".").runtime_config()
            _override_runtime_config(config, device_id=device_id, base_topic=base_topic)
            _log("runtime_config phase=loaded mode=app")
            return config
        except MemoryError:
            _memory("runtime_config_memory_error", collect=True)
            return None
        except Exception as exc:
            _log(
                "runtime_config phase=error mode=app type={} error={}".format(
                    type(exc).__name__,
                    exc,
                )
            )
            return None

    device_id = _device_id(device_id, settings)
    base_topic = str(base_topic or _settings_value(settings, "MQTT", "BASE_TOPIC"))
    config = _minimal_runtime_config(
        device_id=device_id,
        base_topic=base_topic or DEFAULT_BASE_TOPIC,
        sensor_file=DEFAULT_SENSOR_FILE,
    )
    _log("runtime_config phase=loaded mode=minimal")
    return config


def _override_runtime_config(runtime_config, *, device_id, base_topic):
    if str(base_topic or "").strip():
        try:
            runtime_config.mqtt.base_topic = str(base_topic or "").strip()
        except Exception:
            pass
    if str(device_id or "").strip():
        value = str(device_id or "").strip()
        try:
            runtime_config.sensor.sensor_id = value
        except Exception:
            pass
        try:
            runtime_config.network.hostname = value
        except Exception:
            pass


def _config_device_id(runtime_config):
    for owner, name in (
        (getattr(runtime_config, "sensor", None), "sensor_id"),
        (getattr(runtime_config, "switch", None), "device_id"),
        (getattr(runtime_config, "network", None), "hostname"),
    ):
        try:
            value = str(getattr(owner, name, "") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _runtime_config(*, device_id, base_topic, sensor_file):
    return _minimal_runtime_config(
        device_id=device_id,
        base_topic=base_topic,
        sensor_file=sensor_file,
    )


def _minimal_runtime_config(*, device_id, base_topic, sensor_file):
    config = _RuntimeConfig()
    config.mqtt = _Object()
    config.mqtt.base_topic = str(base_topic or DEFAULT_BASE_TOPIC)
    config.network = _Object()
    config.network.hostname = str(device_id or "")
    config.sensor = _Object()
    config.sensor.present = True
    config.sensor.sensor_id = str(device_id or "")
    config.sensor.family = "i2c"
    config.sensor.active_config_file = str(sensor_file or DEFAULT_SENSOR_FILE)
    config.sensor.calibration_device = _Object()
    config.sensor.calibration_system = _Object()
    config.switch = _Object()
    config.switch.present = False
    config.switch.device_id = ""
    config.switch.channels = ()
    return config


class _Message:
    def __init__(self, topic, payload_text):
        self.topic = topic
        self.payload_text = payload_text


class _PublishRecord:
    def __init__(self, topic, payload, retain):
        self.topic = topic
        self.payload = payload
        self.retain = bool(retain)


class _Transport:
    def __init__(self, message):
        self.received_messages = (_Message(message[0], message[1]),)
        self.published = []

    def drain_received(self):
        messages = self.received_messages
        self.received_messages = ()
        return messages

    def receive(self, topic, payload_text):
        self.received_messages = self.received_messages + (
            _Message(topic, payload_text),
        )

    def publish(self, topic, payload, retain=False):
        record = _PublishRecord(topic, payload, retain)
        self.published.append(record)
        return record


def _offsets_from_args(
    offsets,
    *,
    co2_offset=None,
    altitude=None,
    temp_offset=None,
    rh_offset=None,
):
    values = []
    for item in tuple(offsets or ()):
        values.append(_normalize_offset(item))
    if co2_offset is not None:
        values.append(("Calibration.Device.CO2_OFFSET", float(co2_offset)))
    if altitude is not None:
        values.append(("Calibration.Device.ALTITUDE_METERS", float(altitude)))
    if temp_offset is not None:
        values.append(("Calibration.Device.TEMP_OFFSET", float(temp_offset)))
    if rh_offset is not None:
        values.append(("Calibration.Device.RH_OFFSET", float(rh_offset)))
    return tuple(values)


def _normalize_offset(item):
    if isinstance(item, str):
        if "=" not in item:
            return (_normalize_key(item), 0.0)
        key, value = item.split("=", 1)
        return (_normalize_key(key), _scalar(value))
    try:
        key = item[0]
        value = item[1]
    except Exception:
        return ("", 0.0)
    return (_normalize_key(key), value)


def _normalize_key(key):
    text = str(key or "").strip()
    if not text:
        return ""
    if "." in text:
        return text
    return "Calibration.Device.{}".format(text.upper())


def _scalar(value):
    text = str(value or "").strip()
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return float(text)
    except ValueError:
        return text


def _log_results(results):
    for index, result in enumerate(tuple(results or ()), start=1):
        _log(
            (
                "result index={} phase={} type={} message_id={} "
                "published={} persistence={} requested={} errors={}"
            ).format(
                index,
                getattr(result, "phase", ""),
                getattr(result, "command_type", ""),
                getattr(result, "message_id", ""),
                getattr(result, "published_count", 0),
                getattr(result, "persistence_mode", ""),
                getattr(result, "requested_state", ""),
                _join_errors(getattr(result, "errors", ())),
            )
        )


def _log_published(records):
    for index, record in enumerate(tuple(records or ()), start=1):
        _log(
            "publish index={} topic={} retain={} payload={}".format(
                index,
                record.topic,
                1 if record.retain else 0,
                _compact_payload(record.payload),
            )
        )


def _results_ok(results, *, allow_volatile_persistence=False):
    items = tuple(results or ())
    if not items:
        return False
    for result in items:
        if str(getattr(result, "phase", "") or "") not in ("published", "ready"):
            return False
        errors = tuple(getattr(result, "errors", ()) or ())
        if errors and not _volatile_persistence_ok(
            result,
            errors,
            allow_volatile_persistence=allow_volatile_persistence,
        ):
            return False
    return True


def _volatile_persistence_ok(result, errors, *, allow_volatile_persistence):
    if not allow_volatile_persistence:
        return False
    if str(getattr(result, "persistence_mode", "") or "") != "volatile":
        return False
    for error in tuple(errors or ()):
        text = str(error or "").strip().lower()
        if not text:
            continue
        if "persist" in text:
            continue
        if text == "pystack_exhausted":
            continue
        return False
    return True


def _compact_payload(payload):
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, separators=(",", ":"))
        except Exception:
            return str(payload)
    return str(payload)


def _read_settings(root):
    path = _join_path(root, "settings.toml")
    document = {}
    section = ""
    try:
        with open(path, "r") as handle:
            while True:
                line = handle.readline()
                if line == "":
                    break
                text = _strip_comment(line).strip()
                if not text:
                    continue
                if text.startswith("[") and text.endswith("]"):
                    section = text[1:-1].strip()
                    if section not in document:
                        document[section] = {}
                    continue
                if "=" in text and section:
                    key, value = text.split("=", 1)
                    document[section][key.strip().upper()] = _toml_scalar(value)
    except OSError as exc:
        _log("settings phase=warn path={} error={}".format(path, exc))
    return document


def _strip_comment(line):
    text = str(line or "")
    in_string = False
    escaped = False
    out = []
    for char in text:
        if char == '"' and not escaped:
            in_string = not in_string
        if char == "#" and not in_string:
            break
        out.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return "".join(out)


def _toml_scalar(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _settings_value(settings, section, key):
    try:
        return settings.get(section, {}).get(key, "")
    except Exception:
        return ""


def _device_id(device_id, settings):
    value = str(device_id or "").strip()
    if value:
        return value
    value = str(_settings_value(settings, "Network", "HOSTNAME") or "").strip()
    return value


def _join_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


def _allocate_pressure(blocks, target_bytes, *, block_bytes, min_free_bytes):
    target = max(0, int(target_bytes or 0))
    block_size = max(16, int(block_bytes or 1024))
    min_free = max(0, int(min_free_bytes or 0))
    allocated = 0
    _log(
        "pressure_alloc phase=start target_bytes={} block_bytes={}".format(
            target,
            block_size,
        )
    )
    while allocated < target:
        remaining = target - allocated
        chunk = block_size if remaining > block_size else remaining
        free_mem = _free_mem()
        if free_mem >= 0 and free_mem - chunk < min_free:
            _log(
                (
                    "pressure_alloc phase=min_free_stop allocated={} "
                    "requested={} free_mem={} min_free={}"
                ).format(
                    allocated,
                    target,
                    free_mem,
                    min_free,
                )
            )
            return False
        try:
            blocks.append(bytearray(chunk))
            allocated += chunk
        except MemoryError:
            _log(
                "pressure_alloc phase=memory_error allocated={} requested={}".format(
                    allocated,
                    target,
                )
            )
            return False
    _log(
        "pressure_alloc phase=done allocated={} requested={}".format(
            allocated,
            target,
        )
    )
    return True


def _format_sweep(results):
    parts = []
    for item in tuple(results or ()):
        pressure = int(item[0] or 0)
        if len(item) >= 3:
            free_mem = int(item[1] or 0)
            status = str(item[2] or "")
        else:
            free_mem = -1
            status = "pass" if item[1] else "fail"
        if free_mem >= 0:
            parts.append(
                "pressure={} free={} result={}".format(
                    pressure,
                    free_mem,
                    status,
                )
            )
        else:
            parts.append("pressure={} result={}".format(pressure, status))
    return ",".join(parts) or "none"


def _mqtt_round_one_command(
    adapter,
    transport,
    runtime_config,
    offset,
    *,
    settings_root,
    command_index,
    repeat_index,
    total,
    action,
    command_source,
    self_publish,
    wait_external,
    command_timeout_s,
    output_timeout_s,
    poll_interval_s,
    collect_before,
    collect_between,
    subscribe_outputs,
    topics,
    allow_volatile_persistence,
):
    set_topic, ack_topic, result_topic, patch_topic = topics
    message_id = "calmqtt-{}-r{}-{}".format(
        int(time.monotonic()),
        int(repeat_index or 1),
        int(command_index or 0),
    )
    payload_text = _calibration_payload_text(offset, message_id, action=action)
    _log(
        (
            "mqtt_round command {}/{} phase=start message_id={} key={} "
            "value={}"
        ).format(
            command_index,
            total,
            message_id,
            offset[0],
            offset[1],
        )
    )
    source_mode = str(command_source or "broker").strip().lower()
    if source_mode == "direct":
        _memory("mqtt_round_before_direct_receive", collect=False)
        transport.receive(set_topic, payload_text)
        _log(
            "mqtt_round command phase=direct_receive topic={} message_id={}".format(
                set_topic,
                message_id,
            )
        )
        received_payload = payload_text
        output_message_id = message_id
    elif not self_publish and not wait_external:
        _log("mqtt_round command phase=error errors=no_command_source")
        return adapter, False
    else:
        if self_publish:
            _memory("mqtt_round_before_set_publish", collect=False)
            transport.publish(set_topic, payload_text, retain=False)
            adapter, ok = _sync_mqtt_until_idle(
                adapter,
                transport,
                label="publish_set",
                timeout_s=command_timeout_s,
                poll_interval_s=poll_interval_s,
            )
            if not ok:
                return adapter, False

        adapter, received = _wait_for_mqtt_message(
            adapter,
            transport,
            set_topic,
            payload_text if self_publish else "",
            timeout_s=command_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        if not received:
            _log(
                "mqtt_round command phase=timeout topic={} message_id={}".format(
                    set_topic,
                    message_id,
                )
            )
            return adapter, False
        received_payload = _first_received_payload(
            transport,
            set_topic,
            payload_text if self_publish else "",
        )
        output_message_id = _payload_message_id(received_payload) or message_id

    if source_mode == "direct":
        _log(
            "mqtt_round command phase=received topic={} bytes={}".format(
                set_topic,
                len(received_payload or ""),
            )
        )

    _memory("mqtt_round_before_command", collect=collect_before)
    try:
        from cpynodus_ii.features.command_intake import process_inbound_messages

        results = process_inbound_messages(
            transport,
            runtime_config,
            None,
            handled_message_ids=(),
            settings_root=settings_root,
        )
    except MemoryError:
        _memory("mqtt_round_command_memory_error", collect=True)
        return adapter, False
    except Exception as exc:
        _memory("mqtt_round_command_exception", collect=True)
        _log(
            "mqtt_round command phase=exception type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return adapter, False
    _memory("mqtt_round_after_command", collect=True)
    _log_results(results)
    adapter, ok = _sync_mqtt_until_idle(
        adapter,
        transport,
        label="publish_outputs",
        timeout_s=output_timeout_s,
        poll_interval_s=poll_interval_s,
    )
    if not ok:
        return adapter, False
    output_ok = True
    if subscribe_outputs and source_mode != "direct":
        adapter, output_ok = _wait_for_output_echoes(
            adapter,
            transport,
            ack_topic,
            result_topic,
            patch_topic,
            output_message_id,
            timeout_s=output_timeout_s,
            poll_interval_s=poll_interval_s,
        )
    if collect_between:
        _memory("mqtt_round_between_commands", collect=True)
    return adapter, bool(
        _results_ok(
            results,
            allow_volatile_persistence=allow_volatile_persistence,
        )
        and output_ok
    )


def _calibration_payload_text(offset, message_id, *, action):
    payload = {
        "message_id": message_id,
        "action": str(action or "apply"),
        "payload": {
            "offsets": [
                {
                    "key": offset[0],
                    "value": offset[1],
                }
            ]
        },
    }
    return json.dumps(payload, separators=(",", ":"))


def _calibration_topics(runtime_config):
    base_topic = str(getattr(runtime_config.mqtt, "base_topic", "") or "")
    base_topic = base_topic or DEFAULT_BASE_TOPIC
    device_id = _config_device_id(runtime_config)
    prefix = "{}/{}".format(base_topic, device_id)
    return (
        "{}/calibration/set".format(prefix),
        "{}/calibration/ack".format(prefix),
        "{}/calibration/result".format(prefix),
        "{}/meta/patch".format(prefix),
    )


def _subscribe_mqtt_topics(transport, topics, *, include_outputs):
    set_topic, ack_topic, result_topic, patch_topic = topics
    selected = (set_topic,)
    if include_outputs:
        selected = (set_topic, ack_topic, result_topic, patch_topic)
    for topic in selected:
        _log("mqtt_round subscribe queue topic={}".format(topic))
        transport.subscribe(topic)


def _ensure_wifi_connected(runtime_config, *, timeout_s, attempts=3, retry_delay_s=2.0):
    _memory("wifi_before", collect=True)
    try:
        import wifi
    except Exception as exc:
        _log("wifi phase=import_error type={} error={}".format(type(exc).__name__, exc))
        return False
    try:
        if getattr(wifi.radio, "connected", False):
            _log("wifi phase=already_connected ipv4={}".format(_wifi_ipv4(wifi)))
            _memory("wifi_ready", collect=True)
            return True
    except Exception:
        pass
    ssid = str(getattr(runtime_config.network, "ssid", "") or "").strip()
    password = str(getattr(runtime_config.network, "password", "") or "")
    if not ssid:
        _log("wifi phase=error errors=ssid_missing")
        return False
    try:
        hostname = str(getattr(runtime_config.network, "hostname", "") or "").strip()
        if hostname:
            wifi.radio.hostname = hostname
    except Exception:
        pass

    max_attempts = max(1, int(attempts or 1))
    retry_delay = max(0.0, float(retry_delay_s or 0.0))
    _wifi_prepare_station(wifi)
    _wifi_reset_station(wifi, reason="startup")
    last_error = "none"
    for attempt in range(1, max_attempts + 1):
        _log("wifi connect phase=start attempt={} ssid={}".format(attempt, ssid))
        started = time.monotonic()
        try:
            _wifi_prepare_station(wifi)
            _wifi_radio_connect(
                wifi.radio.connect,
                ssid,
                password,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            last_error = "{}:{}".format(type(exc).__name__, exc)
            _memory("wifi_connect_error", collect=True)
            _log(
                (
                    "wifi connect phase=error attempt={} elapsed_s={:.1f} "
                    "type={} error={}"
                ).format(
                    attempt,
                    time.monotonic() - started,
                    type(exc).__name__,
                    exc,
                )
            )
            if attempt < max_attempts:
                _wifi_reset_station(wifi, reason="retry")
                _log(
                    "wifi connect phase=retry attempt={} delay_s={:.1f}".format(
                        attempt,
                        retry_delay,
                    )
                )
                _sleep(retry_delay)
                continue
            break

        deadline = started + float(timeout_s or 0.0)
        while time.monotonic() <= deadline:
            try:
                if getattr(wifi.radio, "connected", False):
                    _log(
                        (
                            "wifi connect phase=ready attempt={} "
                            "elapsed_s={:.1f} ipv4={}"
                        ).format(
                            attempt,
                            time.monotonic() - started,
                            _wifi_ipv4(wifi),
                        )
                    )
                    _memory("wifi_ready", collect=True)
                    return True
            except Exception:
                pass
            _sleep(poll_interval_s=0.10)

        last_error = "timeout"
        _log(
            "wifi connect phase=timeout attempt={} elapsed_s={:.1f}".format(
                attempt,
                time.monotonic() - started,
            )
        )
        if attempt < max_attempts:
            _wifi_reset_station(wifi, reason="retry")
            _log(
                "wifi connect phase=retry attempt={} delay_s={:.1f}".format(
                    attempt,
                    retry_delay,
                )
            )
            _sleep(retry_delay)
    _memory("wifi_connect_failed", collect=True)
    _log(
        "wifi connect phase=failed attempts={} errors={}".format(
            max_attempts,
            last_error,
        )
    )
    return False


def _wifi_radio_connect(connect, ssid, password, *, timeout_s):
    try:
        timeout_value = float(timeout_s or 0.0)
    except Exception:
        timeout_value = 0.0
    if timeout_value > 0.0:
        try:
            return connect(ssid, password, timeout=timeout_value)
        except TypeError:
            pass
    return connect(ssid, password)


def _wifi_prepare_station(wifi_module):
    try:
        stop_ap = getattr(wifi_module.radio, "stop_ap", None)
        if callable(stop_ap):
            stop_ap()
    except Exception:
        pass


def _wifi_reset_station(wifi_module, *, reason):
    stopped = False
    started = False
    try:
        disconnect = getattr(wifi_module.radio, "disconnect", None)
        if callable(disconnect):
            disconnect()
    except Exception:
        pass
    try:
        stop_station = getattr(wifi_module.radio, "stop_station", None)
        if callable(stop_station):
            stop_station()
            stopped = True
    except Exception:
        pass
    try:
        start_station = getattr(wifi_module.radio, "start_station", None)
        if callable(start_station):
            start_station()
            started = True
    except Exception:
        pass
    _log(
        "wifi station_reset reason={} stopped={} started={}".format(
            reason,
            1 if stopped else 0,
            1 if started else 0,
        )
    )


def _wifi_ipv4(wifi_module):
    try:
        return str(getattr(wifi_module.radio, "ipv4_address", "") or "")
    except Exception:
        return "unknown"


def _open_mqtt_session(
    runtime_config,
    *,
    timeout_s,
    preflight,
    preconnect_probe,
    preflight_retries,
    preflight_retry_delay_s,
    connect_delay_s,
):
    _memory("mqtt_session_before", collect=True)
    try:
        import socketpool
        import wifi
    except Exception as exc:
        _log(
            "mqtt_session phase=import_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return None, None
    try:
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.core.mqtt_client import (
            build_mqtt_client_adapter,
            connect_mqtt_client,
        )
    except Exception as exc:
        _log(
            "mqtt_session phase=adapter_import_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return None, None
    try:
        pool = socketpool.SocketPool(wifi.radio)
    except MemoryError:
        _memory("mqtt_session_socketpool_memory_error", collect=True)
        return None, None
    except Exception as exc:
        _log(
            "mqtt_session phase=socketpool_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return None, None
    ssl_context = _ssl_context(runtime_config)
    transport = MQTTTransport(
        getattr(runtime_config.mqtt, "preferred_host", ""),
        getattr(runtime_config.mqtt, "port", 1883),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=pool,
        ssl_context=ssl_context,
    )
    _log(
        "mqtt_session phase=adapter phase={} broker={} targets={} errors={}".format(
            getattr(adapter, "phase", ""),
            getattr(adapter, "broker", ""),
            _join_errors(getattr(adapter, "broker_targets", ()) or ()),
            _join_errors(getattr(adapter, "errors", ()) or ()),
        )
    )
    if getattr(adapter, "phase", "") != "ready":
        return None, transport
    if preconnect_probe:
        if not _mqtt_preconnect_probe(
            adapter,
            retries=preflight_retries,
            retry_delay_s=preflight_retry_delay_s,
            connect_delay_s=connect_delay_s,
        ):
            _memory("mqtt_session_preconnect_failed", collect=True)
            return None, transport
    started = time.monotonic()
    result = connect_mqtt_client(adapter, transport, preflight=bool(preflight))
    adapter = result.adapter
    _log(
        (
            "mqtt_session connect phase={} elapsed_s={:.1f} active_broker={} "
            "resolved_ip={} errors={}"
        ).format(
            result.phase,
            time.monotonic() - started,
            getattr(adapter, "active_broker", ""),
            getattr(adapter, "resolved_broker_ip", ""),
            _join_errors(result.errors),
        )
    )
    _memory("mqtt_session_connected", collect=True)
    if result.phase != "connected" or result.errors:
        _close_mqtt_session(adapter, transport, runtime_config)
        return None, transport
    return adapter, transport


def _ssl_context(runtime_config):
    use_tls = bool(getattr(runtime_config.mqtt, "use_tls", False))
    port = int(getattr(runtime_config.mqtt, "port", 1883) or 1883)
    if not use_tls and port != 8883:
        return None
    try:
        import ssl

        return ssl.create_default_context()
    except Exception as exc:
        _log("mqtt_session phase=ssl_error type={} error={}".format(
            type(exc).__name__,
            exc,
        ))
        return None


def _mqtt_preconnect_probe(adapter, *, retries, retry_delay_s, connect_delay_s):
    try:
        from cpynodus_ii.core.mqtt_client import (
            preflight_mqtt_broker_connect,
            preflight_mqtt_broker_tcp,
            raw_mqtt_connect_enabled,
        )
    except Exception as exc:
        _log(
            "mqtt_preconnect phase=import_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return False
    max_attempts = max(1, int(retries or 1))
    broker = (
        str(getattr(adapter, "active_broker", "") or "")
        or str(getattr(adapter, "broker", "") or "")
        or "none"
    )
    tcp_error = ""
    tcp_target = ""
    for index in range(1, max_attempts + 1):
        started = time.monotonic()
        tcp_error, tcp_target = preflight_mqtt_broker_tcp(adapter)
        _log(
            (
                "mqtt_preconnect preflight phase={} broker={} target={} "
                "port={} elapsed_s={:.1f} try={} errors={}"
            ).format(
                "tcp_error" if tcp_error else "tcp_ok",
                broker,
                tcp_target or "none",
                getattr(adapter, "port", 1883),
                time.monotonic() - started,
                index,
                tcp_error or "none",
            )
        )
        if not tcp_error:
            break
        if index >= max_attempts or not _mqtt_error_indicates_socket_progress(
            tcp_error
        ):
            break
        _log(
            "mqtt_preconnect preflight phase=retry try={} delay_s={:.1f}".format(
                index,
                float(retry_delay_s or 0.0),
            )
        )
        _sleep(retry_delay_s)
    if tcp_error:
        _log(
            (
                "mqtt_preconnect connect_probe phase=skipped broker={} "
                "target={} port={} errors={}"
            ).format(
                broker,
                tcp_target or "none",
                getattr(adapter, "port", 1883),
                "mqtt_connect_probe_skipped:tcp_preflight_error",
            )
        )
        return False

    if raw_mqtt_connect_enabled(adapter):
        _log(
            (
                "mqtt_preconnect connect_probe phase=skipped broker={} "
                "target={} port={} reason=raw_connect errors=none"
            ).format(
                broker,
                tcp_target or "none",
                getattr(adapter, "port", 1883),
            )
        )
    else:
        connect_error = ""
        connect_target = tcp_target
        client_id = ""
        connack = -1
        for index in range(1, max_attempts + 1):
            started = time.monotonic()
            (
                connect_error,
                connect_target,
                client_id,
                connack,
            ) = preflight_mqtt_broker_connect(adapter)
            _log(
                (
                    "mqtt_preconnect connect_probe phase={} broker={} "
                    "target={} port={} elapsed_s={:.1f} client_id={} "
                    "connack={} try={} errors={}"
                ).format(
                    "connack_error" if connect_error else "connack_ok",
                    broker,
                    connect_target or "none",
                    getattr(adapter, "port", 1883),
                    time.monotonic() - started,
                    client_id or "none",
                    connack,
                    index,
                    connect_error or "none",
                )
            )
            if not connect_error:
                break
            if index >= max_attempts or not _mqtt_error_indicates_socket_progress(
                connect_error
            ):
                break
            _log(
                (
                    "mqtt_preconnect connect_probe phase=retry try={} "
                    "delay_s={:.1f}"
                ).format(
                    index,
                    float(retry_delay_s or 0.0),
                )
            )
            _sleep(retry_delay_s)
        if connect_error:
            return False

    delay = max(0.0, float(connect_delay_s or 0.0))
    if delay > 0.0:
        _log("mqtt_preconnect connect_delay phase=pre_minimqtt delay_s={:.1f}".format(
            delay,
        ))
        _sleep(delay)
    _memory("mqtt_preconnect_done", collect=True)
    return True


def _mqtt_error_indicates_socket_progress(error):
    text = str(error or "").strip().lower()
    return "einprogress" in text or "errno 119" in text or "socket_progress" in text


def _close_mqtt_session(adapter, transport, runtime_config):
    del runtime_config
    if adapter is None or transport is None:
        return
    _log("mqtt_session phase=close")
    try:
        client = getattr(adapter, "client", None)
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            disconnect()
            _log("mqtt_session phase=disconnected")
    except Exception as exc:
        _log(
            "mqtt_session phase=disconnect_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
    _close_mqtt_client_socket(adapter)
    try:
        transport.mark_disconnected(reason="mqtt_round_done")
    except Exception:
        pass
    _memory("mqtt_session_closed", collect=True)


def _close_mqtt_client_socket(adapter):
    try:
        client = getattr(adapter, "client", None)
    except Exception:
        client = None
    if client is None:
        return
    try:
        sock = getattr(client, "_sock", None)
    except Exception:
        sock = None
    if sock is None:
        return
    close = getattr(sock, "close", None)
    if not callable(close):
        return
    try:
        close()
        _log("mqtt_session socket_close phase=ok")
    except Exception as exc:
        _log(
            "mqtt_session socket_close phase=error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
    try:
        client._sock = None
    except Exception:
        pass


def _sync_mqtt_until_idle(adapter, transport, *, label, timeout_s, poll_interval_s):
    try:
        from cpynodus_ii.core.mqtt_client import sync_transport_to_client
    except Exception as exc:
        _log("mqtt_sync phase=import_error type={} error={}".format(
            type(exc).__name__,
            exc,
        ))
        return adapter, False
    deadline = time.monotonic() + float(timeout_s or 0.0)
    count = 0
    while time.monotonic() <= deadline:
        pending = _mqtt_pending_count(transport)
        if pending <= 0:
            _log("mqtt_sync label={} phase=idle count={}".format(label, count))
            return adapter, True
        started = time.monotonic()
        result = sync_transport_to_client(adapter, transport)
        adapter = result.adapter
        _log(
            (
                "mqtt_sync label={} phase={} op={} elapsed_s={:.1f} "
                "published={} subscribed={} pending={} errors={}"
            ).format(
                label,
                result.phase,
                result.operation or "none",
                time.monotonic() - started,
                result.published_count,
                result.subscribed_count,
                _mqtt_pending_count(transport),
                _join_errors(result.errors),
            )
        )
        _memory("mqtt_sync_{}".format(label), collect=False)
        if result.phase == "error" or result.errors:
            return adapter, False
        if result.phase == "deferred":
            adapter = _poll_mqtt_once(adapter, transport, label=label)[0]
        count += 1
        _sleep(poll_interval_s)
    _log("mqtt_sync label={} phase=timeout".format(label))
    return adapter, False


def _mqtt_pending_count(transport):
    try:
        published = len(getattr(transport, "published_messages", ()) or ())
    except Exception:
        published = 0
    try:
        subscriptions = len(getattr(transport, "subscriptions", ()) or ())
    except Exception:
        subscriptions = 0
    return int(published or 0) + int(subscriptions or 0)


def _drain_mqtt_for_settle(adapter, transport, *, label, duration_s, poll_interval_s):
    deadline = time.monotonic() + float(duration_s or 0.0)
    while time.monotonic() <= deadline:
        adapter, _ok = _poll_mqtt_once(adapter, transport, label=label)
        _drain_mqtt_received(transport, label=label)
        _sleep(poll_interval_s)
    return adapter


def _wait_for_mqtt_message(
    adapter,
    transport,
    topic,
    payload_text,
    *,
    timeout_s,
    poll_interval_s,
):
    deadline = time.monotonic() + float(timeout_s or 0.0)
    while time.monotonic() <= deadline:
        if _has_received_message(transport, topic, payload_text):
            _log("mqtt_round rx phase=matched topic={}".format(topic))
            return adapter, True
        adapter, ok = _poll_mqtt_once(adapter, transport, label="wait_command")
        if not ok:
            return adapter, False
        _sleep(poll_interval_s)
    return adapter, False


def _has_received_message(transport, topic, payload_text):
    expected_topic = str(topic or "")
    expected_payload = str(payload_text or "")
    for message in tuple(getattr(transport, "received_messages", ()) or ()):
        received_topic = str(getattr(message, "topic", "") or "")
        received_payload = str(getattr(message, "payload_text", "") or "")
        if received_topic != expected_topic:
            continue
        if expected_payload and received_payload != expected_payload:
            continue
        return True
    return False


def _first_received_payload(transport, topic, payload_text):
    expected_topic = str(topic or "")
    expected_payload = str(payload_text or "")
    for message in tuple(getattr(transport, "received_messages", ()) or ()):
        received_topic = str(getattr(message, "topic", "") or "")
        received_payload = str(getattr(message, "payload_text", "") or "")
        if received_topic != expected_topic:
            continue
        if expected_payload and received_payload != expected_payload:
            continue
        return received_payload
    return ""


def _payload_message_id(payload_text):
    try:
        payload = json.loads(str(payload_text or ""))
    except Exception:
        return ""
    try:
        return str(payload.get("message_id", "") or "")
    except Exception:
        return ""


def _wait_for_output_echoes(
    adapter,
    transport,
    ack_topic,
    result_topic,
    patch_topic,
    message_id,
    *,
    timeout_s,
    poll_interval_s,
):
    deadline = time.monotonic() + float(timeout_s or 0.0)
    seen_ack = False
    seen_result = False
    seen_patch = False
    while time.monotonic() <= deadline:
        for message in _drain_mqtt_received(transport, label="outputs"):
            topic = str(getattr(message, "topic", "") or "")
            payload = str(getattr(message, "payload_text", "") or "")
            if str(message_id or "") and str(message_id or "") not in payload:
                continue
            if topic == ack_topic:
                seen_ack = True
            elif topic == result_topic:
                seen_result = True
            elif topic == patch_topic:
                seen_patch = True
        if seen_ack and seen_result:
            _log(
                (
                    "mqtt_round outputs phase=matched ack={} result={} "
                    "patch={}"
                ).format(
                    1 if seen_ack else 0,
                    1 if seen_result else 0,
                    1 if seen_patch else 0,
                )
            )
            return adapter, True
        adapter, ok = _poll_mqtt_once(adapter, transport, label="wait_outputs")
        if not ok:
            return adapter, False
        _sleep(poll_interval_s)
    _log(
        "mqtt_round outputs phase=timeout ack={} result={} patch={}".format(
            1 if seen_ack else 0,
            1 if seen_result else 0,
            1 if seen_patch else 0,
        )
    )
    return adapter, False


def _poll_mqtt_once(adapter, transport, *, label):
    try:
        from cpynodus_ii.core.mqtt_client import poll_mqtt_client
    except Exception as exc:
        _log("mqtt_poll label={} phase=import_error type={} error={}".format(
            label,
            type(exc).__name__,
            exc,
        ))
        return adapter, False
    started = time.monotonic()
    result = poll_mqtt_client(adapter, transport)
    adapter = result.adapter
    _log(
        (
            "mqtt_poll label={} phase={} elapsed_s={:.1f} received={} "
            "pending_rx={} errors={}"
        ).format(
            label,
            result.phase,
            time.monotonic() - started,
            result.received_count,
            len(getattr(transport, "received_messages", ()) or ()),
            _join_errors(result.errors),
        )
    )
    if result.phase == "error" or result.errors:
        return adapter, False
    return adapter, True


def _drain_mqtt_received(transport, *, label):
    drain = getattr(transport, "drain_received", None)
    if not callable(drain):
        return ()
    messages = tuple(drain())
    for message in messages:
        _log(
            "mqtt_rx label={} topic={} payload={}".format(
                label,
                getattr(message, "topic", ""),
                getattr(message, "payload_text", ""),
            )
        )
    return messages


def _override_mqtt_target(runtime_config, *, mqtt_host, mqtt_port):
    host = str(mqtt_host or "").strip()
    if host:
        try:
            runtime_config.mqtt.broker_ip = host
        except Exception:
            pass
        try:
            runtime_config.mqtt.broker = host
        except Exception:
            pass
    try:
        port = int(mqtt_port or 0)
    except Exception:
        port = 0
    if port > 0:
        try:
            runtime_config.mqtt.port = port
        except Exception:
            pass


def _sleep(value=None, *, poll_interval_s=None):
    delay = poll_interval_s if poll_interval_s is not None else value
    try:
        time.sleep(float(delay or 0.0))
    except Exception:
        pass


def _open_socket_holds(count, *, connect_host="", connect_port=1883, timeout=1.0):
    requested = max(0, int(count or 0))
    if requested <= 0:
        return ()
    _log(
        (
            "socket_hold phase=start requested={} connect_host={} "
            "connect_port={} timeout={}"
        ).format(
            requested,
            connect_host or "none",
            int(connect_port or 0),
            timeout,
        )
    )
    _memory("socket_hold_before", collect=True)
    try:
        import socketpool
        import wifi
    except Exception as exc:
        _memory("socket_hold_import_error", collect=True)
        _log(
            "socket_hold phase=error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return ()
    try:
        connected = bool(getattr(wifi.radio, "connected", False))
    except Exception:
        connected = False
    _log("socket_hold phase=radio connected={}".format(1 if connected else 0))
    try:
        pool = socketpool.SocketPool(wifi.radio)
    except MemoryError:
        _memory("socket_hold_pool_memory_error", collect=True)
        return ()
    except Exception as exc:
        _memory("socket_hold_pool_error", collect=True)
        _log(
            "socket_hold phase=pool_error type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        return ()

    sockets = []
    for index in range(requested):
        try:
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
            _set_socket_timeout(sock, timeout)
            if str(connect_host or "").strip():
                try:
                    sock.connect((str(connect_host).strip(), int(connect_port or 0)))
                    _log("socket_hold phase=connected index={}".format(index + 1))
                except Exception as exc:
                    _log(
                        (
                            "socket_hold phase=connect_error index={} type={} "
                            "error={}"
                        ).format(
                            index + 1,
                            type(exc).__name__,
                            exc,
                        )
                    )
            sockets.append(sock)
            _memory("socket_hold_open index={}".format(index + 1), collect=False)
        except MemoryError:
            _memory("socket_hold_open_memory_error index={}".format(index + 1))
            break
        except Exception as exc:
            _memory("socket_hold_open_error index={}".format(index + 1))
            _log(
                "socket_hold phase=open_error index={} type={} error={}".format(
                    index + 1,
                    type(exc).__name__,
                    exc,
                )
            )
            break
    _log(
        "socket_hold phase=ready requested={} opened={}".format(
            requested,
            len(sockets),
        )
    )
    _memory("socket_hold_ready", collect=True)
    return tuple(sockets)


def _set_socket_timeout(sock, timeout):
    try:
        value = float(timeout)
    except Exception:
        value = 0.0
    if value <= 0:
        return
    try:
        sock.settimeout(value)
    except Exception:
        pass


def _close_socket_holds(sockets):
    sockets = tuple(sockets or ())
    if not sockets:
        return
    _log("socket_hold phase=close count={}".format(len(sockets)))
    for index, sock in enumerate(sockets):
        try:
            sock.close()
            _log("socket_hold phase=closed index={}".format(index + 1))
        except Exception as exc:
            _log(
                "socket_hold phase=close_error index={} type={} error={}".format(
                    index + 1,
                    type(exc).__name__,
                    exc,
                )
            )
    _memory("socket_hold_closed", collect=True)


def _free_mem():
    try:
        return int(gc.mem_free())
    except Exception:
        return -1


def _memory(label, *, collect=False):
    if collect:
        try:
            gc.collect()
        except Exception:
            pass
    try:
        free_mem = gc.mem_free()
        alloc = gc.mem_alloc()
        _log(
            "memory phase={} collect={} free_mem={} mem_alloc={}".format(
                label,
                1 if collect else 0,
                free_mem,
                alloc,
            )
        )
    except Exception as exc:
        _log("memory phase={} error={}".format(label, exc))


def _join_errors(errors):
    values = tuple(errors or ())
    if not values:
        return "none"
    return ",".join(str(item) for item in values)


def _log(message):
    print("[{:.3f}] {}".format(time.monotonic(), message))
