"""Exercise long-lived MQTT traffic with synthetic or real sensor data.

Copy this file to CIRCUITPY root and call run() from the REPL with the normal
app stopped. The adapter mode calls firmware operations directly from one
loop. The flat comparison mode substitutes shallow packet I/O. No commands
are executed or automatic MCU resets requested. Optional filesystem controls
write only a dedicated diagnostic file. See the
companion mqtt_soak_test.md for capture instructions and comparison limits.
"""

import gc
import time

TEST_VERSION = "2026-10-28.2"
MAX_PACKET_BYTES = 4096
MAX_QUEUE_ITEMS = 32
MAX_INTERLEAVED_PACKETS = 8


class SyntheticSnapshot:
    """Supply representative AVPD values without importing a sensor driver."""

    phase = "ready"
    errors = ()

    def __init__(self, config, metrics):
        self.sensor_id = config.sensor.sensor_id
        self.device = config.sensor.device
        self.metrics = metrics


def run(
    *,
    settings_root=".",
    duration_s=7200,
    mode="adapter",
    base_topic="probe",
    recover=False,
    availability_interval_s=30,
    sensor_interval_s=60,
    gc_interval_s=300,
    preflight_attempts=3,
    metrics=None,
    sensor_mode="synthetic",
    switch_mode="inactive",
    cleanup_mode="adapter",
    fs_activity="none",
    write_interval_s=300,
    write_limit=256,
    write_sync=False,
    write_max_bytes=65536,
):
    """Run a bounded soak and return separate transport and cleanup results.

    base_topic=None uses configured production topics. recover=True permits
    one MQTT rebuild and then one station/radio reset after captured failures.
    A recovered failure still makes transport_ok false. Ctrl-C closes MQTT
    and propagates KeyboardInterrupt. No hard reset or soft reload is issued.
    fs_activity selects no writes, periodic writes, or a write before/after
    each close. cleanup_mode="direct" bypasses MiniMQTT's close manager.
    Inspect write_errors independently, including errors on final cleanup.
    sensor_mode="real" uses configured sensor hardware and firmware processing;
    metrics overrides are synthetic-only. switch_mode="real" starts relay GPIO
    at saved states and reads outputs each availability interval. Commands are
    discarded in every mode; switch snapshots do not verify relay contacts.
    """
    if mode not in ("adapter", "flat"):
        raise ValueError("mode must be adapter or flat")
    if sensor_mode not in ("synthetic", "real"):
        raise ValueError("sensor_mode must be synthetic or real")
    if switch_mode not in ("inactive", "real"):
        raise ValueError("switch_mode must be inactive or real")
    if sensor_mode == "real" and metrics is not None:
        raise ValueError("metrics overrides require synthetic sensor mode")
    if min(duration_s, availability_interval_s, sensor_interval_s, gc_interval_s) <= 0:
        raise ValueError("duration and intervals must be positive")
    if availability_interval_s > 30:
        raise ValueError("availability interval must be <=30 for 60s keepalive")
    if base_topic is not None and not str(base_topic).strip(" /"):
        raise ValueError("base_topic must be nonempty or None")
    if preflight_attempts not in (1, 2, 3):
        raise ValueError("preflight_attempts must be 1, 2, or 3")
    if cleanup_mode not in ("adapter", "direct"):
        raise ValueError("cleanup_mode must be adapter or direct")
    if fs_activity not in ("none", "periodic", "before_close", "after_close"):
        raise ValueError("invalid fs_activity")
    if write_interval_s <= 0 or not 1 <= write_limit <= 256 or write_max_bytes < 256:
        raise ValueError("invalid diagnostic write limits")
    writer = (
        DiagnosticWriter(settings_root, write_limit, write_sync, write_max_bytes)
        if fs_activity != "none"
        else None
    )

    # Import once, before the measured session; never import the old harness.
    from cpynodus_ii import __version__
    from cpynodus_ii.core import mqtt_client as client
    from cpynodus_ii.core.mqtt import MQTTTransport
    from cpynodus_ii.core.network import reconnect_network_stack
    from cpynodus_ii.features.command_intake import (
        subscribe_device_runtime_topics,
        subscribe_switch_runtime_topics,
    )
    from cpynodus_ii.features.publish_cycle import (
        publish_availability_refresh_cycle,
        publish_sensor_cycle,
        publish_startup_cycle,
    )

    started = time.monotonic()
    config, network = _setup(settings_root)
    if sensor_mode == "real" and not config.sensor.present:
        raise ValueError("real acquisition requires a configured sensor")
    if switch_mode == "real" and not config.switch.present:
        raise ValueError("real switch mode requires a configured switch")
    if base_topic is not None:
        config.mqtt.base_topic = str(base_topic).strip(" /")
    snapshot = None
    if sensor_mode == "synthetic":
        snapshot = SyntheticSnapshot(
            config,
            metrics
            if metrics is not None
            else {
                "Temperature": 22.0,
                "Rel-Humidity": 50.0,
                "Humidity": 9.7,
                "Ambient VPD": 1.32,
                "Dew Point": 11.1,
                "Dew Point Deficit": 10.9,
                "DewVPD Risk": 20.0,
                "Pressure": 1013.25,
            },
        )
    if snapshot is not None and metrics is None and config.sensor.device == "co2":
        del snapshot.metrics["Pressure"]
        snapshot.metrics["CO2"] = 600.0
    transport = MQTTTransport(config.mqtt.preferred_host, config.mqtt.port)
    adapter = None
    failures = 0
    cleanup_errors = 0
    operations = 0
    preflight_errors = 0
    generations = 0
    operational = 0
    switch_service = None
    switch_snapshot = {}
    switch_reads = 0
    switch_errors = 0
    switch_cleanup_errors = 0
    sensor_service = None
    sensor_reads = 0
    sensor_skips = 0
    sensor_errors = 0
    sensor_cleanup_errors = 0
    last_failure = "none"
    finished = False
    next_gc = started + gc_interval_s
    last_availability = started
    last_sensor = started
    last_paced_publish = -1.0
    next_write = None
    last_write_count = 0
    stage = "sensor_startup" if sensor_mode == "real" else "connect"
    if switch_mode == "real":
        stage = "switch_startup"
    operation = "connect"
    switch_subscriptions_queued = False
    _log(
        started,
        "start",
        "test={} firmware={} mode={} duration_s={} "
        "broker={} port={} base={} recover={} metadata_qos_threshold={} "
        "ip={} profile={} fs={} ntp=not_attempted".format(
            TEST_VERSION,
            __version__,
            mode,
            duration_s,
            config.mqtt.preferred_host,
            config.mqtt.port,
            config.mqtt.base_topic,
            int(recover),
            client.MQTT_RAW_VERIFY_PUBLISH_BYTES,
            network.ip_address,
            config.active_profile,
            _filesystem_mode(),
        ),
    )
    _memory(started, "initial", transport)
    _log(
        started,
        "controls",
        "sensor={} sensor_id={} switches={} cleanup={} fs_activity={} "
        "write_interval_s={} write_limit={} write_sync={} write_max_bytes={} "
        "sensor_mode={} switch_mode={}".format(
            config.sensor.device,
            config.sensor.sensor_id,
            len(config.switch.channels),
            cleanup_mode,
            fs_activity,
            write_interval_s,
            write_limit,
            int(write_sync),
            write_max_bytes,
            sensor_mode,
            switch_mode,
        ),
    )
    try:
        while time.monotonic() - started < duration_s:
            try:
                if stage == "switch_startup":
                    operation = "switch_startup"
                    switch_service = _start_real_switch(config)
                    if switch_service.phase != "ready":
                        raise OSError("switch_startup:{}".format(switch_service.errors))
                    operation = "switch_read"
                    switch_snapshot = _read_switch(switch_service, started, transport)
                    switch_reads += 1
                    stage = "sensor_startup" if sensor_mode == "real" else "connect"
                if stage == "sensor_startup":
                    operation = "sensor_startup"
                    sensor_service = _start_real_sensor(config)
                    if sensor_service.phase != "ready":
                        raise OSError("sensor_startup:{}".format(sensor_service.errors))
                    operation = "sensor_read"
                    sensor_reads += 1
                    snapshot = _acquire_sensor(
                        sensor_service, config, started, sensor_reads, transport
                    )
                    sensor_skips += int(snapshot.phase != "ready")
                    stage = "connect"
                if stage == "connect":
                    operation = "connect"
                    adapter = client.build_mqtt_client_adapter(
                        config,
                        socket_pool=network.socket_pool,
                        ssl_context=network.ssl_context,
                    )
                    if adapter.phase != "ready":
                        raise OSError("adapter_unavailable:{}".format(adapter.errors))
                    # TCP-only preflight, no raw CONNECT primer or conditioning.
                    stage = "preflight"
                    for attempt in range(1, preflight_attempts + 1):
                        error, target = client.preflight_mqtt_broker_tcp(adapter)
                        preflight_errors += int(bool(error))
                        _log(
                            started,
                            stage,
                            "attempt={} target={} errors={}".format(
                                attempt, target, error or "none"
                            ),
                        )
                        if not error:
                            break
                        if (
                            attempt == preflight_attempts
                            or _failure_kind(OSError(error)) != "socket_progress"
                        ):
                            break
                        _log(
                            started,
                            "preflight_retry",
                            "reason=socket_progress delay_s=0.5",
                        )
                        time.sleep(0.5)
                    if error:
                        raise OSError(error)
                    stage = "connect"
                    result = client.connect_mqtt_client(adapter, transport)
                    adapter = result.adapter
                    if result.phase != "connected" or result.errors:
                        raise OSError("connect:{}".format(result.errors))
                    generations += 1
                    subscribe_device_runtime_topics(transport, config)
                    publish_startup_cycle(
                        transport,
                        config,
                        version=__version__,
                        sensor_snapshot=snapshot,
                        switch_snapshot=switch_snapshot,
                        active_broker=adapter.active_broker,
                        ip_address=network.ip_address,
                        publish_switch_startup=False,
                    )
                    last_paced_publish = -1.0
                    switch_subscriptions_queued = False
                    stage = "startup"
                    _log(started, "connected", "generation={}".format(generations))

                now = time.monotonic()
                if stage == "steady" and fs_activity == "periodic":
                    if next_write is None:
                        next_write = now + write_interval_s
                    if now >= next_write and writer.count < write_limit:
                        if not writer.write(started, "periodic", transport):
                            break
                        next_write = time.monotonic() + write_interval_s
                if now >= next_gc:
                    _memory(started, "before_gc", transport)
                    gc.collect()
                    _memory(started, "after_gc", transport)
                    _network_state(started, network)
                    next_gc = time.monotonic() + gc_interval_s
                if stage == "steady":
                    # Sensor before availability matches the steady-state cycle.
                    if now - last_sensor >= sensor_interval_s:
                        if sensor_mode == "real":
                            operation = "sensor_read"
                            sensor_reads += 1
                            snapshot = _acquire_sensor(
                                sensor_service, config, started, sensor_reads, transport
                            )
                            sensor_skips += int(snapshot.phase != "ready")
                        operation = "queue_sensor"
                        publish_sensor_cycle(transport, config, snapshot)
                        last_sensor = now
                    if now - last_availability >= availability_interval_s:
                        if switch_service is not None:
                            operation = "switch_read"
                            switch_snapshot = _read_switch(
                                switch_service, started, transport
                            )
                            switch_reads += 1
                        operation = "queue_availability"
                        publish_availability_refresh_cycle(transport, config)
                        last_availability = now
                if (
                    max(
                        len(transport.published_messages),
                        len(transport.subscriptions),
                        len(transport.received_messages),
                    )
                    > MAX_QUEUE_ITEMS
                ):
                    raise OSError("queue_limit")

                if transport.published_messages or transport.subscriptions:
                    if stage == "startup" and now - last_paced_publish < 0.35:
                        time.sleep(0.05)
                        continue
                    operations += 1
                    operation = "publish_or_subscribe"
                    op_started = time.monotonic()
                    if mode == "adapter":
                        # Keep this call directly in run(), avoiding harness frames.
                        result = client.sync_transport_to_client(
                            adapter, transport, require_clean_poll_before_subscribe=True
                        )
                        adapter = result.adapter
                        _adapter_packet_detail(result, adapter, client, started)
                        _log(
                            started,
                            "operation",
                            "n={} generation={} "
                            "phase={} op={} topic={} bytes={} retain={} "
                            "published={} subscribed={} elapsed_ms={} errors={}".format(
                                operations,
                                generations,
                                result.phase,
                                result.operation,
                                result.topic,
                                result.payload_bytes,
                                int(result.retain),
                                result.published_count,
                                result.subscribed_count,
                                result.elapsed_ms,
                                result.errors,
                            ),
                        )
                        if result.errors or result.phase == "error":
                            raise OSError(str(result.errors))
                        if stage == "startup" and result.published_count:
                            last_paced_publish = time.monotonic()
                    else:
                        # Same queued payloads and priority selection; different I/O.
                        _flat_step(adapter, transport, client, started, operations)
                        if stage == "startup":
                            last_paced_publish = time.monotonic()
                    if time.monotonic() - op_started >= 5:
                        raise OSError("slow_operation")

                if not transport.published_messages and not transport.subscriptions:
                    if not switch_subscriptions_queued:
                        subscribe_switch_runtime_topics(transport, config)
                        switch_subscriptions_queued = True
                    elif stage == "startup":
                        operational += 1
                        stage = "steady"
                        last_sensor = time.monotonic()
                        last_availability = last_sensor
                        _log(
                            started, "operational", "generation={}".format(generations)
                        )

                # Poll without dispatching commands. Idle timeouts are normal;
                # EOF is a failure in flat mode (the adapter's policy is unchanged).
                if mode == "adapter":
                    operation = "poll"
                    result = client.poll_mqtt_client(adapter, transport)
                    if result.errors or result.phase == "error":
                        raise OSError(str(result.errors))
                    if transport.received_messages:
                        _log(
                            started,
                            "inbound_discard",
                            "count={}".format(len(transport.received_messages)),
                        )
                        transport.received_messages.clear()
                else:
                    operation = "poll"
                    packet = _read_packet(adapter.client._sock, idle=True)
                    if packet is not None:
                        _discard_packet(adapter.client._sock, packet, started)
                if writer is not None and writer.count != last_write_count:
                    _log(
                        started,
                        "post_write_cycle",
                        "write_n={} pub={} sub={} local_only=1".format(
                            writer.count,
                            len(transport.published_messages),
                            len(transport.subscriptions),
                        ),
                    )
                    last_write_count = writer.count
                time.sleep(0.1)
            except Exception as exc:
                sensor_failure = operation in ("sensor_startup", "sensor_read")
                switch_failure = operation in ("switch_startup", "switch_read")
                if switch_failure:
                    switch_errors += 1
                elif sensor_failure:
                    sensor_errors += 1
                else:
                    failures += 1
                last_failure = _failure_kind(exc)
                if switch_failure and last_failure != "stack_limit":
                    last_failure = "switch_error"
                if sensor_failure and last_failure != "stack_limit":
                    last_failure = "sensor_error"
                _log(
                    started,
                    "failure",
                    "n={} stage={} operation={} kind={} type={} error={}".format(
                        operations, stage, operation, last_failure,
                        type(exc).__name__, exc
                    ),
                )
                # Capture BEFORE cleanup or GC can alter the failure evidence.
                _memory(started, "failure_before_cleanup", transport)
                _network_state(started, network)
                cleanup_errors += _finish_connection(
                    adapter, transport, client, started, cleanup_mode,
                    writer if last_failure != "stack_limit" else None, fs_activity,
                )
                adapter = None
                if (
                    sensor_failure or switch_failure or not recover or failures > 2
                    or last_failure == "stack_limit"
                    or writer is not None and writer.errors
                ):
                    break
                gc.collect()
                if failures == 2:
                    _log(started, "recovery", "action=station_reset radio_cycle=1")
                    network = reconnect_network_stack(
                        config,
                        network,
                        rebuild_socket_artifacts=True,
                        reset_station=True,
                        cycle_radio=True,
                        force_station_reset=True,
                        mdns_mode="off",
                    )
                    if network.phase != "ready":
                        _log(started, "recovery_failed", str(network.errors))
                        break
                else:
                    _log(started, "recovery", "action=mqtt_rebuild")
                # Clean session, bounded queues; reuse the latest sensor snapshot.
                transport = MQTTTransport(config.mqtt.preferred_host, config.mqtt.port)
                stage = "connect"
                time.sleep(1)
        else:
            finished = True
    except KeyboardInterrupt:
        _log(started, "interrupted", "operator=1")
        raise
    finally:
        try:
            cleanup_errors += _finish_connection(
                adapter, transport, client, started, cleanup_mode, writer, fs_activity,
            )
        finally:
            if switch_service is not None:
                from cpynodus_ii.features.switch_service import stop_switch_service

                try:
                    stop_switch_service(switch_service)
                except Exception as exc:
                    switch_cleanup_errors += 1
                    _log(started, "switch_cleanup_error", str(exc))
            if sensor_service is not None:
                from cpynodus_ii.features.sensor_service import stop_sensor_service

                try:
                    stop_sensor_service(sensor_service)
                except Exception as exc:
                    sensor_cleanup_errors += 1
                    _log(started, "sensor_cleanup_error", str(exc))
    summary = {
        "completed": finished,
        "transport_ok": finished and failures == 0 and operational > 0,
        "cleanup_ok": cleanup_errors == 0,
        "failures": failures,
        "cleanup_errors": cleanup_errors,
        "operations": operations,
        "preflight_errors": preflight_errors,
        "generations": generations,
        "operational_generations": operational,
        "last_failure": last_failure,
        "elapsed_s": round(time.monotonic() - started, 1),
        "write_count": writer.count if writer is not None else 0,
        "write_errors": writer.errors if writer is not None else 0,
        "cleanup_mode": cleanup_mode,
        "fs_activity": fs_activity,
        "switch_mode": switch_mode,
        "switch_reads": switch_reads,
        "switch_errors": switch_errors,
        "switch_cleanup_errors": switch_cleanup_errors,
        "sensor_mode": sensor_mode,
        "sensor_reads": sensor_reads,
        "sensor_skips": sensor_skips,
        "sensor_errors": sensor_errors,
        "sensor_cleanup_errors": sensor_cleanup_errors,
    }
    _log(started, "summary", str(summary))
    return summary


def _start_real_switch(config):
    """Build the firmware switch stack and apply configured startup states."""
    from cpynodus_ii.features.switch import plan_switch_initialization
    from cpynodus_ii.features.switch_runtime import build_switch_runtime
    from cpynodus_ii.features.switch_service import (
        start_switch_service,
        stop_switch_service,
    )
    from cpynodus_ii.hardware.switch_adapter import bind_switch_hardware

    runtime = build_switch_runtime(plan_switch_initialization(config))
    hardware = bind_switch_hardware(runtime)
    try:
        return start_switch_service(runtime, hardware)
    except BaseException:
        stop_switch_service(hardware)
        raise


def _read_switch(service, started, transport):
    """Measure firmware output snapshots without applying further states."""
    from cpynodus_ii.features.switch_service import snapshot_switch_states

    _memory(started, "before_switch", transport)
    began = time.monotonic()
    snapshot = snapshot_switch_states(service)
    _log(
        started, "switch_snapshot",
        "elapsed_ms={} channels={} states={}".format(
            int((time.monotonic() - began) * 1000), len(snapshot), snapshot
        ),
    )
    _memory(started, "after_switch", transport)
    if len(snapshot) != service.channel_count or any(
        item["phase"] != "ready" or item["state"] is None
        for item in snapshot.values()
    ):
        raise OSError("switch_snapshot_not_ready")
    return snapshot


def _start_real_sensor(config):
    """Build only the firmware sensor stack without importing the main app."""
    from cpynodus_ii.features.sensor import plan_sensor_initialization
    from cpynodus_ii.features.sensor_runtime import build_sensor_runtime
    from cpynodus_ii.features.sensor_service import (
        start_sensor_service,
        stop_sensor_service,
    )
    from cpynodus_ii.hardware.sensor_adapter import bind_sensor_hardware

    plan = plan_sensor_initialization(config)
    runtime = build_sensor_runtime(plan, config)
    hardware = bind_sensor_hardware(runtime, config)
    try:
        return start_sensor_service(runtime, hardware, config)
    except BaseException:
        # The service has not been returned to run() to own cleanup yet.
        stop_sensor_service(hardware)
        raise


def _acquire_sensor(service, config, started, number, transport):
    """Measure real acquisition and derivation without forcing collection."""
    from cpynodus_ii.features.sensor_service import read_sensor_snapshot

    _memory(started, "before_sensor", transport)
    _log(started, "sensor_begin", "n={}".format(number))
    before = time.monotonic()
    snapshot = read_sensor_snapshot(service, config)
    _log(
        started,
        "sensor_end",
        "n={} phase={} metrics={} elapsed_ms={} errors={}".format(
            number, snapshot.phase, len(snapshot.metrics),
            int((time.monotonic() - before) * 1000), snapshot.errors,
        ),
    )
    _memory(started, "after_sensor", transport)
    if snapshot.phase not in ("ready", "waiting"):
        raise OSError("sensor_acquisition:{}".format(snapshot.errors))
    return snapshot


class DiagnosticWriter:
    """Write bounded recovery-style records only to a fresh apparatus log."""

    def __init__(self, root, limit, sync, max_bytes):
        import os

        if _filesystem_mode() != "RWFS":
            raise ValueError("diagnostic writes require confirmed RWFS")
        self.path = str(root).rstrip("/") + "/_mqtt_soak_test.log"
        for suffix in ("", ".tmp", ".bak"):
            try:
                os.stat(self.path + suffix)
            except OSError as exc:
                if not exc.args or exc.args[0] != 2:
                    raise
            else:
                raise ValueError("remove prior apparatus log first: " + self.path)
        from cpynodus_ii.core.recovery_log import append_recovery_event

        self.append = append_recovery_event
        self.limit = limit
        self.sync = sync
        self.max_bytes = max_bytes
        self.count = 0
        self.errors = 0

    def write(self, started, phase, transport):
        """Log write timing and retain errors separately from transport faults."""
        import os

        if self.count >= self.limit or self.errors:
            return not self.errors
        self.count += 1
        before = time.monotonic()
        _memory(started, "before_write", transport)
        _log(started, "write_begin", "n={} phase={}".format(self.count, phase))
        try:
            ok = self.append(
                "mqtt_soak",
                "n={} phase={} elapsed_s={:.3f}".format(
                    self.count, phase, before - started
                ),
                path=self.path,
                device_id="apparatus",
                max_bytes=self.max_bytes,
            )
            if not ok:
                raise OSError("diagnostic_append_failed")
            if os.stat(self.path)[6] > self.max_bytes:
                raise OSError("diagnostic_trim_failed")
            if self.sync:
                os.sync()
        except Exception as exc:
            self.errors += 1
            _log(started, "write_error", "n={} error={}".format(self.count, exc))
        _log(
            started, "write_end", "n={} elapsed_ms={} errors={}".format(
                self.count, int((time.monotonic() - before) * 1000), self.errors
            ),
        )
        _memory(started, "after_write", transport)
        return self.errors == 0


def _finish_connection(adapter, transport, client, started, mode, writer, activity):
    if adapter is None:
        return 0
    try:
        if writer is not None and activity == "before_close":
            writer.write(started, "before_close", transport)
    finally:
        # Ctrl-C during the diagnostic write must still attempt socket cleanup.
        if mode == "adapter":
            errors = _close(adapter, transport, client, started)
        else:
            errors = 0
            sock = getattr(adapter.client, "_sock", None)
            try:
                if sock is not None:
                    sock.close()
                adapter.client._sock = None
                adapter.client._is_connected = False
            except Exception as exc:
                errors = 1
                _log(started, "direct_close_error", str(exc))
            transport.mark_disconnected(reason="apparatus_direct_close")
            _log(started, "cleanup", "mode=direct errors={}".format(errors))
    if writer is not None and activity == "after_close":
        writer.write(started, "after_close", transport)
    return errors


def _setup(settings_root):
    from cpynodus_ii.core.network import build_network_stack
    from cpynodus_ii.core.settings import Settings

    config = Settings.from_directory(settings_root).runtime_config()
    if config.ap_mode or not config.mqtt_enabled:
        raise ValueError("configure a station MQTT profile before testing")
    if config.active_profile == "homeassistant":
        raise ValueError("use sensorius or weewx; HA discovery is outside this probe")
    if config.mqtt.use_tls or config.mqtt.port == 8883:
        raise ValueError("this comparison requires plain MQTT")
    if not config.mqtt.connection_targets:
        raise ValueError("configure MQTT.BROKER_IP; this probe does not persist DNS")
    network = build_network_stack(config, preconnect_scan=True, mdns_mode="off")
    if network.phase != "ready":
        raise OSError("network_unavailable:{}".format(network.errors))
    return config, network


def _flat_step(adapter, transport, client, started, operation):
    sock = adapter.client._sock
    if transport.subscriptions:
        index = client._next_startup_priority_publish_index(adapter, transport)
    else:
        index = client._next_publish_index(adapter, transport)
    packet_id = 0
    if index is not None:
        message = transport.published_messages[index]
        topic = message.topic
        startup = client._is_retained_startup_meta_publish(message)
        payload = client._serialize_payload(message.payload)
        size = client._mqtt_publish_packet_size(topic, payload)
        qos = int(startup and 0 < client.MQTT_RAW_VERIFY_PUBLISH_BYTES < size)
        packet_id = client._next_mqtt_packet_id(adapter.client) if qos else 0
        packet = client._mqtt_publish_packet(
            topic, payload, message.retain, qos=qos, packet_id=packet_id
        )
        _log(
            started,
            "operation",
            "n={} op=publish topic={} bytes={} qos={} retain={} packet_id={}".format(
                operation, topic, len(packet), qos, int(message.retain), packet_id
            ),
        )
        _send(sock, packet, started, topic, chunked=startup)
        if qos:
            _wait_ack(sock, 4, packet_id, started)
        del transport.published_messages[index]
        if startup:
            gc.collect()
    elif transport.subscriptions:
        topic = transport.subscriptions[0]
        packet_id = client._next_mqtt_packet_id(adapter.client)
        packet = client._mqtt_qos0_subscribe_packet(topic, packet_id)
        _log(
            started,
            "operation",
            "n={} op=subscribe topic={} bytes={} packet_id={}".format(
                operation, topic, len(packet), packet_id
            ),
        )
        _send(sock, packet, started, topic)
        _wait_ack(sock, 9, packet_id, started)
        del transport.subscriptions[0]


def _send(sock, packet, started, topic, *, chunked=False):
    offset = 0
    chunk = 0
    while offset < len(packet):
        end = min(offset + 256, len(packet)) if chunked else len(packet)
        data = memoryview(packet)[offset:end]
        before = time.monotonic()
        chunk += 1
        try:
            count = sock.send(data)
        except Exception as exc:
            _log(
                started,
                "send",
                "topic={} chunk={} offset={} requested={} "
                "returned=-1 elapsed_ms={} error={}".format(
                    topic,
                    chunk,
                    offset,
                    len(data),
                    int((time.monotonic() - before) * 1000),
                    exc,
                ),
            )
            raise
        elapsed = time.monotonic() - before
        _log(
            started,
            "send",
            "topic={} packet_bytes={} chunk={} offset={} "
            "requested={} returned={} elapsed_ms={}".format(
                topic, len(packet), chunk, offset, len(data), count, int(elapsed * 1000)
            ),
        )
        if elapsed >= 5:
            raise OSError("slow_send")
        if count is None or count <= 0 or count > len(data):
            raise OSError("invalid_send_count:{}".format(count))
        offset += count


def _read_packet(sock, *, idle=False, deadline=None):
    # Two shallow helpers; do not use the deep production ACK-reader chain.
    if deadline is None:
        deadline = time.monotonic() + 1.0
    header = _read_exact(sock, 1, idle=idle, deadline=deadline)
    if header is None:
        return None
    remaining = 0
    multiplier = 1
    for _ in range(4):
        encoded = _read_exact(sock, 1, deadline=deadline)[0]
        remaining += (encoded & 127) * multiplier
        if remaining > MAX_PACKET_BYTES:
            raise OSError("packet_size_limit:{}".format(remaining))
        if not encoded & 128:
            return header[0], _read_exact(sock, remaining, deadline=deadline)
        multiplier *= 128
    raise OSError("invalid_remaining_length")


def _read_exact(sock, size, *, idle=False, deadline=None):
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        if deadline is not None and time.monotonic() >= deadline:
            raise OSError("receive_deadline_timeout")
        try:
            count = sock.recv_into(view[offset:], size - offset)
        except OSError as exc:
            if idle and offset == 0 and _idle_timeout(exc):
                return None
            raise
        if not count:
            raise OSError("socket_eof")
        offset += count
    return data


def _wait_ack(sock, kind, packet_id, started):
    before = time.monotonic()
    for interleaved in range(MAX_INTERLEAVED_PACKETS + 1):
        packet = _read_packet(sock, deadline=before + 1.0)
        header, payload = packet
        if header >> 4 == kind:
            if header != kind << 4:
                raise OSError("ack_header")
            expected_size = 2 if kind == 4 else 3
            if len(payload) != expected_size:
                raise OSError("ack_size")
            received = (payload[0] << 8) | payload[1]
            if received != packet_id:
                raise OSError("ack_id:{}:{}".format(received, packet_id))
            if kind == 9 and payload[2] > 2:
                raise OSError("subscription_rejected")
            _log(
                started,
                "ack",
                "kind={} packet_id={} elapsed_ms={}".format(
                    kind, packet_id, int((time.monotonic() - before) * 1000)
                ),
            )
            return
        if interleaved == MAX_INTERLEAVED_PACKETS:
            raise OSError("ack_interleaved_limit")
        _discard_packet(sock, packet, started)
    raise OSError("ack_interleaved_limit")


def _discard_packet(sock, packet, started):
    header, payload = packet
    kind = header >> 4
    if kind == 3:
        if len(payload) < 2:
            raise OSError("short_publish")
        offset = 2 + (payload[0] << 8) + payload[1]
        if offset > len(payload):
            raise OSError("short_publish_topic")
        qos = (header >> 1) & 3
        if qos == 1:
            if offset + 2 > len(payload):
                raise OSError("short_publish_id")
            packet_id = (payload[offset] << 8) | payload[offset + 1]
            _send(
                sock,
                bytes((0x40, 2, packet_id >> 8, packet_id & 255)),
                started,
                "inbound_puback",
            )
        elif qos:
            raise OSError("unsupported_inbound_qos")
        _log(started, "inbound_discard", "bytes={} qos={}".format(len(payload), qos))
    elif kind != 13:
        raise OSError("unexpected_packet:{}".format(kind))


def _idle_timeout(exc):
    return bool(exc.args and exc.args[0] in (11, 116)) or any(
        word in str(exc).lower() for word in ("timed out", "etimedout", "timeout")
    )


def _failure_kind(exc):
    text = str(exc).lower()
    if "ebadf" in text or "bad file descriptor" in text or (
        exc.args and exc.args[0] == 9
    ):
        return "bad_descriptor"
    if "pystack" in text:
        return "stack_limit"
    if (
        "einprogress" in text
        or "errno 119" in text
        or "operation now in progress" in text
        or exc.args
        and exc.args[0] == 119
    ):
        return "socket_progress"
    if "slow" in text:
        return "send_stall"
    if (
        "enomem" in text
        or "errno 12" in text
        or isinstance(exc, MemoryError)
        or exc.args
        and exc.args[0] == 12
    ):
        return "allocation"
    if "socket_eof" in text:
        return "peer_closed"
    if _idle_timeout(exc):
        return "timeout"
    return "operation_error"


def _close(adapter, transport, client, started):
    if adapter is None:
        return 0
    try:
        result = client.close_mqtt_client(adapter, transport)
        _log(started, "cleanup", "errors={}".format(result.errors))
        return len(result.errors)
    except Exception as exc:
        _log(started, "cleanup", "error={}".format(exc))
        return 1


def _memory(started, phase, transport):
    free = gc.mem_free() if hasattr(gc, "mem_free") else -1
    allocated = gc.mem_alloc() if hasattr(gc, "mem_alloc") else -1
    _log(
        started,
        "memory",
        "phase={} free={} allocated={} pub={} sub={} rx={}".format(
            phase,
            free,
            allocated,
            len(transport.published_messages),
            len(transport.subscriptions),
            len(transport.received_messages),
        ),
    )


def _filesystem_mode():
    try:
        import storage

        return "ROFS" if storage.getmount("/").readonly else "RWFS"
    except (ImportError, AttributeError, OSError):
        return "unknown"


def _network_state(started, network):
    # Diagnostics must not replace the original transport exception.
    try:
        radio = getattr(network, "wifi_radio", None)
        connected = getattr(radio, "connected", "unknown")
        address = getattr(radio, "ipv4_address", network.ip_address)
    except Exception as exc:
        _log(started, "network", "diagnostic_error={}".format(exc))
        return
    _log(
        started,
        "network",
        "phase={} wifi_connected={} ipv4={}".format(
            network.phase,
            connected,
            address,
        ),
    )


def _adapter_packet_detail(result, adapter, client, started):
    if result.operation != "publish" or result.payload_bytes < 0:
        return
    remaining = 2 + len(result.topic.encode("utf-8")) + result.payload_bytes
    size = 1 + len(client._mqtt_remaining_length(remaining)) + remaining
    meta = result.retain and (
        result.topic.endswith("/meta")
        or result.topic.endswith("/meta/switch")
        or result.topic.endswith("/meta/config")
    )
    qos = int(meta and 0 < client.MQTT_RAW_VERIFY_PUBLISH_BYTES < size)
    if qos:
        remaining += 2
        size = 1 + len(client._mqtt_remaining_length(remaining)) + remaining
    _log(
        started,
        "adapter_packet",
        "topic={} packet_bytes_expected={} qos_expected={} packet_id={}".format(
            result.topic,
            size,
            qos,
            getattr(adapter.client, "_cpynodus_packet_id", 0) if qos else 0,
        ),
    )


def _log(started, event, detail):
    print(
        "mqtt_soak elapsed_s={:.3f} event={} {}".format(
            time.monotonic() - started, event, detail
        )
    )
