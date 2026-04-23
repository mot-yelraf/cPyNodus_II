"""Tests for best-effort NTP synchronization behavior."""

from cpynodus_ii.core.config import NetworkConfig, RuntimeConfig
from cpynodus_ii.core.ntp import DEFAULT_NTP_SERVER, NTPState, maybe_sync_ntp
from cpynodus_ii.core.network import NetworkStack


class _ResolveOKPool:
    def __init__(self):
        self.calls = []

    def getaddrinfo(self, host, port):
        self.calls.append((host, port))
        return [(None, None, None, None, ("10.0.0.2", port))]


class _ResolveFailPool:
    def getaddrinfo(self, host, port):
        raise OSError(-2)


class _FakeNTP:
    instances = []

    def __init__(self, socket_pool, *, server, tz_offset, socket_timeout):
        self.socket_pool = socket_pool
        self.server = server
        self.tz_offset = tz_offset
        self.socket_timeout = socket_timeout
        self.datetime = (2026, 4, 22, 3, 12, 0, 0, -1, -1)
        self.__class__.instances.append(self)


class _FakeRTC:
    instances = []

    def __init__(self):
        self.datetime = None
        self.__class__.instances.append(self)


def _runtime_config():
    return RuntimeConfig(
        active_profile="nodusweb",
        network=NetworkConfig(ssid="PeaceHill", password="secretpass", hostname="aqi-x943fm"),
    )


def _network_stack(socket_pool):
    return NetworkStack(
        phase="ready",
        mode="station",
        ssid="PeaceHill",
        hostname="aqi-x943fm",
        ip_address="10.0.0.44",
        socket_pool=socket_pool,
        ssl_context=object(),
        errors=(),
    )


def test_maybe_sync_ntp_uses_default_server_when_config_blank():
    _FakeNTP.instances = []
    _FakeRTC.instances = []
    pool = _ResolveOKPool()

    result = maybe_sync_ntp(
        _runtime_config(),
        _network_stack(pool),
        now_monotonic=10.0,
        modules={"ntp_cls": _FakeNTP, "rtc_factory": _FakeRTC},
    )

    assert result.phase == "synced"
    assert result.state.server == DEFAULT_NTP_SERVER
    assert result.state.datetime_text == "2026-04-22T03:12:00"
    assert _FakeNTP.instances[0].server == DEFAULT_NTP_SERVER
    assert _FakeNTP.instances[0].tz_offset == -7.0
    assert _FakeNTP.instances[0].socket_timeout == 1.5
    assert _FakeRTC.instances[0].datetime == _FakeNTP.instances[0].datetime


def test_maybe_sync_ntp_uses_configured_server():
    _FakeNTP.instances = []
    _FakeRTC.instances = []
    pool = _ResolveOKPool()
    runtime_config = _runtime_config()
    runtime_config.time.ntp_server = "time.nist.gov"

    result = maybe_sync_ntp(
        runtime_config,
        _network_stack(pool),
        now_monotonic=10.0,
        modules={"ntp_cls": _FakeNTP, "rtc_factory": _FakeRTC},
    )

    assert result.phase == "synced"
    assert result.state.server == "time.nist.gov"
    assert result.state.datetime_text == "2026-04-22T03:12:00"
    assert _FakeNTP.instances[0].server == "time.nist.gov"
    assert pool.calls == [("time.nist.gov", 123)]


def test_maybe_sync_ntp_defers_until_hostname_resolution_is_ready():
    _FakeNTP.instances = []
    result = maybe_sync_ntp(
        _runtime_config(),
        _network_stack(_ResolveFailPool()),
        state=NTPState(),
        now_monotonic=10.0,
        modules={"ntp_cls": _FakeNTP, "rtc_factory": _FakeRTC},
    )

    assert result.phase == "deferred"
    assert result.errors[0].startswith("ntp_dns_unready:")
    assert _FakeNTP.instances == []


def test_maybe_sync_ntp_resyncs_after_86400_seconds():
    _FakeNTP.instances = []
    _FakeRTC.instances = []
    pool = _ResolveOKPool()
    state = NTPState(
        phase="synced",
        server=DEFAULT_NTP_SERVER,
        datetime_text="2026-04-22T03:12:00",
        last_attempt_at=10.0,
        last_sync_at=10.0,
    )

    skipped = maybe_sync_ntp(
        _runtime_config(),
        _network_stack(pool),
        state=state,
        now_monotonic=86409.0,
        modules={"ntp_cls": _FakeNTP, "rtc_factory": _FakeRTC},
    )
    synced = maybe_sync_ntp(
        _runtime_config(),
        _network_stack(pool),
        state=state,
        now_monotonic=86410.0,
        modules={"ntp_cls": _FakeNTP, "rtc_factory": _FakeRTC},
    )

    assert skipped.phase == "skipped"
    assert skipped.errors == ("ntp_not_due",)
    assert synced.phase == "synced"
    assert len(_FakeNTP.instances) == 1
