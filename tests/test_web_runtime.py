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
    assert controller.runtime_config.switch.channels[0].last_state is True
