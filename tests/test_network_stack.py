"""Tests for station-mode and AP-mode network stack helpers."""

import cpynodus_ii.core.network as network_module
from cpynodus_ii.core import (
    build_network_stack,
    network_error_signature,
    network_link_is_ready,
    reconnect_network_stack,
)
from cpynodus_ii.core.config import NetworkConfig, RuntimeConfig


class _FakeRadio:
    def __init__(self):
        self.connected = []
        self.ap_started = []
        self.hostname = ""
        self.ipv4_address = "192.168.1.44"
        self.ipv4_address_ap = "192.168.4.1"

    def connect(self, ssid, password):
        self.connected.append((ssid, password))

    def start_ap(self, ssid, password):
        self.ap_started.append((ssid, password))


class _RadioNotConnectedAfterConnect:
    def __init__(self):
        self.connect_calls = []
        self.ap_started = []
        self.hostname = ""
        self.ipv4_address = "192.168.1.44"
        self.ipv4_address_ap = "192.168.4.1"

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))

    @property
    def connected(self):
        return False


class _FakeAPInfo:
    def __init__(self, ssid):
        self.ssid = ssid


class _StaleAPStationRadio:
    def __init__(self):
        self.ap_info = _FakeAPInfo("Nodus_Setup")
        self.connected = True
        self.connect_calls = []
        self.disconnect_calls = 0
        self.start_station_calls = 0
        self.stop_ap_calls = 0
        self.stop_station_calls = 0
        self.hostname = ""
        self.ipv4_address = "192.168.4.17"
        self.ipv4_address_ap = "192.168.4.1"

    def disconnect(self):
        self.disconnect_calls += 1
        self.ap_info = None
        self.ipv4_address = ""

    def start_station(self):
        self.start_station_calls += 1

    def stop_ap(self):
        self.stop_ap_calls += 1
        self.ipv4_address_ap = ""

    def stop_station(self):
        self.stop_station_calls += 1
        self.ap_info = None
        self.ipv4_address = ""

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        self.ap_info = _FakeAPInfo(ssid)
        self.ipv4_address = "10.0.0.252"


class _APSubnetStationRadio(_StaleAPStationRadio):
    def __init__(self):
        super().__init__()
        self.ap_info = None

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        self.ipv4_address = "192.168.4.17"


class _FakeConnMgr:
    @staticmethod
    def get_radio_socketpool(radio):
        return {"kind": "socketpool", "radio": radio}

    @staticmethod
    def get_radio_ssl_context(radio):
        return {"kind": "ssl", "radio": radio}


class _FlakyRadio:
    def __init__(self, failures):
        self.failures = list(failures)
        self.connected = []
        self.hostname = ""
        self.ipv4_address = "192.168.1.99"

    def connect(self, ssid, password):
        self.connected.append((ssid, password))
        if self.failures:
            raise self.failures.pop(0)


class _NotImplementedAPInfoRadio(_FlakyRadio):
    @property
    def ap_info(self):
        raise NotImplementedError()


class _FakeScanNetwork:
    def __init__(self, ssid):
        self.ssid = ssid


class _ScanMissThenSuccessRadio:
    def __init__(self):
        self.connect_calls = []
        self.scan_calls = 0
        self.stop_scan_calls = 0
        self.hostname = ""
        self.ipv4_address = ""
        self.ipv4_address_ap = "192.168.4.1"

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        if len(self.connect_calls) == 1:
            raise ConnectionError("No network with that ssid")
        self.ipv4_address = "10.0.0.219"

    def start_scanning_networks(self):
        self.scan_calls += 1
        return [_FakeScanNetwork("NeighborWiFi"), _FakeScanNetwork("PeaceHill")]

    def stop_scanning_networks(self):
        self.stop_scan_calls += 1


def test_build_network_stack_connects_station_mode_and_returns_socket_artifacts():
    radio = _FakeRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi",
            password="secretpass",
            hostname="aqi-x943fm",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "ready"
    assert stack.mode == "station"
    assert radio.connected == [("TestWiFi", "secretpass")]
    assert radio.hostname == "aqi-x943fm"
    assert stack.ip_address == "192.168.1.44"
    assert stack.socket_pool["kind"] == "socketpool"
    assert stack.ssl_context["kind"] == "ssl"


def test_build_network_stack_returns_ap_mode_when_requested():
    runtime_config = RuntimeConfig(
        ap_mode=True,
        network=NetworkConfig(
            ap_ssid="Nodus_Setup",
            ap_password="password",
            hostname="nodus-ap",
        ),
    )

    radio = _FakeRadio()
    stack = build_network_stack(
        runtime_config, wifi_radio=radio, connection_manager_module=_FakeConnMgr
    )

    assert stack.phase == "ap"
    assert stack.mode == "ap"
    assert stack.ssid == "Nodus_Setup"
    assert stack.ip_address == "192.168.4.1"
    assert radio.ap_started == [("Nodus_Setup", "password")]


def test_build_network_stack_reports_missing_wifi_modules():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(ssid="TestWiFi", password="secretpass"),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=None,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "unavailable"
    assert "wifi_module_unavailable" in stack.errors


def test_network_error_signature_marks_station_scan_miss_after_ready():
    radio = _FlakyRadio([ConnectionError("No network with that ssid")])
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            hostname="co2-ykdvea",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=1,
        retry_delay_s=0.0,
    )

    assert network_error_signature(stack) == "station_scan_miss"
    assert (
        network_error_signature(stack, had_ready_link=True)
        == "station_scan_miss_after_ready"
    )


def test_network_error_signature_keeps_auth_failure_distinct():
    radio = _FlakyRadio([ConnectionError("Authentication failure")])
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            hostname="co2-ykdvea",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=1,
        retry_delay_s=0.0,
    )

    assert network_error_signature(stack, had_ready_link=True) == "station_auth_failed"


def test_build_network_stack_retries_transient_failures_and_then_succeeds():
    radio = _FlakyRadio(
        [
            ConnectionError("temporary network failure"),
            ConnectionError("temporary network failure"),
        ]
    )
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi", password="secretpass", hostname="aqi-x943fm"
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "ready"
    assert len(radio.connected) == 3
    assert stack.ip_address == "192.168.1.99"


def test_build_network_stack_scans_after_scan_miss_before_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        network_module.time,
        "sleep",
        lambda value: sleeps.append(value),
    )
    radio = _ScanMissThenSuccessRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            hostname="co2-ykdvea",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "ready"
    assert radio.connect_calls == [
        ("PeaceHill", "secretpass"),
        ("PeaceHill", "secretpass"),
    ]
    assert radio.scan_calls == 1
    assert radio.stop_scan_calls == 1
    assert sleeps == [2.0]


def test_build_network_stack_retries_authentication_error_before_failing():
    radio = _FlakyRadio(
        [
            ConnectionError("Authentication failure"),
            ConnectionError("Authentication failure"),
            ConnectionError("Authentication failure"),
        ]
    )
    radio.ipv4_address = "10.0.0.252"
    radio.ipv4_address_ap = "192.168.4.1"
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi", password="badpass", hostname="aqi-x943fm"
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "error"
    assert stack.ip_address == "10.0.0.252"
    assert "network_auth_failed" in stack.errors
    assert "exception=ConnectionError" in stack.errors
    assert "station_ip=10.0.0.252" in stack.errors
    assert "ap_ip=192.168.4.1" in stack.errors
    assert len(radio.connected) == 3


def test_build_network_stack_recovers_after_transient_authentication_error():
    radio = _FlakyRadio([ConnectionError("Authentication failure")])
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi", password="secretpass", hostname="aqi-x943fm"
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "ready"
    assert stack.ip_address == "192.168.1.99"
    assert len(radio.connected) == 2


def test_build_network_stack_diagnostics_ignore_unimplemented_ap_info():
    radio = _NotImplementedAPInfoRadio([ConnectionError("temporary network failure")])
    radio.ipv4_address = "10.0.0.252"
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi", password="secretpass", hostname="aqi-x943fm"
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=1,
        retry_delay_s=0.0,
    )

    assert stack.phase == "error"
    assert "network_connect_failed" in stack.errors
    assert "station_ip=10.0.0.252" in stack.errors


def test_build_network_stack_reports_missing_ssid_for_station_mode():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(ssid="", password="secretpass", hostname="aqi-x943fm"),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=_FakeRadio(),
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "error"
    assert stack.errors == ("network_ssid_missing",)


def test_build_network_stack_rejects_unconnected_radio_after_connect_call():
    radio = _RadioNotConnectedAfterConnect()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill", password="secretpass", hostname="co2-29j39c"
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "error"
    assert "network_not_connected" in stack.errors
    assert "connected=False" in stack.errors


def test_build_network_stack_rejects_wrong_station_ssid():
    radio = _FakeRadio()
    radio.ap_info = _FakeAPInfo("Nodus_Setup")
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            ap_ssid="Nodus_Setup",
            hostname="co2-29j39c",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "error"
    assert "network_wrong_ssid" in stack.errors
    assert "actual_ssid=Nodus_Setup" in stack.errors


def test_build_network_stack_rejects_ap_subnet_station_ip_without_wrong_ssid():
    radio = _FakeRadio()
    radio.ipv4_address = "192.168.4.16"
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            ap_ssid="Nodus_Setup",
            hostname="co2-29j39c",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "error"
    assert stack.ip_address == "192.168.4.16"
    assert "network_ap_subnet_suspect" in stack.errors
    assert "station_ip=192.168.4.16" in stack.errors


def test_build_network_stack_disconnects_stale_ap_station_link_before_join():
    radio = _StaleAPStationRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            ap_ssid="Nodus_Setup",
            hostname="co2-29j39c",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    assert stack.phase == "ready"
    assert stack.ip_address == "10.0.0.252"
    assert radio.disconnect_calls == 1
    assert radio.stop_ap_calls == 1
    assert radio.stop_station_calls == 1
    assert radio.start_station_calls == 1
    assert radio.connect_calls == [("PeaceHill", "secretpass")]


def test_build_network_stack_retries_ap_subnet_station_ip():
    radio = _APSubnetStationRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            ap_ssid="Nodus_Setup",
            hostname="co2-29j39c",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "error"
    assert "network_ap_subnet_suspect" in stack.errors
    assert radio.connect_calls == [
        ("PeaceHill", "secretpass"),
        ("PeaceHill", "secretpass"),
        ("PeaceHill", "secretpass"),
    ]
    assert radio.stop_ap_calls == 3
    assert radio.stop_station_calls == 2
    assert radio.start_station_calls == 2


def test_reconnect_network_stack_preserves_socket_artifacts_until_rebuild_requested():
    radio = _FakeRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="TestWiFi",
            password="secretpass",
            hostname="aqi-x943fm",
        ),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    preserved = reconnect_network_stack(
        runtime_config,
        stack,
        max_attempts=1,
        retry_delay_s=0.0,
        rebuild_socket_artifacts=False,
    )
    rebuilt = reconnect_network_stack(
        runtime_config,
        stack,
        max_attempts=1,
        retry_delay_s=0.0,
        rebuild_socket_artifacts=True,
    )

    assert network_link_is_ready(preserved) is True
    assert preserved.socket_pool is stack.socket_pool
    assert rebuilt.socket_pool is not stack.socket_pool


def test_reconnect_network_stack_can_force_station_reset_before_retry():
    radio = _StaleAPStationRadio()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(
            ssid="PeaceHill",
            password="secretpass",
            hostname="co2-ykdvea",
        ),
    )
    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
    )

    radio.ipv4_address = ""
    reconnect = reconnect_network_stack(
        runtime_config,
        stack,
        max_attempts=1,
        retry_delay_s=0.0,
        rebuild_socket_artifacts=True,
        reset_station=True,
    )

    assert network_link_is_ready(reconnect) is True
    assert radio.disconnect_calls >= 1
    assert radio.stop_station_calls >= 1
    assert radio.start_station_calls >= 1
    assert reconnect.socket_pool is not stack.socket_pool
