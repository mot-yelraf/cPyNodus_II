# ruff: noqa: E501
"""Integrate the lightweight web server with runtime handlers and state.

This module starts and polls the constrained web runtime, dispatches requests
to handler helpers, and reports availability back to the main application loop.
"""

import gc
import json
from errno import EAGAIN, ECONNRESET, ETIMEDOUT
from time import localtime, monotonic, sleep

from cpynodus_ii.features.web_handlers import (
    build_setup_payload,
    build_status_payload,
    handle_switch_state_request,
    handle_web_config_request,
)
from cpynodus_ii.features.web_routes import route_paths
from cpynodus_ii.features.web_services import (
    apply_itaot_init_payload,
    build_itaot_meta_payload,
)

_HTTP_STATUS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

_MANUAL_SWITCH_GUARD_S = 5.0
_STATUS_HEAP_FLOOR = 16000
_CONFIG_HEAP_FLOOR = 24000
_HEADER_READ_WINDOW_S = 0.25
_BODY_READ_WINDOW_S = 0.5
_RESPONSE_SEND_WINDOW_S = 1.0
_SOCKET_RETRY_DELAY_S = 0.005
_MAX_HEADER_BYTES = 2048


class WebRuntimeController:
    """Bridge route helpers onto an `adafruit_httpserver` server."""

    def __init__(
        self,
        runtime_config,
        network_stack,
        *,
        version,
        sensor_service=None,
        sensor_snapshot=None,
        switch_service=None,
        automation_service=None,
        settings_root=None,
        server_module=None,
        reboot_callbacks=None,
        event_logger=None,
        wifi_recovery_count=0,
    ):
        self.runtime_config = runtime_config
        self.network_stack = network_stack
        self.version = version
        self.sensor_service = sensor_service
        self.switch_service = switch_service
        self.automation_service = automation_service
        self.settings_root = settings_root
        self.server_module = server_module
        self.reboot_callbacks = dict(reboot_callbacks or {})
        self.event_logger = event_logger
        self.wifi_recovery_count = int(wifi_recovery_count or 0)
        self.server = None
        self.phase = "new"
        self.errors = ()
        self._route_paths = ()
        self._pending_reboot_callback = None
        self._manual_switch_guard_until = {}
        self._latest_sensor_snapshot = sensor_snapshot
        self._latest_sample_timestamp = (
            _sample_timestamp_text()
            if getattr(sensor_snapshot, "phase", "") == "ready"
            else ""
        )
        self._response_cls = None
        self._json_response_cls = None

    def start(self):
        """Initialize the backing HTTP server and register routes."""
        if not getattr(self.runtime_config, "web_enabled", False):
            self.phase = "disabled"
            return self
        if getattr(self.network_stack, "socket_pool", None) is None:
            self.phase = "unavailable"
            self.errors = ("web_socket_pool_unavailable",)
            return self
        module = self._resolve_server_module()
        if module is None:
            self.phase = "unavailable"
            self.errors = ("adafruit_httpserver_unavailable",)
            return self
        self.server_module = module
        server = self._build_server(module)
        if server is None:
            self.phase = "error"
            self.errors = ("web_server_init_failed",)
            return self
        self.server = server
        self._register_routes()
        self.phase = "ready"
        return self

    def update_context(
        self,
        *,
        runtime_config=None,
        network_stack=None,
        sensor_service=None,
        sensor_snapshot=None,
        switch_service=None,
        automation_service=None,
        version=None,
        wifi_recovery_count=None,
    ):
        """Refresh mutable runtime context used by handlers."""
        if runtime_config is not None:
            self.runtime_config = runtime_config
        if network_stack is not None:
            self.network_stack = network_stack
        if sensor_service is not None:
            self.sensor_service = sensor_service
        if (
            sensor_snapshot is not None
            and sensor_snapshot is not self._latest_sensor_snapshot
            and getattr(sensor_snapshot, "phase", "") == "ready"
        ):
            self._latest_sensor_snapshot = sensor_snapshot
            self._latest_sample_timestamp = _sample_timestamp_text()
        if switch_service is not None:
            self.switch_service = switch_service
        if automation_service is not None:
            self.automation_service = automation_service
        if version is not None:
            self.version = version
        if wifi_recovery_count is not None:
            self.wifi_recovery_count = int(wifi_recovery_count or 0)

    def poll(self):
        """Poll the server once if active."""
        if self.phase != "ready" or self.server is None:
            return self
        poll = getattr(self.server, "poll", None)
        if not callable(poll):
            self.phase = "error"
            self.errors = ("web_server_poll_unavailable",)
            self._log_web_event(
                "phase=error reason=web_server_poll_unavailable server={} free_mem={}".format(
                    _server_state_text(self.server), _free_mem_text()
                )
            )
            return self
        try:
            poll()
        except Exception as exc:
            if _is_recoverable_web_poll_error(exc):
                self.errors = ()
                if not _is_tls_client_hello_error(exc):
                    self._log_web_event(
                        "phase=ready reason=web_poll_recovered type={} detail={} server={} free_mem={}".format(
                            type(exc).__name__,
                            self._error_text(exc),
                            _server_state_text(self.server),
                            _free_mem_text(),
                        )
                    )
                return self
            self.phase = "error"
            self.errors = ("web_poll_failed", str(exc))
            self._log_web_event(
                "phase=error reason=web_poll_failed type={} detail={} server={} free_mem={}".format(
                    type(exc).__name__,
                    self._error_text(exc),
                    _server_state_text(self.server),
                    _free_mem_text(),
                )
            )
            return self
        self._run_pending_reboot()
        return self

    def stop(self):
        """Stop the backing HTTP server if the implementation supports it."""
        server = self.server
        if server is None:
            self.phase = "stopped"
            return False
        stopped = False
        for method_name in ("stop", "deinit", "close"):
            method = getattr(server, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                stopped = True
                break
            except Exception as exc:
                self.phase = "error"
                self.errors = ("web_server_stop_failed", str(exc))
                return False
        self.server = None
        self.phase = "stopped"
        return stopped

    @property
    def route_paths(self):
        return self._route_paths

    def _resolve_server_module(self):
        if self.server_module is not None:
            return self.server_module
        try:
            import adafruit_httpserver as module  # type: ignore
        except ImportError:
            return None
        return module

    def _build_server(self, module):
        server_cls = getattr(module, "Server", None)
        if server_cls is None:
            return None
        server_cls = _bounded_server_class(server_cls)
        self._response_cls = _bounded_response_class(
            getattr(module, "Response", None)
        )
        self._json_response_cls = _bounded_response_class(
            getattr(module, "JSONResponse", None)
        )
        try:
            server = server_cls(self.network_stack.socket_pool, debug=False)
        except TypeError:
            server = server_cls(self.network_stack.socket_pool)
        if hasattr(server, "headers"):
            server.headers = {
                "Access-Control-Allow-Origin": "*",
                "Connection": "close",
                "Cache-Control": "no-store",
            }
        if hasattr(server, "socket_timeout"):
            server.socket_timeout = 1
        if hasattr(server, "request_buffer_size"):
            server.request_buffer_size = 2048
        start = getattr(server, "start", None)
        if callable(start):
            start("0.0.0.0", int(self.runtime_config.network.http_port or 8000))
        return server

    def _register_routes(self):
        self._route_paths = tuple(route_paths(self.runtime_config))
        route = getattr(self.server, "route", None)
        if not callable(route):
            self.phase = "error"
            self.errors = ("web_route_registration_unavailable",)
            return

        @route("/", methods=["GET"])
        def _root(request):
            return self._status_page_response(request)

        @route("/current-data", methods=["GET"])
        def _current_data(request):
            payload = self._build_status_payload()
            return self._json_response(request, payload)

        @route("/setup", methods=["GET"])
        def _setup(request):
            return self._config_page_response(request, "setup")

        @route("/calibration", methods=["GET"])
        def _calibration(request):
            return self._config_page_response(request, "calibration")

        @route("/info", methods=["GET"])
        def _info(request):
            return self._config_page_response(request, "info")

        if "/switch-setup" in self._route_paths:

            @route("/switch-setup", methods=["GET"])
            def _switch_setup(request):
                return self._config_page_response(request, "switch")

        if "/automations-ui" in self._route_paths:

            @route("/automations-ui", methods=["GET"])
            def _automations_ui(request):
                return self._automation_page_response(request)

        @route("/config", methods=["POST"])
        def _config(request):
            updates = _extract_updates(request)
            if updates is None:
                return self._json_response(
                    request,
                    {"success": False, "error": "invalid_config_payload"},
                    status_code=400,
                )
            payload = handle_web_config_request(
                self.runtime_config,
                updates,
                settings_root=self.settings_root,
            )
            self.runtime_config = payload.pop("runtime_config")
            return self._json_response(request, payload)

        @route("/set-switch-state", methods=["POST"])
        def _set_switch_state(request):
            body = _parse_json_body(request)
            if body is None:
                return self._json_response(
                    request,
                    {"success": False, "error": "invalid_json"},
                    status_code=400,
                )
            state = body.get("state")
            if state is None:
                return self._json_response(
                    request,
                    {"success": False, "error": "state_required"},
                    status_code=400,
                )
            channel_id = str(body.get("channel_id", "") or "").strip()
            channel_key = str(body.get("channel_key", "") or "").strip()
            owners = ()
            if self.automation_service is not None:
                owners = self.automation_service.channel_controlled(
                    channel_id=channel_id or None,
                    channel_key=channel_key or None,
                )
            if owners:
                return self._json_response(
                    request,
                    {
                        "success": False,
                        "error": "switch_controlled_by_automation",
                        "automations": list(owners),
                    },
                    status_code=409,
                )
            channel_token = _manual_switch_channel_token(
                self.switch_service,
                channel_id=channel_id,
                channel_key=channel_key,
            )
            now = monotonic()
            guard_until = float(
                self._manual_switch_guard_until.get(channel_token, 0.0) or 0.0
            )
            if channel_token and now < guard_until:
                return self._json_response(
                    request,
                    {
                        "success": False,
                        "error": "switch_manual_guard_active",
                        "retry_after_seconds": max(1, int(guard_until - now + 0.999)),
                        "guard_seconds": int(_MANUAL_SWITCH_GUARD_S),
                    },
                    status_code=429,
                )
            if channel_token:
                self._manual_switch_guard_until[channel_token] = (
                    now + _MANUAL_SWITCH_GUARD_S
                )
            payload = handle_switch_state_request(
                self.runtime_config,
                self.switch_service,
                channel_id=channel_id or None,
                channel_key=channel_key or None,
                state=_coerce_bool(state),
                settings_root=self.settings_root,
            )
            self.runtime_config = payload.pop("runtime_config")
            if not payload.get("success") and channel_token:
                self._manual_switch_guard_until.pop(channel_token, None)
            payload["guard_seconds"] = int(_MANUAL_SWITCH_GUARD_S)
            return self._json_response(request, payload)

        if (
            self.runtime_config.active_profile == "nodusweb"
            and self.automation_service is not None
        ):

            @route("/automations", methods=["GET"])
            def _automations_get(request):
                return self._json_response(request, self.automation_service.payload())

            @route("/automations", methods=["POST"])
            def _automations_save(request):
                body = _parse_json_body(request)
                if body is None:
                    return self._json_response(
                        request,
                        {"success": False, "error": "invalid_json"},
                        status_code=400,
                    )
                payload = self.automation_service.save_rule(
                    body.get("rule_id"),
                    body.get("enabled", False),
                    body.get("script"),
                )
                status = 200 if payload.get("success") else 400
                return self._json_response(request, payload, status_code=status)

            @route("/automations/delete", methods=["POST"])
            def _automations_delete(request):
                body = _parse_json_body(request)
                if body is None:
                    return self._json_response(
                        request,
                        {"success": False, "error": "invalid_json"},
                        status_code=400,
                    )
                payload = self.automation_service.delete_rule(body.get("rule_id"))
                status = 200 if payload.get("success") else 400
                return self._json_response(request, payload, status_code=status)

        @route("/restart", methods=["POST"])
        def _restart(request):
            body = _parse_json_body(request) or {}
            mode = str(body.get("mode", "soft") or "soft").strip().lower()
            callback = self.reboot_callbacks.get(mode)
            if callback is None:
                return self._json_response(
                    request,
                    {"success": False, "error": "unsupported_restart_mode"},
                    status_code=400,
                )
            callback()
            return self._json_response(request, {"success": True, "restart": mode})

        if "/itaot-init" in self._route_paths:

            @route("/itaot-init", methods=["POST"])
            def _itaot_init(request):
                self._log_ap_event("request path=/itaot-init phase=received")
                payload = _parse_json_body(request)
                if payload is None:
                    self._log_ap_event(
                        "request path=/itaot-init phase=rejected status=400 "
                        "error=invalid_json"
                    )
                    return self._json_response(
                        request,
                        {"success": False, "error": "invalid_json"},
                        status_code=400,
                    )
                time_present = 0
                if isinstance(payload, dict) and isinstance(payload.get("time"), dict):
                    time_present = 1
                self._log_ap_event(
                    "request path=/itaot-init phase=parsed time_present={}".format(
                        time_present
                    )
                )
                try:
                    result = apply_itaot_init_payload(
                        payload,
                        self.runtime_config,
                        settings_root=self.settings_root or ".",
                        event_logger=self._log_itaot_apply_event,
                    )
                except Exception as exc:
                    self._log_ap_event(
                        "request path=/itaot-init phase=apply_exception type={} "
                        "detail={}".format(type(exc).__name__, self._error_text(exc))
                    )
                    self._log_ap_event(
                        "request path=/itaot-init phase=response_return status=500"
                    )
                    return self._json_response(
                        request,
                        {
                            "success": False,
                            "accepted": False,
                            "error": "itaot_init_exception",
                            "exception": type(exc).__name__,
                        },
                        status_code=500,
                    )
                self.runtime_config = result.runtime_config
                time_keys = tuple(
                    str(update.get("key", "") or "")
                    for update in result.applied_updates
                    if str(update.get("section", "") or "") == "Time"
                )
                self._log_ap_event(
                    "request path=/itaot-init phase=applied status={} accepted={} "
                    "rebooting={} updates={} time_updates={} time_keys={} "
                    "errors={}".format(
                        result.status_code,
                        1 if result.accepted else 0,
                        1 if result.rebooting else 0,
                        len(result.applied_updates),
                        len(time_keys),
                        ",".join(time_keys) if time_keys else "none",
                        ",".join(result.errors) if result.errors else "none",
                    )
                )
                if result.accepted:
                    callback = self.reboot_callbacks.get(
                        "hard"
                    ) or self.reboot_callbacks.get("soft")
                    if callback is not None:
                        self._pending_reboot_callback = callback
                        self._log_ap_event(
                            "request path=/itaot-init phase=reboot_scheduled"
                        )
                self._log_ap_event(
                    "request path=/itaot-init phase=response_return status={}".format(
                        result.status_code
                    )
                )
                return self._json_response(
                    request, result.body, status_code=result.status_code
                )

            @route("/itaot-meta", methods=["GET"])
            def _itaot_meta(request):
                self._log_ap_event("request path=/itaot-meta phase=received")
                payload = build_itaot_meta_payload(
                    self.runtime_config,
                    version=self.version,
                    ip_address=getattr(self.network_stack, "ip_address", ""),
                    switch_states={},
                )
                if self.switch_service is not None:
                    from cpynodus_ii.features.switch_service import (
                        snapshot_switch_states,
                    )

                    payload = build_itaot_meta_payload(
                        self.runtime_config,
                        version=self.version,
                        ip_address=getattr(self.network_stack, "ip_address", ""),
                        switch_states=snapshot_switch_states(self.switch_service),
                    )
                self._log_ap_event("request path=/itaot-meta phase=response status=200")
                return self._json_response(request, payload)

    def _status_page_response(self, request):
        """Render only current data and switch controls for the initial page."""
        if not _heap_available(_STATUS_HEAP_FLOOR):
            return self._web_unavailable_response(request)
        try:
            from cpynodus_ii.features.web_status_ui import render_status_html

            return self._html_response(
                request, render_status_html(self._build_status_payload())
            )
        except MemoryError:
            return self._web_unavailable_response(request)

    def _config_page_response(self, request, page):
        """Lazily render one configuration or information page."""
        if not _heap_available(_CONFIG_HEAP_FLOOR):
            return self._web_unavailable_response(request)
        try:
            from cpynodus_ii.features.web_config_ui import render_config_html

            setup = build_setup_payload(self.runtime_config, version=self.version)
            status = self._build_status_payload() if page == "info" else None
            return self._html_response(
                request, render_config_html(page, setup, status_payload=status)
            )
        except MemoryError:
            return self._web_unavailable_response(request)

    def _automation_page_response(self, request):
        """Lazily render the local automation editor."""
        if not _heap_available(_CONFIG_HEAP_FLOOR):
            return self._web_unavailable_response(request)
        try:
            from cpynodus_ii.features.web_automation_ui import (
                render_automation_html,
            )

            return self._html_response(
                request,
                render_automation_html(self.runtime_config.network.hostname),
            )
        except MemoryError:
            return self._web_unavailable_response(request)

    def _web_unavailable_response(self, request):
        return self._plain_response(
            request,
            "Web UI temporarily unavailable; retry shortly.",
            content_type="text/plain; charset=utf-8",
            status_code=503,
        )

    def _build_status_payload(self):
        """Return status from the latest main-loop sensor snapshot."""
        snapshot = self._latest_sensor_snapshot
        if self._latest_sensor_snapshot is not None and not self._latest_sample_timestamp:
            self._latest_sample_timestamp = _sample_timestamp_text()
        return build_status_payload(
            self.runtime_config,
            version=self.version,
            sensor_snapshot=snapshot,
            display_timestamp=self._latest_sample_timestamp,
            switch_service=self.switch_service,
            automation_service=self.automation_service,
            ip_address=getattr(self.network_stack, "ip_address", ""),
            wifi_recovery_count=self.wifi_recovery_count,
        )

    def _run_pending_reboot(self):
        callback = self._pending_reboot_callback
        if callback is None:
            return
        self._pending_reboot_callback = None
        self._log_ap_event("request path=/itaot-init phase=reboot_execute")
        callback()

    def _log_ap_event(self, message):
        network_phase = str(getattr(self.network_stack, "phase", "") or "")
        if not getattr(self.runtime_config, "ap_mode", False) and network_phase != "ap":
            return
        logger = self.event_logger
        if not callable(logger):
            return
        try:
            logger(message)
        except Exception:
            pass

    def _log_web_event(self, message):
        logger = self.event_logger
        if not callable(logger):
            return
        try:
            logger(message)
        except Exception:
            pass

    def _log_itaot_apply_event(self, message):
        self._log_ap_event("request path=/itaot-init {}".format(str(message or "")))

    @staticmethod
    def _error_text(exc):
        return str(exc or "").replace(" ", "_") or type(exc).__name__

    def _json_response(self, request, payload, *, status_code=200):
        response_cls = self._json_response_cls
        status = (status_code, _HTTP_STATUS.get(status_code, "OK"))
        if response_cls is not None:
            return response_cls(request, payload, status=status)
        return self._plain_response(
            request,
            json.dumps(payload),
            content_type="application/json",
            status_code=status_code,
        )

    def _html_response(self, request, html, *, status_code=200):
        return self._plain_response(
            request,
            html,
            content_type="text/html; charset=utf-8",
            status_code=status_code,
        )

    def _plain_response(self, request, body, *, content_type, status_code=200):
        response_cls = self._response_cls
        status = (status_code, _HTTP_STATUS.get(status_code, "OK"))
        return response_cls(request, body, content_type=content_type, status=status)


def _parse_json_body(request):
    try:
        raw = getattr(request, "body", b"")
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        text = str(raw or "").strip()
        if not text:
            return {}
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_updates(request):
    payload = _parse_json_body(request)
    if payload is None:
        return None
    if isinstance(payload.get("updates"), list):
        return tuple(payload.get("updates", ()))
    if {"section", "key"} <= set(payload.keys()):
        return (payload,)
    return None


def _coerce_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def _manual_switch_channel_token(switch_service, *, channel_id="", channel_key=""):
    """Return one canonical channel token for the manual command guard."""
    channel_id = str(channel_id or "").strip()
    channel_key = str(channel_key or "").strip()
    for channel in getattr(switch_service, "channels", ()):
        if channel_id and channel.channel_id == channel_id:
            return str(channel.channel_id or channel.key or channel_id)
        if channel_key and channel.key == channel_key:
            return str(channel.channel_id or channel.key or channel_key)
    return channel_id or channel_key


def _free_mem_text():
    mem_free = getattr(gc, "mem_free", None)
    if not callable(mem_free):
        return "unknown"
    try:
        return str(mem_free())
    except Exception:
        return "unknown"


def _heap_available(floor):
    """Collect garbage and enforce a real pre-render heap floor when known."""
    try:
        gc.collect()
    except Exception:
        pass
    mem_free = getattr(gc, "mem_free", None)
    if not callable(mem_free):
        return True
    try:
        return int(mem_free()) >= int(floor)
    except Exception:
        return True


def _server_state_text(server):
    if server is None:
        return "none"
    items = ["present"]
    for name in ("request_buffer_size", "socket_timeout"):
        value = getattr(server, name, None)
        if value is not None:
            items.append("{}={}".format(name, value))
    return ";".join(items)


def _is_recoverable_web_poll_error(exc):
    if isinstance(exc, ValueError):
        return "Unparseable raw_request" in str(exc or "")
    if isinstance(exc, OSError):
        return _socket_error_number(exc) in (EAGAIN, ECONNRESET, ETIMEDOUT)
    return False


def _is_tls_client_hello_error(exc):
    """Identify an HTTPS handshake sent to the plain HTTP listener."""
    if not _is_recoverable_web_poll_error(exc):
        return False
    pending = list(getattr(exc, "args", ()) or ())
    while pending:
        value = pending.pop()
        if isinstance(value, bytes):
            return len(value) >= 3 and value[0] == 0x16 and value[1] == 0x03
        if isinstance(value, (tuple, list)):
            pending.extend(value)
    return False


def _bounded_server_class(server_cls):
    """Add bounded nonblocking request reads to the Adafruit server."""
    if not callable(getattr(server_cls, "_receive_header_bytes", None)):
        return server_cls

    class BoundedServer(server_cls):
        def _receive_header_bytes(self, sock):
            return _receive_bounded_header(self, sock)

        def _receive_body_bytes(self, sock, received_body_bytes, content_length):
            return _receive_bounded_body(
                self, sock, received_body_bytes, content_length
            )

    return BoundedServer


def _bounded_response_class(response_cls):
    """Bound response writes so a stopped client cannot hold the main loop."""
    if response_cls is None or not callable(
        getattr(response_cls, "_send_bytes", None)
    ):
        return response_cls

    class BoundedResponse(response_cls):
        def _send_bytes(self, conn, buffer):
            sent = _send_bounded_bytes(conn, buffer)
            self._size += sent

    return BoundedResponse


def _receive_bounded_header(server, sock):
    """Read one complete HTTP header without blocking the cooperative loop."""
    received = bytes()
    deadline = monotonic() + _HEADER_READ_WINDOW_S
    _set_socket_nonblocking(sock)
    try:
        while b"\r\n\r\n" not in received and len(received) < _MAX_HEADER_BYTES:
            try:
                length = sock.recv_into(server._buffer, len(server._buffer))
            except OSError as exc:
                error = _socket_error_number(exc)
                if error == ETIMEDOUT:
                    break
                if error != EAGAIN:
                    raise
                if monotonic() >= deadline:
                    break
                sleep(_SOCKET_RETRY_DELAY_S)
                continue
            if not length:
                break
            received += server._buffer[:length]
            if len(received) >= 2 and received[0] == 0x16 and received[1] == 0x03:
                break
            if monotonic() >= deadline:
                break
    finally:
        _restore_socket_timeout(sock, getattr(server, "_timeout", 1))
    return received[:_MAX_HEADER_BYTES]


def _receive_bounded_body(server, sock, received, content_length):
    """Read a declared request body within a short fixed window."""
    deadline = monotonic() + _BODY_READ_WINDOW_S
    _set_socket_nonblocking(sock)
    try:
        while len(received) < content_length:
            try:
                length = sock.recv_into(server._buffer, len(server._buffer))
            except OSError as exc:
                error = _socket_error_number(exc)
                if error == ETIMEDOUT:
                    break
                if error != EAGAIN:
                    raise
                if monotonic() >= deadline:
                    break
                sleep(_SOCKET_RETRY_DELAY_S)
                continue
            if not length:
                break
            received += server._buffer[:length]
            if monotonic() >= deadline:
                break
    finally:
        _restore_socket_timeout(sock, getattr(server, "_timeout", 1))
    return received[:content_length]


def _send_bounded_bytes(conn, buffer):
    """Send bytes with retry bounds for nonblocking or stalled clients."""
    sent = 0
    view = memoryview(buffer)
    deadline = monotonic() + _RESPONSE_SEND_WINDOW_S
    while sent < len(buffer):
        if monotonic() >= deadline:
            raise OSError(ETIMEDOUT)
        try:
            count = conn.send(view[sent:])
        except OSError as exc:
            error = _socket_error_number(exc)
            if error == ECONNRESET:
                raise
            if error != EAGAIN:
                raise
            sleep(_SOCKET_RETRY_DELAY_S)
            continue
        if not count:
            raise OSError(ECONNRESET)
        sent += count
    return sent


def _set_socket_nonblocking(sock):
    setter = getattr(sock, "setblocking", None)
    if callable(setter):
        try:
            setter(False)
            return True
        except Exception:
            pass
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        try:
            setter(0)
            return True
        except Exception:
            pass
    return False


def _socket_error_number(exc):
    error = getattr(exc, "errno", None)
    if error is not None:
        return error
    args = getattr(exc, "args", ()) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _restore_socket_timeout(sock, timeout):
    setter = getattr(sock, "settimeout", None)
    if not callable(setter):
        return False
    try:
        setter(timeout)
        return True
    except Exception:
        return False


def _sample_timestamp_text():
    """Return the current RTC time when it has been synchronized."""
    try:
        parts = localtime()
        if int(parts[0]) < 2023:
            return ""
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            int(parts[0]),
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
        )
    except Exception:
        return ""
