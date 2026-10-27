"""Verify the soak apparatus with bounded simulated MQTT connections.

These tests exercise real firmware serialization and adapter operations with
fake sockets and time. They do not establish CircuitPython stack headroom or
broker delivery on hardware.
"""

from types import SimpleNamespace

import pytest

from cpynodus_ii.core import mqtt_client
from cpynodus_ii.core.config import (
    DetectedSensor,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from testApparatus import mqtt_soak_test as soak


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Socket:
    def __init__(self, clock, incoming=b"", pool=None):
        self.clock = clock
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.pool = pool
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.address = address

    def close(self):
        self.closed = True

    def send(self, data):
        self.clock.sleep(0.001)
        self.outgoing.extend(data)
        if self.pool is not None:
            while len(self.outgoing) >= 2:
                remaining = 0
                multiplier = 1
                offset = 1
                while offset < len(self.outgoing):
                    value = self.outgoing[offset]
                    remaining += (value & 127) * multiplier
                    offset += 1
                    if value < 128:
                        break
                    multiplier *= 128
                else:
                    break
                if len(self.outgoing) < offset + remaining:
                    break
                header = self.outgoing[0]
                body = bytes(self.outgoing[offset : offset + remaining])
                del self.outgoing[: offset + remaining]
                kind = header >> 4
                if kind == 1:
                    self.incoming.extend(b"\x20\x02\x00\x00")
                elif kind == 8:
                    self.pool.subscriptions.append(body[4:-1].decode())
                    self.incoming.extend(b"\x90\x03" + body[:2] + b"\x00")
                elif kind == 3:
                    topic_size = (body[0] << 8) | body[1]
                    topic = body[2 : 2 + topic_size].decode()
                    payload_offset = 2 + topic_size
                    qos = (header >> 1) & 3
                    if qos:
                        packet_id = body[payload_offset : payload_offset + 2]
                        payload_offset += 2
                        self.incoming.extend(b"\x40\x02" + packet_id)
                    self.pool.publications.append(
                        (self.clock.now, topic, qos, body[payload_offset:])
                    )
                    if (
                        self.pool.stalls
                        and self.clock.now >= self.pool.stall_after
                        and topic.endswith("/status/heartbeat")
                    ):
                        self.pool.stalls -= 1
                        self.clock.sleep(9.97)
        return len(data)

    def recv_into(self, buffer, size):
        if not self.incoming:
            self.clock.sleep(1)
            raise OSError(116)
        count = min(size, len(self.incoming))
        buffer[:count] = self.incoming[:count]
        del self.incoming[:count]
        return count


class Pool:
    def __init__(self, clock, stalls=0):
        self.clock = clock
        self.stalls = stalls
        self.sockets = []
        self.publications = []
        self.subscriptions = []
        self.stall_after = 0

    def socket(self):
        sock = Socket(self.clock, pool=self)
        self.sockets.append(sock)
        return sock


class Client:
    def __init__(self, broker, **kwargs):
        self._sock = None
        self._socket_timeout = kwargs.get("socket_timeout", 3)

    def disconnect(self):
        # Model the actual unregistered-raw-socket ownership error.
        raise RuntimeError("Socket not managed")

    def _close_socket(self):
        raise RuntimeError("Socket not managed")


@pytest.fixture
def environment(monkeypatch):
    clock = Clock()
    pool = Pool(clock)
    config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(ssid="test", hostname="avpd-test"),
        mqtt=MQTTConfig(broker_ip="10.0.0.241"),
        sensor=DetectedSensor(sensor_id="avpd-test", device="avpd", family="i2c"),
        switch=SwitchConfig(
            present=True,
            device_id="switch-test",
            channels=(SwitchChannelConfig(key="S1", channel_id="S1-test"),),
        ),
    )
    network = SimpleNamespace(
        phase="ready", socket_pool=pool, ssl_context=None, ip_address="10.0.0.225"
    )
    monkeypatch.setattr(soak, "time", clock)
    monkeypatch.setattr(mqtt_client, "time", clock)
    monkeypatch.setattr(soak, "_setup", lambda root: (config, network))
    original = mqtt_client.build_mqtt_client_adapter

    def build(config, **kwargs):
        return original(config, modules={"mqtt_cls": Client}, **kwargs)

    monkeypatch.setattr(mqtt_client, "build_mqtt_client_adapter", build)
    return SimpleNamespace(clock=clock, pool=pool, config=config, network=network)


@pytest.mark.parametrize("mode", ["adapter", "flat"])
def test_soak_runs_repeated_cycles_and_separates_cleanup(environment, mode):
    result = soak.run(duration_s=7200, mode=mode, gc_interval_s=300)
    assert result["transport_ok"] is True
    assert result["completed"] is True
    assert result["cleanup_ok"] is False
    assert result["cleanup_errors"] == 2
    assert result["operational_generations"] == 1
    assert all(sock.closed for sock in environment.pool.sockets)
    pubs = environment.pool.publications
    assert all(topic.startswith("probe/") for _, topic, _, _ in pubs)
    assert sum(topic.endswith("/status/heartbeat") for _, topic, _, _ in pubs) >= 200
    assert sum(topic.endswith("/data") for _, topic, _, _ in pubs) >= 100
    assert len(environment.pool.subscriptions) == 5
    assert any(topic.endswith("/meta/config") and qos == 1 for _, topic, qos, _ in pubs)
    # No retained command handlers or real hardware were invoked.


@pytest.mark.parametrize("mode", ["adapter", "flat"])
def test_first_slow_send_stops_without_reset(environment, mode, capsys):
    environment.pool.stalls = 1
    result = soak.run(duration_s=130, mode=mode)
    assert result["completed"] is False
    assert result["transport_ok"] is False
    assert result["failures"] == 1
    assert result["last_failure"] == "send_stall"
    assert result["generations"] == 1
    log = capsys.readouterr().out
    assert log.index("failure_before_cleanup") < log.index("event=cleanup")
    assert "event=recovery" not in log


@pytest.mark.parametrize("mode", ["adapter", "flat"])
def test_late_heartbeat_stall_is_detected_after_operational_soak(environment, mode):
    environment.pool.stalls = 1
    environment.pool.stall_after = 3360
    result = soak.run(duration_s=4000, mode=mode)
    assert result["operational_generations"] == 1
    assert result["last_failure"] == "send_stall"
    assert result["transport_ok"] is False
    assert 3360 < result["elapsed_s"] < 3450
    assert environment.pool.publications[-1][1].endswith("/status/heartbeat")


def test_recovery_is_bounded_and_cannot_turn_failures_into_pass(
    environment, monkeypatch
):
    from cpynodus_ii.core import network as network_module

    resets = []
    environment.pool.stalls = 3

    def reset(config, network, **kwargs):
        resets.append(kwargs)
        return network

    monkeypatch.setattr(network_module, "reconnect_network_stack", reset)
    result = soak.run(duration_s=130, mode="flat", recover=True)
    assert result["failures"] == 3
    assert result["transport_ok"] is False
    assert result["generations"] == 3
    assert len(resets) == 1
    assert resets[0]["cycle_radio"] is True


def test_stack_limit_stops_even_when_recovery_requested(environment, monkeypatch):
    def exhaust(*args, **kwargs):
        raise RuntimeError("pystack exhausted")

    monkeypatch.setattr(mqtt_client, "sync_transport_to_client", exhaust)
    result = soak.run(duration_s=130, recover=True)
    assert result["last_failure"] == "stack_limit"
    assert result["generations"] == 1


def test_recovered_session_rebuilds_every_subscription_and_continues(environment):
    environment.pool.stalls = 1
    result = soak.run(duration_s=130, mode="flat", recover=True)
    assert result["completed"] is True
    assert result["transport_ok"] is False
    assert result["failures"] == 1
    assert result["generations"] == 2
    assert result["operational_generations"] == 1
    assert len(environment.pool.subscriptions) == 5
    assert (
        sum(
            topic.endswith("/status/heartbeat")
            for _, topic, _, _ in environment.pool.publications
        )
        >= 4
    )


def test_production_namespace_requires_explicit_none(environment):
    soak.run(duration_s=10, mode="flat", base_topic=None)
    assert environment.pool.publications
    assert all(
        topic.startswith("nodus/") for _, topic, _, _ in environment.pool.publications
    )


@pytest.mark.parametrize(
    "errors, attempts, successful",
    [
        (["[Errno 119] EINPROGRESS", ""], 3, True),
        (["[Errno 119] EINPROGRESS"] * 3, 3, False),
        (["[Errno 119] EINPROGRESS"], 1, False),
        (["[Errno 12] ENOMEM"], 3, False),
    ],
)
def test_preflight_retries_only_socket_progress_with_bounded_attempts(
    environment, monkeypatch, errors, attempts, successful
):
    observed = []

    def preflight(adapter):
        error = errors[len(observed)]
        observed.append(environment.clock.now)
        return error, "10.0.0.241"

    monkeypatch.setattr(mqtt_client, "preflight_mqtt_broker_tcp", preflight)
    result = soak.run(duration_s=130, preflight_attempts=attempts)
    assert result["transport_ok"] is successful
    assert result["preflight_errors"] == sum(bool(error) for error in errors)
    assert len(observed) == len(errors)
    if len(observed) > 1:
        assert observed[1] - observed[0] == pytest.approx(0.5)
    if not successful:
        assert result["generations"] == 0
        assert result["operations"] == 0
    if successful:
        assert result["failures"] == 0


def test_partial_writes_preserve_exact_bytes(monkeypatch):
    clock = Clock()
    sent = bytearray()

    class Partial:
        def send(self, data):
            count = min(3, len(data))
            sent.extend(data[:count])
            return count

    monkeypatch.setattr(soak, "time", clock)
    soak._send(Partial(), b"abcdefghij", 0, "test", chunked=True)
    assert sent == b"abcdefghij"


def test_idle_timeout_is_not_eof(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(soak, "time", clock)
    assert soak._read_packet(Socket(clock), idle=True) is None

    class Closed:
        def recv_into(self, buffer, size):
            return 0

    with pytest.raises(OSError, match="socket_eof"):
        soak._read_packet(Closed(), idle=True)


@pytest.mark.parametrize(
    "wire, error",
    [
        (b"\x40\x02\x00\x02", "ack_id"),
        (b"\x40\x01\x00", "ack_size"),
        (b"\x90\x03\x00\x01\x80", "subscription_rejected"),
        (b"\x30\xff\xff\x7f", "packet_size_limit"),
    ],
)
def test_invalid_ack_or_packet_is_a_failure(monkeypatch, wire, error):
    clock = Clock()
    monkeypatch.setattr(soak, "time", clock)
    kind = 9 if wire[0] == 0x90 else 4
    with pytest.raises(OSError, match=error):
        soak._wait_ack(Socket(clock, wire), kind, 1, 0)


def test_interleaved_publish_is_acknowledged_without_dispatch(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(soak, "time", clock)
    # QoS1 PUBLISH topic x, packet ID 7, payload ON; then our PUBACK ID 1.
    sock = Socket(clock, b"\x32\x07\x00\x01x\x00\x07ON\x40\x02\x00\x01")
    soak._wait_ack(sock, 4, 1, 0)
    assert sock.outgoing == b"\x40\x02\x00\x07"


def test_ack_wait_has_interleaved_bound(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(soak, "time", clock)
    sock = Socket(clock, b"\xd0\x00" * 10)
    with pytest.raises(OSError, match="ack_interleaved_limit"):
        soak._wait_ack(sock, 4, 1, 0)


def test_partial_packet_deadline_is_not_an_idle_poll(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(soak, "time", clock)

    class SlowSocket:
        def recv_into(self, buffer, size):
            clock.sleep(0.6)
            buffer[0] = 0x40
            return 1

    with pytest.raises(OSError, match="receive_deadline_timeout"):
        soak._read_packet(SlowSocket(), idle=True)


@pytest.mark.parametrize(
    "error, classification",
    [
        (OSError(12), "allocation"),
        (OSError(116), "timeout"),
        (OSError(119), "socket_progress"),
        (OSError("[Errno 119] EINPROGRESS"), "socket_progress"),
        (RuntimeError("pystack exhausted"), "stack_limit"),
        (OSError("socket_eof"), "peer_closed"),
        (RuntimeError("Socket not managed"), "operation_error"),
    ],
)
def test_failure_classification_does_not_infer_corruption(error, classification):
    assert soak._failure_kind(error) == classification


def test_ctrl_c_closes_socket_and_does_not_become_a_pass(environment, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mqtt_client, "sync_transport_to_client", interrupt)
    with pytest.raises(KeyboardInterrupt):
        soak.run(duration_s=130)
    assert all(sock.closed for sock in environment.pool.sockets)


@pytest.fixture
def writable_probe(environment, monkeypatch, tmp_path):
    monkeypatch.setattr(soak, "_filesystem_mode", lambda: "RWFS")
    return tmp_path


@pytest.mark.parametrize("activity", ["before_close", "after_close"])
@pytest.mark.parametrize("stall", [False, True])
def test_diagnostic_write_order_preserves_first_failure(
    environment, writable_probe, capsys, activity, stall
):
    environment.pool.stalls = int(stall)
    result = soak.run(
        duration_s=130, settings_root=str(writable_probe), fs_activity=activity
    )
    log = capsys.readouterr().out
    write = log.index("event=write_begin")
    close = log.index("event=cleanup")
    assert (write < close) is (activity == "before_close")
    if stall:
        evidence = log.index("failure_before_cleanup")
        assert evidence < min(write, close)
        assert result["last_failure"] == "send_stall"
    assert result["transport_ok"] is not stall
    assert result["cleanup_errors"] == 2
    assert result["write_count"] == 1
    assert result["write_errors"] == 0
    paths = list(writable_probe.iterdir())
    assert [p.name for p in paths] == ["_mqtt_soak_test.log"]
    assert "recovery event: mqtt_soak" in paths[0].read_text()


def test_periodic_writes_are_paced_bounded_and_followed_by_mqtt(
    environment, writable_probe, capsys
):
    result = soak.run(
        duration_s=160, settings_root=str(writable_probe), fs_activity="periodic",
        write_interval_s=30, write_limit=2,
    )
    log = capsys.readouterr().out
    assert result["transport_ok"] is True
    assert result["write_count"] == 2
    assert result["write_errors"] == 0
    assert log.count("event=post_write_cycle") == 2
    times = [
        float(line.split("elapsed_s=")[1].split()[0])
        for line in log.splitlines() if "event=write_begin" in line
    ]
    assert times[1] - times[0] >= 30
    assert environment.pool.publications[-1][0] > times[-1]


def test_rwfs_no_write_control_never_constructs_writer(
    environment, writable_probe, monkeypatch
):
    def forbidden(*args):
        raise AssertionError("baseline must not create diagnostic writer")

    monkeypatch.setattr(soak, "DiagnosticWriter", forbidden)
    result = soak.run(duration_s=130, settings_root=str(writable_probe))
    assert result["write_count"] == 0
    assert result["write_errors"] == 0
    assert not list(writable_probe.iterdir())


def test_direct_close_avoids_manager_and_closes_raw_socket(environment, monkeypatch):
    def forbidden(*args):
        raise AssertionError("direct mode must bypass adapter cleanup")

    monkeypatch.setattr(mqtt_client, "close_mqtt_client", forbidden)
    result = soak.run(duration_s=130, cleanup_mode="direct")
    assert result["transport_ok"] is True
    assert result["cleanup_ok"] is True
    assert all(sock.closed for sock in environment.pool.sockets)


def test_injected_ebadf_records_failure_before_write_and_close(
    environment, writable_probe, monkeypatch, capsys
):
    original = mqtt_client.poll_mqtt_client

    def poll(adapter, transport):
        if environment.clock.now > 60:
            return SimpleNamespace(adapter=adapter, phase="error", errors="EBADF")
        return original(adapter, transport)

    monkeypatch.setattr(mqtt_client, "poll_mqtt_client", poll)
    result = soak.run(
        duration_s=130, settings_root=str(writable_probe), fs_activity="before_close"
    )
    log = capsys.readouterr().out
    assert result["last_failure"] == "bad_descriptor"
    assert "operation=poll" in log
    assert (
        log.index("failure_before_cleanup") < log.index("event=write_begin")
        < log.index("event=cleanup")
    )


def test_write_failure_stops_without_claiming_transport_fault(
    environment, writable_probe, monkeypatch
):
    from cpynodus_ii.core import recovery_log

    monkeypatch.setattr(recovery_log, "append_recovery_event", lambda *a, **k: False)
    result = soak.run(
        duration_s=130, settings_root=str(writable_probe), fs_activity="periodic",
        write_interval_s=5,
    )
    assert result["completed"] is False
    assert result["write_errors"] == 1
    assert result["failures"] == 0
    assert result["last_failure"] == "none"
    assert all(sock.closed for sock in environment.pool.sockets)


@pytest.mark.parametrize("suffix", ["", ".tmp", ".bak"])
def test_existing_diagnostic_files_are_preserved(
    environment, writable_probe, suffix
):
    path = writable_probe / ("_mqtt_soak_test.log" + suffix)
    path.write_text("prior evidence")
    with pytest.raises(ValueError, match="remove prior apparatus log"):
        soak.run(settings_root=str(writable_probe), fs_activity="before_close")
    assert path.read_text() == "prior evidence"
    assert not environment.pool.sockets


def test_rofs_write_request_fails_before_network(environment, monkeypatch):
    monkeypatch.setattr(soak, "_filesystem_mode", lambda: "ROFS")
    with pytest.raises(ValueError, match="require confirmed RWFS"):
        soak.run(fs_activity="periodic")
    assert not environment.pool.sockets


def test_explicit_sync_and_trim_are_separate_controls(
    environment, writable_probe, monkeypatch
):
    import os

    syncs = []
    monkeypatch.setattr(os, "sync", lambda: syncs.append(True))
    writer = soak.DiagnosticWriter(str(writable_probe), 8, False, 65536)
    transport = SimpleNamespace(
        published_messages=[], subscriptions=[], received_messages=[]
    )
    assert writer.write(0, "control", transport)
    assert not syncs
    writer.sync = True
    assert writer.write(0, "sync", transport)
    assert len(syncs) == 1
    writer.sync = False
    writer.max_bytes = 256
    for _ in range(4):
        assert writer.write(0, "trim", transport)
    assert (writable_probe / "_mqtt_soak_test.log").stat().st_size <= 256
    assert (writable_probe / "_mqtt_soak_test.log.bak").exists()


def test_co2_defaults_use_synthetic_co2_metric(environment):
    import json

    environment.config.sensor.device = "co2"
    result = soak.run(duration_s=130)
    assert result["transport_ok"] is True
    payloads = [
        json.loads(payload) for _, topic, _, payload in environment.pool.publications
        if topic.endswith("/data")
    ]
    assert payloads
    assert "CO2" in str(payloads)


def test_interrupt_during_final_write_still_closes_socket(
    environment, writable_probe, monkeypatch
):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(soak.DiagnosticWriter, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        soak.run(
            duration_s=130, settings_root=str(writable_probe),
            fs_activity="before_close",
        )
    assert all(sock.closed for sock in environment.pool.sockets)
