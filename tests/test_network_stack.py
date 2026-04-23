"""Tests for station-mode and AP-mode network stack helpers."""

from cpynodus_ii.core import build_network_stack, network_link_is_ready, reconnect_network_stack
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
    stack = build_network_stack(runtime_config, wifi_radio=radio, connection_manager_module=_FakeConnMgr)

    assert stack.phase == "ap"
    assert stack.mode == "ap"
    assert stack.ssid == "Nodus_Setup"
    assert stack.ip_address == "192.168.1.44"
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


def test_build_network_stack_retries_transient_failures_and_then_succeeds():
    radio = _FlakyRadio([ConnectionError("temporary network failure"), ConnectionError("temporary network failure")])
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(ssid="TestWiFi", password="secretpass", hostname="aqi-x943fm"),
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


def test_build_network_stack_fails_fast_on_authentication_error():
    radio = _FlakyRadio([ConnectionError("Authentication failure")])
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        network=NetworkConfig(ssid="TestWiFi", password="badpass", hostname="aqi-x943fm"),
    )

    stack = build_network_stack(
        runtime_config,
        wifi_radio=radio,
        connection_manager_module=_FakeConnMgr,
        max_attempts=3,
        retry_delay_s=0.0,
    )

    assert stack.phase == "error"
    assert "network_auth_failed" in stack.errors
    assert len(radio.connected) == 1


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
