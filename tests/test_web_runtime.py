"""Tests for web runtime startup and polling behavior."""

from pathlib import Path
from tempfile import TemporaryDirectory

from cpynodus_ii.core.network import build_network_stack
from cpynodus_ii.core.settings import Settings
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

    def start(self, host, port):
        self.started = (host, port)

    def route(self, path, methods=None):
        def _decorator(fn):
            self.routes[(path, tuple(methods or ()))] = fn
            return fn

        return _decorator

    def poll(self):
        self.poll_count += 1


class _FakeServerModule:
    Server = _FakeServer
    Response = _FakeResponse
    JSONResponse = _FakeJSONResponse


def _runtime_config():
    docs_root = Path(__file__).resolve().parents[1] / "docs" / "sensor+switch"
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for name in ("settings.toml", "sensor_i2c.toml", "switch.toml"):
            (tmpdir_path / name).write_text((docs_root / name).read_text(), encoding="utf-8")
        runtime_config = Settings.from_directory(tmpdir_path).runtime_config()
        runtime_config.active_profile = "nodusweb"
        return runtime_config


def test_ap_mode_network_stack_includes_socket_artifacts_for_web_runtime():
    runtime_config = _runtime_config()
    runtime_config.ap_mode = True

    stack = build_network_stack(runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr)

    assert stack.phase == "ap"
    assert stack.socket_pool["kind"] == "socketpool"
    assert stack.ssl_context["kind"] == "ssl"


def test_web_runtime_controller_registers_and_polls_routes():
    runtime_config = _runtime_config()
    stack = build_network_stack(runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr)

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


def test_web_runtime_controller_config_route_updates_runtime_config():
    runtime_config = _runtime_config()
    stack = build_network_stack(runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr)
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


def test_web_runtime_controller_switch_route_applies_live_override():
    runtime_config = _runtime_config()
    stack = build_network_stack(runtime_config, wifi_radio=_FakeRadio(), connection_manager_module=_FakeConnMgr)

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
    assert controller.runtime_config.switch.channels[0].last_state is True
