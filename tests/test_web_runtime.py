"""Tests for web runtime startup and polling behavior."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cpynodus_ii.core.network import build_network_stack
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import web_config_ui, web_runtime, web_ui_common
from cpynodus_ii.features.web_handlers import build_config_page_payload
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
    def __init__(self, body=b"", path=""):
        self.body = body
        self.path = path


class _FakeResponse:
    def __init__(self, request, body=None, content_type="", status=None):
        self.request = request
        self.raw_body = body
        self.body = (
            body.decode("utf-8")
            if isinstance(body, bytes) and content_type.startswith("text/html")
            else body
        )
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
        self.send_sizes = []
        self.blocking = []
        self.timeouts = []
        self.close_count = 0

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
        self.send_sizes.append(len(buffer))
        value = self.sends.pop(0) if self.sends else len(buffer)
        if isinstance(value, Exception):
            raise value
        return min(int(value), len(buffer))

    def close(self):
        self.close_count += 1


def _runtime_config():
    docs_root = Path(__file__).resolve().parent / "fixtures" / "sensor_switch"
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
    sock = _ScriptedSocket(
        sends=(OSError(web_runtime.EAGAIN), OSError(web_runtime.ETIMEDOUT), 2, 3)
    )
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    assert web_runtime._send_bounded_bytes(sock, b"hello") == 5
    assert sock.blocking == [False]
    assert sock.timeouts == [0]

    stalled = _ScriptedSocket(sends=(0,))
    try:
        web_runtime._send_bounded_bytes(stalled, b"hello")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ECONNRESET
    else:
        raise AssertionError("zero-progress send did not fail")


def test_bounded_send_fails_closed_when_zero_timeout_is_rejected():
    class _RejectZeroTimeoutSocket(_ScriptedSocket):
        def settimeout(self, value):
            self.timeouts.append(value)
            raise OSError("timeout control unavailable")

    sock = _RejectZeroTimeoutSocket()

    try:
        web_runtime._send_bounded_bytes(sock, b"hello")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ETIMEDOUT
        assert "nonblocking_unavailable" in str(exc)
        assert sock.blocking == [False]
        assert sock.timeouts == [0]
        assert sock.send_sizes == []
    else:
        raise AssertionError("response write used a socket without zero timeout")


def test_bounded_send_limits_chunk_size_and_paces_progress(monkeypatch):
    sock = _ScriptedSocket()
    pauses = []
    monkeypatch.setattr(web_runtime, "sleep", lambda seconds: pauses.append(seconds))

    assert web_runtime._send_bounded_bytes(sock, bytes(2500)) == 2500
    assert sock.send_sizes == [
        256,
        256,
        256,
        256,
        256,
        256,
        256,
        256,
        256,
        196,
    ]
    assert pauses == [0.025] * 9
    assert sock.blocking == [False]
    assert sock.timeouts == [0]


def test_bounded_send_expires_would_block_client(monkeypatch):
    sock = _ScriptedSocket(
        sends=(OSError(web_runtime.EAGAIN), OSError(web_runtime.EAGAIN))
    )
    times = iter((0.0, 0.1, 1.0))
    monkeypatch.setattr(web_runtime, "monotonic", lambda: next(times))
    monkeypatch.setattr(web_runtime, "sleep", lambda _seconds: None)

    try:
        web_runtime._send_bounded_bytes(sock, b"hello")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ETIMEDOUT
        assert "web_response_send_timeout" in str(exc)
        assert "elapsed_ms=1000" in str(exc)
    else:
        raise AssertionError("stalled send did not expire")


def test_bounded_send_resets_stall_deadline_on_progress(monkeypatch):
    sock = _ScriptedSocket(sends=(1, 1, 1))
    times = iter((0.0, 0.5, 0.5, 1.4, 1.4, 2.3, 2.3))
    monkeypatch.setattr(web_runtime, "monotonic", lambda: next(times))

    assert web_runtime._send_bounded_bytes(sock, b"abc") == 3


def test_bounded_send_enforces_five_second_total_deadline(monkeypatch):
    sock = _ScriptedSocket(sends=(1, 1, 1, 1, 1))
    times = iter(
        (0.0, 0.9, 0.9, 1.8, 1.8, 2.7, 2.7, 3.6, 3.6, 4.5, 4.5, 5.0)
    )
    monkeypatch.setattr(web_runtime, "monotonic", lambda: next(times))

    try:
        web_runtime._send_bounded_bytes(sock, b"abcdef")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ETIMEDOUT
    else:
        raise AssertionError("progressing send exceeded its total deadline")


def test_bounded_adapters_wrap_real_server_shapes():
    class _ServerShape:
        def _receive_header_bytes(self, sock):
            return b"base"

    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    assert web_runtime._bounded_server_class(_ServerShape) is not _ServerShape
    assert web_runtime._bounded_response_class(_ResponseShape) is not _ResponseShape


def test_bounded_server_adds_stage_to_bad_descriptor():
    class _ServerShape:
        def __init__(self):
            self._buffer = bytearray(32)
            self._timeout = 1

        def _receive_header_bytes(self, sock):
            return b"base"

        def _receive_body_bytes(self, sock, received, content_length):
            return received

    server = web_runtime._bounded_server_class(_ServerShape)()
    header_sock = _ScriptedSocket(reads=(OSError(web_runtime.EBADF),))
    body_sock = _ScriptedSocket(reads=(OSError(web_runtime.EBADF),))

    try:
        server._receive_header_bytes(header_sock)
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.EBADF
        assert web_runtime._socket_error_stage(exc) == "header_receive"
    else:
        raise AssertionError("header EBADF did not include stage context")

    try:
        server._receive_body_bytes(body_sock, b"", 1)
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.EBADF
        assert web_runtime._socket_error_stage(exc) == "body_receive"
    else:
        raise AssertionError("body EBADF did not include stage context")


def test_inactive_page_renderers_are_removed_from_module_cache(monkeypatch):
    active = object()
    inactive_status = object()
    inactive_automation = object()
    features_package = SimpleNamespace(
        web_status_ui=inactive_status,
        web_config_ui=active,
        web_automation_ui=inactive_automation,
    )
    modules = {
        "cpynodus_ii.features": features_package,
        "cpynodus_ii.features.web_status_ui": inactive_status,
        "cpynodus_ii.features.web_config_ui": active,
        "cpynodus_ii.features.web_automation_ui": inactive_automation,
    }
    collections = []
    monkeypatch.setattr(web_runtime, "sys", SimpleNamespace(modules=modules))
    monkeypatch.setattr(web_runtime.gc, "collect", lambda: collections.append(True))

    released = web_runtime._release_inactive_web_renderers(
        "cpynodus_ii.features.web_config_ui"
    )

    assert released == 2
    assert modules["cpynodus_ii.features.web_config_ui"] is active
    assert "cpynodus_ii.features.web_status_ui" not in modules
    assert "cpynodus_ii.features.web_automation_ui" not in modules
    assert not hasattr(features_package, "web_status_ui")
    assert features_package.web_config_ui is active
    assert not hasattr(features_package, "web_automation_ui")
    assert collections == [True]


def test_bounded_response_timeout_identifies_request_path(monkeypatch):
    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    response_cls = web_runtime._bounded_response_class(_ResponseShape)
    response = response_cls()
    sock = _ScriptedSocket()
    response._request = _FakeRequest(path="/setup")
    response._size = 0

    def fail_send(_conn, _buffer):
        raise OSError(
            web_runtime.ETIMEDOUT,
            "web_response_send_timeout sent=256/8705 chunk=2 elapsed_ms=1000",
        )

    monkeypatch.setattr(web_runtime, "_send_bounded_bytes", fail_send)

    try:
        response._send_bytes(sock, b"body")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.ETIMEDOUT
        assert "path=/setup" in str(exc)
        assert "sent=256/8705" in str(exc)
        assert "elapsed_ms=1000" in str(exc)
        assert sock.blocking == [False]
        assert sock.close_count == 1
    else:
        raise AssertionError("response timeout did not propagate diagnostics")


def test_bounded_response_closes_client_after_unexpected_send_error(monkeypatch):
    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    response_cls = web_runtime._bounded_response_class(_ResponseShape)
    response = response_cls()
    sock = _ScriptedSocket()
    response._request = _FakeRequest(path="/setup")
    response._size = 0

    def fail_send(_conn, _buffer):
        raise RuntimeError("unexpected send failure")

    monkeypatch.setattr(web_runtime, "_send_bounded_bytes", fail_send)

    try:
        response._send_bytes(sock, b"body")
    except RuntimeError as exc:
        assert str(exc) == "unexpected send failure"
        assert sock.blocking == [False]
        assert sock.close_count == 1
    else:
        raise AssertionError("unexpected send failure did not propagate")


def test_bounded_response_adds_stage_and_path_to_send_bad_descriptor(monkeypatch):
    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    response_cls = web_runtime._bounded_response_class(_ResponseShape)
    response = response_cls()
    sock = _ScriptedSocket()
    response._request = _FakeRequest(path="/current-data")
    response._size = 0

    monkeypatch.setattr(
        web_runtime,
        "_send_bounded_bytes",
        lambda _conn, _buffer: (_ for _ in ()).throw(OSError(web_runtime.EBADF)),
    )

    try:
        response._send_bytes(sock, b"body")
    except OSError as exc:
        assert web_runtime._socket_error_number(exc) == web_runtime.EBADF
        assert web_runtime._socket_error_stage(exc) == "response_send"
        assert "path=/current-data" in str(exc)
        assert sock.close_count == 1
    else:
        raise AssertionError("response EBADF did not include stage context")


def test_bounded_response_closes_client_nonblocking_after_send():
    class _ResponseShape:
        def _send_bytes(self, conn, buffer):
            return None

    response_cls = web_runtime._bounded_response_class(_ResponseShape)
    response = response_cls()
    sock = _ScriptedSocket()
    response._request = SimpleNamespace(connection=sock)

    response._close_connection()

    assert sock.blocking == [False]
    assert sock.timeouts == []
    assert sock.close_count == 1

    response._close_connection()
    assert sock.close_count == 1


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


def test_web_runtime_collects_after_successful_response(monkeypatch):
    class _SuccessfulResponseServer(_FakeServer):
        def poll(self):
            self.poll_count += 1
            return "request_handled_response_sent"

    class _SuccessfulResponseModule(_FakeServerModule):
        Server = _SuccessfulResponseServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    collections = []
    events = []
    monkeypatch.setattr(web_runtime.gc, "collect", lambda: collections.append(True))
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.202.4",
        server_module=_SuccessfulResponseModule,
        event_logger=events.append,
    ).start()
    controller._response_timeout_count = 1

    controller.poll()

    assert collections == [True]
    assert controller._response_timeout_count == 0
    assert controller.phase == "ready"
    assert events == []


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

    assert isinstance(response.raw_body, bytes)
    assert "Status" in html
    assert 'rel="icon" href="data:image/svg+xml;base64,PHN2Zy' in html
    assert "grid-template-columns:220px minmax(0,900px)" in html
    assert "th,td{width:50%" in html
    assert "NodusWeb" in html
    assert 'href="/" class="active"' in html
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


def test_config_page_payload_omits_values_unused_by_setup_renderer():
    runtime_config = _runtime_config()

    payload = build_config_page_payload(
        runtime_config,
        version="v0.26.202.1",
        page="setup",
    )

    assert "routes" not in payload
    assert "calibration_device" not in payload["sensor"]
    assert "broker_ip" not in payload["mqtt"]
    assert "base_topic" not in payload["mqtt"]
    assert tuple(payload["sensor"]["display_metrics"]) == tuple(
        runtime_config.sensor.display.metrics
    )


def test_config_renderer_collects_before_final_page_wrapper(monkeypatch):
    runtime_config = _runtime_config()
    payload = build_config_page_payload(
        runtime_config,
        version="v0.26.202.7",
        page="setup",
    )
    events = []

    monkeypatch.setattr(web_config_ui.gc, "collect", lambda: events.append("collect"))

    def wrapped_page(*args, **kwargs):
        events.append("render_page")
        return "complete"

    monkeypatch.setattr(web_config_ui, "render_page", wrapped_page)

    assert web_config_ui.render_config_html("setup", payload) == "complete"
    assert events == ["collect", "collect", "collect", "render_page"]


def test_calibration_labels_expand_device_abbreviations():
    assert web_config_ui._calibration_label("TEMP_OFFSET") == "Temperature Offset"
    assert (
        web_config_ui._calibration_label("RH_OFFSET")
        == "Relative Humidity Offset"
    )
    assert web_config_ui._calibration_label("CO2_OFFSET") == "CO2 Offset"
    assert (
        web_config_ui._calibration_label("SOIL_MOIST_CAL_VAL")
        == "Soil Moisture"
    )


def test_shared_page_wrapper_collects_before_document_allocation(monkeypatch):
    collections = []
    monkeypatch.setattr(
        web_ui_common.gc, "collect", lambda: collections.append(True)
    )

    html = web_ui_common.render_page("nodus", "Setup", "body")

    assert collections == [True]
    assert html.startswith("<!doctype html>")
    assert "<section class=\"panel\">body</section>" in html


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
    assert "grid-template-columns:1fr 1fr" in setup
    assert (
        '<div class="f loc"><label>Location</label>'
        '<input id="location"' in setup
    )
    assert (
        '<div class="g"><div class="f">'
        '<label>Metric 1</label><input id="metric_1"' in setup
    )
    assert (
        '<label>Metric 6</label><input id="metric_6"' in setup
        and '<label>Style 6</label><input id="style_6"' in setup
    )
    assert setup.count('class="f"') == 12
    assert len(setup.encode("utf-8")) < 8900
    assert "Device Calibration" in calibration
    assert "grid-template-columns:1fr 1fr" in calibration
    assert ".cal{grid-column:1}" in calibration
    assert (
        '<div class="g"><div class="cal">'
        '<label>Temperature Offset</label><input id="cal_TEMP_OFFSET"'
        in calibration
    )
    assert (
        '<label>Relative Humidity Offset</label>'
        '<input id="cal_RH_OFFSET"' in calibration
    )
    assert "AQI Offset" in calibration
    assert "AQI OFFSET" not in calibration
    assert len(calibration.encode("utf-8")) < 6050
    assert "Switch Settings" in switch
    assert "grid-template-columns:1fr 1fr" in switch
    assert ".ctl input{width:90%}" in switch
    assert (
        '<div class="loc"><label>Location</label>'
        '<input id="switch_location"' in switch
    )
    assert (
        '<div class="g"><div class="sf"><label>SWITCH_1</label>'
        '<div class="ctl"><button class="on"' in switch
    )
    assert (
        '<div class="sf"><label>SWITCH_2</label><div class="ctl">'
        in switch
    )
    assert (
        'class="on" data-channel="S1-x943fm" data-state="true" '
        'onclick="toggleSwitch(this)">ON</button>'
        '<input id="SWITCH_1_label"' in switch
    )
    assert (
        'class="off" data-channel="S2-x943fm" data-state="false" '
        'onclick="toggleSwitch(this)">OFF</button>'
        '<input id="SWITCH_2_label"' in switch
    )
    assert "Switch ID" not in switch
    assert "Serial Number" not in switch
    assert "Enabled Channels" not in switch
    assert len(switch.encode("utf-8")) < 6450
    assert "Switch Automations" in automations
    assert 'id="automation_script"' in automations
    assert "loadAutomations()" in automations
    assert "Nodus Info" in info
    assert "WiFi Recovery Count" in info
    assert "Network Info" in info
    assert "Device Info" in info
    assert 'href="/setup" class="active"' in setup
    assert 'href="/calibration" class="active"' in calibration
    assert 'href="/switch-setup" class="active"' in switch
    assert 'href="/automations-ui" class="active"' in automations
    assert 'href="/info" class="active"' in info
    assert 'id="automation_script"' not in setup


def test_web_status_memory_error_returns_503(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.193.1",
        server_module=_FakeServerModule,
        event_logger=events.append,
    ).start()

    def fail_render(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(
        "cpynodus_ii.features.web_status_ui.render_status_html", fail_render
    )
    response = controller.server.routes[("/", ("GET",))](_FakeRequest())

    assert response.status == (503, "Service Unavailable")
    assert "retry" in response.body
    assert any(
        "phase=render_memory_error stage=html_render" in event for event in events
    )


def test_web_setup_payload_memory_error_reports_stage(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.202.6",
        server_module=_FakeServerModule,
        event_logger=events.append,
    ).start()

    def fail_payload(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(web_runtime, "build_config_page_payload", fail_payload)
    response = controller.server.routes[("/setup", ("GET",))](_FakeRequest())

    assert response.status == (503, "Service Unavailable")
    assert any(
        "phase=render_memory_error stage=payload_build" in event for event in events
    )


def test_web_setup_encoding_memory_error_reports_stage(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.202.6",
        server_module=_FakeServerModule,
        event_logger=events.append,
    ).start()

    class _UnencodableHtml:
        def __len__(self):
            return 12

        def encode(self, _encoding):
            raise MemoryError

    monkeypatch.setattr(
        "cpynodus_ii.features.web_config_ui.render_config_html",
        lambda *args, **kwargs: _UnencodableHtml(),
    )
    response = controller.server.routes[("/setup", ("GET",))](_FakeRequest())

    assert response.status == (503, "Service Unavailable")
    assert any(
        "phase=render_memory_error stage=utf8_encode" in event for event in events
    )


def test_web_renderer_error_returns_500_without_disabling_other_routes(monkeypatch):
    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.203.3",
        server_module=_FakeServerModule,
        event_logger=events.append,
    ).start()

    def fail_payload(*args, **kwargs):
        raise AttributeError("unsupported renderer method")

    monkeypatch.setattr(web_runtime, "build_config_page_payload", fail_payload)
    failed = controller.server.routes[("/calibration", ("GET",))](_FakeRequest())
    current = controller.server.routes[("/current-data", ("GET",))](_FakeRequest())

    assert failed.status == (500, "Internal Server Error")
    assert "serial log" in failed.body
    assert current.status == (200, "OK")
    assert controller.phase == "ready"
    assert any(
        "phase=render_failed type=AttributeError" in event for event in events
    )


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
    monkeypatch.setattr(web_runtime.gc, "mem_free", lambda: 9999, raising=False)

    status = controller.server.routes[("/", ("GET",))](_FakeRequest())
    setup = controller.server.routes[("/setup", ("GET",))](_FakeRequest())

    assert status.status == (503, "Service Unavailable")
    assert setup.status == (503, "Service Unavailable")
    # Two admission collections and one inactive Status-renderer release when
    # switching to Setup.
    assert len(collections) == 3

    monkeypatch.setattr(web_runtime.gc, "mem_free", lambda: 10000, raising=False)
    status = controller.server.routes[("/", ("GET",))](_FakeRequest())
    setup = controller.server.routes[("/setup", ("GET",))](_FakeRequest())

    assert status.status == (200, "OK")
    assert setup.status == (200, "OK")
    # The successful pair adds one renderer release, two admission
    # collections, one Status wrapper collection, and four Setup
    # profile/display/renderer/wrapper collections. The prior guarded Setup did
    # not import its renderer, so Status has nothing to release first.
    assert len(collections) == 11


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


def test_web_runtime_collects_and_restarts_listener_after_two_send_timeouts(
    monkeypatch,
):
    class _TimedOutResponseServer(_FakeServer):
        def poll(self):
            self.poll_count += 1
            raise OSError(
                web_runtime.ETIMEDOUT,
                "web_response_send_timeout sent=0/8705 chunk=1",
            )

    class _TimedOutResponseModule(_FakeServerModule):
        Server = _TimedOutResponseServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    collections = []
    monkeypatch.setattr(web_runtime.gc, "collect", lambda: collections.append(True))
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.202.1",
        server_module=_TimedOutResponseModule,
        event_logger=events.append,
    ).start()

    controller.poll()
    assert controller.server.stop_count == 0
    assert controller._response_timeout_count == 1

    controller.poll()

    assert controller.phase == "ready"
    assert controller.errors == ()
    assert controller.server.stop_count == 1
    assert controller.server.started == ("0.0.0.0", 8000)
    assert controller._response_timeout_count == 0
    assert controller._listener_restart_count == 1
    assert len(collections) == 3
    assert sum("response phase=timeout_cleanup" in event for event in events) == 2
    assert any(
        "listener phase=ready reason=response_timeouts restarts=1" in event
        for event in events
    )


def test_web_runtime_recovers_scoped_client_bad_descriptor():
    class _ClientBadDescriptorServer(_FakeServer):
        def poll(self):
            self.poll_count += 1
            raise web_runtime._socket_stage_error(
                OSError(web_runtime.EBADF), "response_send", path="/current-data"
            )

    class _ClientBadDescriptorModule(_FakeServerModule):
        Server = _ClientBadDescriptorServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.203.4",
        server_module=_ClientBadDescriptorModule,
        event_logger=events.append,
    ).start()

    controller.poll()

    assert controller.phase == "ready"
    assert controller.errors == ()
    assert controller.server.stop_count == 0
    assert any("web_socket_stage=response_send" in event for event in events)
    assert any("path=/current-data" in event for event in events)


def test_web_runtime_restarts_listener_after_unscoped_bad_descriptor():
    class _ListenerBadDescriptorServer(_FakeServer):
        def poll(self):
            self.poll_count += 1
            raise OSError(web_runtime.EBADF)

    class _ListenerBadDescriptorModule(_FakeServerModule):
        Server = _ListenerBadDescriptorServer

    runtime_config = _runtime_config()
    stack = build_network_stack(
        runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr
    )
    events = []
    controller = WebRuntimeController(
        runtime_config,
        stack,
        version="v0.26.203.4",
        server_module=_ListenerBadDescriptorModule,
        event_logger=events.append,
    ).start()

    controller.poll()

    assert controller.phase == "ready"
    assert controller.errors == ()
    assert controller.server.stop_count == 1
    assert controller.server.started == ("0.0.0.0", 8000)
    assert controller._listener_restart_count == 1
    assert any(
        "listener phase=ready reason=bad_descriptor restarts=1" in event
        for event in events
    )


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
