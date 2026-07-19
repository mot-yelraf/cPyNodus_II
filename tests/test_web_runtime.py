"""Tests for web runtime startup and polling behavior."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.network import build_network_stack
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import web_runtime
from cpynodus_ii.features.web_runtime import WebRuntimeController


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


class _FakeRequest:
    def __init__(self, body=b""):
        self.body = body


class _FakeResponse:
    def __init__(self, request, body=None, content_type="", status=None):
        self.request = request
        self.body = body
        self.content_type = content_type
        self.status = status


class _FakeJSONResponse(_FakeResponse):
    pass


class _FakeServer:
    def __init__(self, socket_pool, debug=False):
        self.socket_pool = socket_pool
        self.debug = debug
        self.headers = {}
        self.request_buffer_size = 0
        self.socket_timeout = 0
        self.started = None
        self.routes = {}
        self.poll_count = 0
        self.stop_count = 0

    def start(self, host, port):
        self.started = (host, port)

    def route(self, path, methods=None):
        def _decorator(fn):
            self.routes[(path, tuple(methods or ()))] = fn
            return fn

        return _decorator

    def poll(self):
        self.poll_count += 1

    def stop(self):
        self.stop_count += 1


class _FakeServerModule:
    Server = _FakeServer
    Response = _FakeResponse
    JSONResponse = _FakeJSONResponse


class _ScriptedSocket:
    def __init__(self, reads=(), sends=()):
        self.reads = list(reads)
        self.sends = list(sends)
        self.blocking = []
        self.timeouts = []

    def setblocking(self, value):
        self.blocking.append(value)

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv_into(self, buffer, size):
        value = self.reads.pop(0)
        if isinstance(value, Exception):
            raise value
        length = min(len(value), size)
        buffer[:length] = value[:length]
        return length

    def send(self, buffer):
        value = self.sends.pop(0) if self.sends else len(buffer)
        if isinstance(value, Exception):
            raise value
        return min(int(value), len(buffer))


def _runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text(
                (docs_root / name).read_text(), encoding="utf-8"
            )
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        runtime_config.active_profile = "nodusweb"
        return runtime_config


def test_bounded_header_read_accepts_fragmented_http(monkeypatch):
    would_block = OSError(web_runtime.EAGAIN)
    sock = _ScriptedSocket(
        reads=(b"GET / HTTP/1.1\r\nHost: nodus", would_block, b"\r\n\r\n")
    )
    server = SimpleNamespace(_buffer=bytearray(64), _timeout=1)
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    received = web_runtime._receive_bounded_header(server, sock)

    assert received.endswith(b"\r\n\r\n")
    assert sock.blocking == [False]
    assert sock.timeouts == [1]


def test_bounded_header_read_returns_tls_client_hello_immediately():
    sock = _ScriptedSocket(reads=(b"\x16\x03\x01\x05\xfe",))
    server = SimpleNamespace(_buffer=bytearray(64), _timeout=1)

    received = web_runtime._receive_bounded_header(server, sock)

    assert received == b"\x16\x03\x01\x05\xfe"
    assert sock.blocking == [False]
    assert sock.timeouts == [1]


def test_bounded_header_read_expires_incomplete_client(monkeypatch):
    sock = _ScriptedSocket(
        reads=(
            b"GET / HTTP/1.1\r\n",
            OSError(web_runtime.EAGAIN),
            OSError(web_runtime.EAGAIN),
        )
    )
    server = SimpleNamespace(_buffer=bytearray(64), _timeout=1)
    times = iter((0.0, 0.1, 0.3))
    monkeypatch.setattr(web_runtime, "monotonic", lambda: next(times))
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    received = web_runtime._receive_bounded_header(server, sock)

    assert received == b"GET / HTTP/1.1\r\n"
    assert sock.timeouts == [1]


def test_bounded_body_read_accepts_fragments(monkeypatch):
    sock = _ScriptedSocket(
        reads=(b'"state":', OSError(web_runtime.EAGAIN), b"true}")
    )
    server = SimpleNamespace(_buffer=bytearray(64), _timeout=1)
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    received = web_runtime._receive_bounded_body(
        server, sock, b"{", len(b'{"state":true}')
    )

    assert received == b'{"state":true}'
    assert sock.timeouts == [1]


def test_bounded_send_retries_and_rejects_zero_progress(monkeypatch):
    sock = _ScriptedSocket(sends=(OSError(web_runtime.EAGAIN), 2, 3))
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    assert web_runtime._send_bounded_bytes(sock, b"hello") == 5

    stalled = _ScriptedSocket(sends=(0,))
    try:
        web_runtime._send_bounded_bytes(stalled, b"hello")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ECONNRESET
    else:
        raise AssertionError("zero-progress send did not fail")


def test_bounded_adapters_wrap_real_server_shapes():
    class _ServerShape:
        def _receive_header_bytes(self, sock):
            return b"base"

    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    assert web_runtime._bounded_server_class(_ServerShape) is not _ServerShape
    assert web_runtime._bounded_response_class(_ResponseShape) is not _ResponseShape


def test_ap_mode_network_stack_includes_socket_artifacts_for_web_runtime():
    runtime_config = _runtime_config()
    runtime_config.ap_mode = True

    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    assert stack.phase == "ap"
    assert stack.socket_pool["kind"] == "socketpool"
    assert stack.ssl_context["kind"] == "ssl"


def test_web_runtime_controller_registers_and_polls_routes():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.112.15",
        server_module=_FakeServerModule,
    ).start()
    controller.poll()

    assert controller.phase == "ready"
    assert controller.server.started == ("0.0.0.0", 8000)
    assert ("/setup", ("GET",)) in controller.server.routes
    assert ("/config", ("POST",)) in controller.server.routes
    assert controller.server.poll_count == 1


def test_web_status_page_is_small_and_excludes_configuration_editors():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.193.1",
        server_module=_FakeServerModule,
    ).start()

    response = controller.server.routes[("/", ("GET",))](_FakeRequest())
    html = response.body

    assert "Status" in html
    assert 'rel="icon" href="data:image/svg+xml;base64,PHN2Zy' in html
    assert "background:#eef8f3;color:#1b1f24" in html
    assert 'id="sample_timestamp"' in html
    assert 'id="sample_1"' in html
    assert 'id="switch_SWITCH_1"' in html
    assert 'href="/switch-setup"' in html
    assert 'href="/automations-ui"' in html
    assert "setInterval(refresh,15000)" in html
    assert "fetch('/current-data',{cache:'no-store'})" in html
    assert "Device Calibration" not in html
    assert "Save &amp; Restart" not in html
    assert 'id="wifi_password"' not in html
    assert 'id="automation_script"' not in html
    assert "loadAutomations()" not in html
    assert len(html.encode("utf-8")) < 7000


def test_web_configuration_surfaces_render_on_separate_routes():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.200.1",
        automation_service=SimpleNamespace(
            active=True,
            payload=lambda: {"rules": []},
            channel_controlled=lambda **kwargs: (),
        ),
        server_module=_FakeServerModule,
    ).start()

    setup = controller.server.routes[("/setup", ("GET",))](_FakeRequest()).body
    calibration = controller.server.routes[("/calibration", ("GET",))](
        _FakeRequest()
    ).body
    switch = controller.server.routes[("/switch-setup", ("GET",))](
        _FakeRequest()
    ).body
    automations = controller.server.routes[("/automations-ui", ("GET",))](
        _FakeRequest()
    ).body
    info = controller.server.routes[("/info", ("GET",))](_FakeRequest()).body

    assert "Save &amp; Restart" in setup
    assert 'id="wifi_password" type="password"' in setup
    assert "Device Calibration" in calibration
    assert "AQI OFFSET" in calibration
    assert "Switch Settings" in switch
    assert "Switch Automations" in automations
    assert 'id="automation_script"' in automations
    assert "loadAutomations()" in automations
    assert "Nodus Info" in info
    assert "WiFi Recovery Count" in info
    assert 'id="automation_script"' not in setup


def test_web_status_memory_error_returns_503(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.193.1",
        server_module=_FakeServerModule,
    ).start()

    def fail_render(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(
        "cpynodus_ii.features.web_status_ui.render_status_html", fail_render
    )
    response = controller.server.routes[("/", ("GET",))](_FakeRequest())

    assert response.status == (503, "Service Unavailable")
    assert "retry" in response.body


def test_web_pages_enforce_heap_floors_after_collection(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.200.1",
        server_module=_FakeServerModule,
    ).start()
    collections = []

    monkeypatch.setattr(web_runtime.gc, "collect", lambda: collections.append(True))
    monkeypatch.setattr(web_runtime.gc, "mem_free", lambda: 15999, raising=False)

    status = controller.server.routes[("/", ("GET",))](_FakeRequest())
    setup = controller.server.routes[("/setup", ("GET",))](_FakeRequest())

    assert status.status == (503, "Service Unavailable")
    assert setup.status == (503, "Service Unavailable")
    assert len(collections) == 2


def test_web_dashboard_keeps_last_successful_sample_and_timestamp(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    ready_snapshot = SimpleNamespace(
        phase="ready",
        sensor_id="aqi-x943fm",
        device="aqi",
        metrics={"Air Quality": 42.5},
        errors=(),
    )
    monkeypatch.setattr(
        web_runtime,
        "localtime",
        lambda: (2026, 7, 11, 7, 30, 45, 5, 192, -1),
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.192.1",
        sensor_snapshot=ready_snapshot,
        server_module=_FakeServerModule,
    ).start()

    first = controller.server.routes[("/current-data", ("GET",))](_FakeRequest())
    second = controller.server.routes[("/", ("GET",))](_FakeRequest())

    assert first.body["sensor"]["display_timestamp"] == "2026-07-11 07:30:45"
    assert first.body["sensor"]["display_metrics"][0]["value"] == 42.5
    assert "2026-07-11 07:30:45" in second.body
    assert "42.50" in second.body


def test_web_dashboard_never_reads_sensor_from_request_handler(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    startup_snapshot = SimpleNamespace(
        phase="ready",
        sensor_id="aqi-x943fm",
        device="aqi",
        metrics={"Air Quality": 37.0},
        errors=(),
    )
    def fail_if_called(*args, **kwargs):
        raise AssertionError("request handler read sensor")

    monkeypatch.setattr(
        "cpynodus_ii.features.sensor_service.read_sensor_snapshot",
        fail_if_called,
    )
    monkeypatch.setattr(
        web_runtime,
        "localtime",
        lambda: (2026, 7, 11, 9, 15, 0, 5, 192, -1),
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.192.1",
        sensor_service=SimpleNamespace(phase="ready"),
        sensor_snapshot=startup_snapshot,
        server_module=_FakeServerModule,
    ).start()

    response = controller.server.routes[("/current-data", ("GET",))](_FakeRequest())

    assert response.body["sensor"]["display_metrics"][0]["value"] == 37.0
    assert response.body["sensor"]["display_timestamp"] == "2026-07-11 09:15:00"
    assert response.body["sensor"]["snapshot"]["errors"] == []


def test_web_runtime_remains_disabled_outside_nodusweb_and_ap():
    runtime_config = _runtime_config()
    runtime_config.active_profile = "sensorius"
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.193.1",
        server_module=_FakeServerModule,
    ).start()

    assert controller.phase == "disabled"
    assert controller.server is None


def test_web_runtime_controller_stops_server():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.112.15",
        server_module=_FakeServerModule,
    ).start()
    server = controller.server

    assert controller.stop() is True
    assert server.stop_count == 1
    assert controller.server is None
    assert controller.phase == "stopped"


def test_web_info_displays_updated_wifi_recovery_count():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.192.1",
        wifi_recovery_count=1,
        server_module=_FakeServerModule,
    ).start()
    controller.update_context(wifi_recovery_count=3)

    response = controller.server.routes[("/info", ("GET",))](_FakeRequest())

    assert "WiFi Recovery Count" in response.body
    assert "<td>3</td>" in response.body


def test_web_runtime_silently_recovers_tls_client_hello():
    class _MalformedRequestServer(_FakeServer):
        def poll(self):
            raise ValueError(("Unparseable raw_request: ", b"\x16\x03\x01"))

    class _MalformedRequestModule(_FakeServerModule):
        Server = _MalformedRequestServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.192.1",
        server_module=_MalformedRequestModule,
        event_logger=events.append,
    ).start()

    controller.poll()

    assert controller.phase == "ready"
    assert controller.errors == ()
    assert events == []


def test_web_runtime_logs_other_unparseable_browser_requests():
    class _MalformedRequestServer(_FakeServer):
        def poll(self):
            raise ValueError(("Unparseable raw_request: ", b"NOT HTTP"))

    class _MalformedRequestModule(_FakeServerModule):
        Server = _MalformedRequestServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.200.1",
        server_module=_MalformedRequestModule,
        event_logger=events.append,
    ).start()

    controller.poll()

    assert controller.phase == "ready"
    assert controller.errors == ()
    assert len(events) == 1
    assert "reason=web_poll_recovered" in events[0]
    assert "NOT_HTTP" in events[0]


def test_web_runtime_logs_and_stops_polling_after_unexpected_error():
    class _FailedPollServer(_FakeServer):
        def poll(self):
            raise RuntimeError("poll failed")

    class _FailedPollModule(_FakeServerModule):
        Server = _FailedPollServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.192.1",
        server_module=_FailedPollModule,
        event_logger=events.append,
    ).start()

    controller.poll()

    assert controller.phase == "error"
    assert controller.errors == ("web_poll_failed", "poll failed")
    assert events == [
        "phase=error reason=web_poll_failed type=RuntimeError detail=poll_failed "
        "server=present;request_buffer_size=2048;socket_timeout=1 free_mem=unknown"
    ]


def test_web_runtime_controller_config_route_updates_runtime_config():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.112.15",
        server_module=_FakeServerModule,
    ).start()

    handler = controller.server.routes[("/config", ("POST",))]
    response = handler(
        _FakeRequest(
            b'{"updates":[{"section":"Sensor","key":"LOCATION","value":"Bench D"}]}'
        )
    )

    assert response.body["success"] is True
    assert controller.runtime_config.sensor.location == "Bench D"


def test_web_runtime_controller_defers_itaot_init_reboot_until_after_response():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            '[Profile]\nACTIVE_PROFILE = "nodusweb"\n',
            encoding="utf-8",
        )
        runtime_config = _runtime_config()
        runtime_config.ap_mode = True
        stack = build_network_stack(
            runtime_config,
            wifi_radio=_FakeRadio(),
            connection_manager_module=_FakeConnMgr,
        )
        reboot_calls = []
        log_events = []
        controller = WebRuntimeController(
            runtime_config,
            stack,
            version="v0.26.162.5",
            settings_root=tmpdir_path,
            server_module=_FakeServerModule,
            reboot_callbacks={"hard": lambda: reboot_calls.append("hard")},
            event_logger=log_events.append,
        ).start()

        handler = controller.server.routes[("/itaot-init", ("POST",))]
        response = handler(
            _FakeRequest(
                (
                    b'{"onboard_token":"token-123","ssid":"TestWiFi",'
                    b'"password":"secretpass","hostname":"co2-w9umh8",'
                    b'"mqtt":{"broker_host":"sensorius-broker.local",'
                    b'"broker_port":1883}}'
                )
            )
        )

        assert response.body["accepted"] is True
        assert reboot_calls == []
        assert log_events == [
            "request path=/itaot-init phase=received",
            "request path=/itaot-init phase=parsed time_present=0",
            "request path=/itaot-init apply phase=normalize_begin",
            (
                "request path=/itaot-init apply phase=normalize_done errors=none "
                "ssid_present=1 password_present=1 hostname_present=1 "
                "broker_host_present=1 time_keys=none"
            ),
            (
                "request path=/itaot-init apply phase=updates_built updates=7 "
                "sections=Network,MQTT,Profile time_updates=0 time_keys=none"
            ),
            (
                "request path=/itaot-init apply phase=persist_begin "
                "writer=direct reload_runtime=0"
            ),
            "request path=/itaot-init apply phase=persist_targets_begin",
            (
                "request path=/itaot-init apply phase=persist_targets_done "
                "targets=7 sections=Network,MQTT,Profile password_updates=1"
            ),
            (
                "request path=/itaot-init apply phase=persist_open_begin "
                "file=settings.toml"
            ),
            "request path=/itaot-init apply phase=persist_open_done",
            "request path=/itaot-init apply phase=persist_copy_begin",
            (
                "request path=/itaot-init apply phase=persist_copy_done "
                "copied=2 appended=6 found=7 missing=none"
            ),
            "request path=/itaot-init apply phase=persist_flush_done",
            "request path=/itaot-init apply phase=persist_close_done",
            "request path=/itaot-init apply phase=persist_validate_done",
            "request path=/itaot-init apply phase=persist_rotate_begin",
            "request path=/itaot-init apply phase=persist_backup_done",
            "request path=/itaot-init apply phase=persist_rename_done",
            (
                "request path=/itaot-init apply phase=persist_done applied=7 "
                "time_updates=0 time_keys=none errors=none"
            ),
            "request path=/itaot-init apply phase=state_persist_begin",
            "request path=/itaot-init apply phase=state_persist_done",
            "request path=/itaot-init apply phase=response_ready status=200",
            (
                "request path=/itaot-init phase=applied status=200 accepted=1 "
                "rebooting=1 updates=7 time_updates=0 time_keys=none errors=none"
            ),
            "request path=/itaot-init phase=reboot_scheduled",
            "request path=/itaot-init phase=response_return status=200",
        ]

        controller.poll()

    assert reboot_calls == ["hard"]
    assert log_events[-1] == "request path=/itaot-init phase=reboot_execute"


def test_web_runtime_controller_logs_itaot_init_time_updates():
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "settings.toml").write_text(
            '[Profile]\nACTIVE_PROFILE = "nodusweb"\n',
            encoding="utf-8",
        )
        runtime_config = _runtime_config()
        runtime_config.ap_mode = True
        stack = build_network_stack(
            runtime_config,
            wifi_radio=_FakeRadio(),
            connection_manager_module=_FakeConnMgr,
        )
        log_events = []
        controller = WebRuntimeController(
            runtime_config,
            stack,
            version="v0.26.162.5",
            settings_root=tmpdir_path,
            server_module=_FakeServerModule,
            event_logger=log_events.append,
        ).start()

        handler = controller.server.routes[("/itaot-init", ("POST",))]
        response = handler(
            _FakeRequest(
                (
                    b'{"onboard_token":"token-123","ssid":"TestWiFi",'
                    b'"password":"secretpass","hostname":"co2-w9umh8",'
                    b'"mqtt":{"broker_host":"sensorius-broker.local",'
                    b'"broker_port":1883},"time":{"TZ":"America/Denver",'
                    b'"TZ_OFFSET":-21600,"TZ_NAME":"MDT",'
                    b'"NTP_SERVER":"us.pool.ntp.org",'
                    b'"NTP_SERVER_IP":"132.163.96.6"}}'
                )
            )
        )

    assert response.body["accepted"] is True
    assert log_events[:3] == [
        "request path=/itaot-init phase=received",
        "request path=/itaot-init phase=parsed time_present=1",
        "request path=/itaot-init apply phase=normalize_begin",
    ]
    assert log_events[3:21] == [
        (
            "request path=/itaot-init apply phase=normalize_done errors=none "
            "ssid_present=1 password_present=1 hostname_present=1 "
            "broker_host_present=1 "
            "time_keys=TZ,TZ_NAME,NTP_SERVER,NTP_SERVER_IP,TZ_OFFSET"
        ),
        (
            "request path=/itaot-init apply phase=updates_built updates=12 "
            "sections=Network,MQTT,Profile,Time time_updates=5 "
            "time_keys=TZ,TZ_NAME,NTP_SERVER,NTP_SERVER_IP,TZ_OFFSET"
        ),
        (
            "request path=/itaot-init apply phase=persist_begin "
            "writer=direct reload_runtime=0"
        ),
        "request path=/itaot-init apply phase=persist_targets_begin",
        (
            "request path=/itaot-init apply phase=persist_targets_done "
            "targets=12 sections=Network,MQTT,Profile,Time password_updates=1"
        ),
        "request path=/itaot-init apply phase=persist_open_begin file=settings.toml",
        "request path=/itaot-init apply phase=persist_open_done",
        "request path=/itaot-init apply phase=persist_copy_begin",
        (
            "request path=/itaot-init apply phase=persist_copy_done "
            "copied=2 appended=11 found=12 missing=none"
        ),
        "request path=/itaot-init apply phase=persist_flush_done",
        "request path=/itaot-init apply phase=persist_close_done",
        "request path=/itaot-init apply phase=persist_validate_done",
        "request path=/itaot-init apply phase=persist_rotate_begin",
        "request path=/itaot-init apply phase=persist_backup_done",
        "request path=/itaot-init apply phase=persist_rename_done",
        (
            "request path=/itaot-init apply phase=persist_done applied=12 "
            "time_updates=5 "
            "time_keys=TZ,TZ_NAME,NTP_SERVER,NTP_SERVER_IP,TZ_OFFSET "
            "errors=none"
        ),
        "request path=/itaot-init apply phase=state_persist_begin",
        "request path=/itaot-init apply phase=state_persist_done",
    ]
    assert log_events[21:24] == [
        "request path=/itaot-init apply phase=response_ready status=200",
        (
            "request path=/itaot-init phase=applied status=200 accepted=1 "
            "rebooting=1 updates=12 time_updates=5 "
            "time_keys=TZ,TZ_NAME,NTP_SERVER,NTP_SERVER_IP,TZ_OFFSET errors=none"
        ),
        "request path=/itaot-init phase=response_return status=200",
    ]


def test_web_runtime_controller_logs_itaot_init_apply_exception(monkeypatch):
    runtime_config = _runtime_config()
    runtime_config.ap_mode = True
    stack = build_network_stack(
        runtime_config,
        wifi_radio=_FakeRadio(),
        connection_manager_module=_FakeConnMgr,
    )
    log_events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.162.5",
        server_module=_FakeServerModule,
        event_logger=log_events.append,
    ).start()

    def fail_apply(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "cpynodus_ii.features.web_runtime.apply_itaot_init_payload",
        fail_apply,
    )
    handler = controller.server.routes[("/itaot-init", ("POST",))]
    response = handler(
        _FakeRequest(
            (
                b'{"onboard_token":"token-123","ssid":"TestWiFi",'
                b'"password":"secretpass","hostname":"co2-w9umh8",'
                b'"mqtt":{"broker_host":"sensorius-broker.local",'
                b'"broker_port":1883}}'
            )
        )
    )

    assert response.status == (500, "Internal Server Error")
    assert response.body["error"] == "itaot_init_exception"
    assert log_events == [
        "request path=/itaot-init phase=received",
        "request path=/itaot-init phase=parsed time_present=0",
        "request path=/itaot-init phase=apply_exception type=RuntimeError detail=boom",
        "request path=/itaot-init phase=response_return status=500",
    ]


def test_web_runtime_controller_switch_route_applies_live_override():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    class _Handle:
        def __init__(self, value=False):
            self.value = value

    switch_service = type(
        "_SwitchService",
        (),
        {
            "phase": "ready",
            "device_id": "switch-x943fm",
            "channel_count": 2,
            "errors": (),
            "channels": (
                type(
                    "_Channel",
                    (),
                    {
                        "key": "SWITCH_1",
                        "channel_id": "S1-x943fm",
                        "phase": "ready",
                        "control_handle": _Handle(False),
                        "enable_handle": _Handle(True),
                        "errors": (),
                    },
                )(),
            ),
        },
    )()

    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.112.15",
        switch_service=switch_service,
        server_module=_FakeServerModule,
    ).start()
    handler = controller.server.routes[("/set-switch-state", ("POST",))]
    response = handler(_FakeRequest(b'{"channel_id":"S1-x943fm","state":true}'))

    assert response.body["success"] is True
    assert response.body["guard_seconds"] == 5
    assert controller.runtime_config.switch.channels[0].last_state is True

    guarded = handler(_FakeRequest(b'{"channel_id":"S1-x943fm","state":false}'))

    assert guarded.status == (429, "Too Many Requests")
    assert guarded.body["error"] == "switch_manual_guard_active"


def test_web_runtime_automation_routes_and_ownership_guard():
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )

    class _AutomationService:
        active = True

        def payload(self):
            return {"success": True, "rules": []}

        def channel_controlled(self, channel_id=None, channel_key=None):
            return ("Water plants",) if channel_id == "S1-x943fm" else ()

        def save_rule(self, rule_id, enabled, script):
            return {"success": True, "rule_id": rule_id or "new_rule"}

        def delete_rule(self, rule_id):
            return {"success": True, "rule_id": rule_id}

    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.193.1",
        automation_service=_AutomationService(),
        server_module=_FakeServerModule,
    ).start()

    assert ("/automations", ("GET",)) in controller.server.routes
    assert ("/automations", ("POST",)) in controller.server.routes
    assert ("/automations/delete", ("POST",)) in controller.server.routes
    response = controller.server.routes[("/set-switch-state", ("POST",))](
        _FakeRequest(b'{"channel_id":"S1-x943fm","state":true}')
    )

    assert response.status == (409, "Conflict")
    assert response.body["error"] == "switch_controlled_by_automation"
