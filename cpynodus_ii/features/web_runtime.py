# ruff: noqa: E501
"""Integrate the lightweight web server with runtime handlers and state.

This module starts and polls the constrained web runtime, dispatches requests
to handler helpers, and reports availability back to the main application loop.
"""

import gc
import json
from time import localtime

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
    500: "Internal Server Error",
    503: "Service Unavailable",
}


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
        self._latest_sensor_snapshot = sensor_snapshot
        self._latest_sample_timestamp = (
            _sample_timestamp_text()
            if getattr(sensor_snapshot, "phase", "") == "ready"
            else ""
        )

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
            server.request_buffer_size = 1024
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
            return self._dashboard_response(request)

        @route("/current-data", methods=["GET"])
        def _current_data(request):
            payload = self._build_status_payload()
            return self._json_response(request, payload)

        @route("/setup", methods=["GET"])
        def _setup(request):
            return self._dashboard_response(request)

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
            payload = handle_switch_state_request(
                self.runtime_config,
                self.switch_service,
                channel_id=str(body.get("channel_id", "") or "").strip() or None,
                channel_key=str(body.get("channel_key", "") or "").strip() or None,
                state=_coerce_bool(state),
                settings_root=self.settings_root,
            )
            self.runtime_config = payload.pop("runtime_config")
            return self._json_response(request, payload)

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

    def _dashboard_response(self, request):
        """Render the Pico-sized dashboard with a bounded failure response."""
        try:
            setup = build_setup_payload(self.runtime_config, version=self.version)
            status = self._build_status_payload()
            return self._html_response(request, _render_dashboard_html(setup, status))
        except MemoryError:
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
        response_cls = getattr(self.server_module, "JSONResponse", None)
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
        response_cls = getattr(self.server_module, "Response", None)
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


def _free_mem_text():
    mem_free = getattr(gc, "mem_free", None)
    if not callable(mem_free):
        return "unknown"
    try:
        return str(mem_free())
    except Exception:
        return "unknown"


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
    if not isinstance(exc, ValueError):
        return False
    return "Unparseable raw_request" in str(exc or "")


def _html_escape(value):
    text = str(value if value is not None else "")
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text


def _render_dashboard_html(payload, status_payload):
    """Render the nodusweb dashboard without history or export features."""
    metric_rows = []
    for item in status_payload["sensor"]["display_metrics"]:
        metric_rows.append(
            '<tr><td>{}</td><td id="sample_{}">{}</td></tr>'.format(
                _html_escape(item["metric"]),
                _html_escape(item["index"]),
                _html_escape(_status_value(item.get("value"))),
            )
        )
    if not metric_rows:
        metric_rows.append("<tr><td colspan='2'>No display metrics configured</td></tr>")

    status_switch_rows = []
    switch_controls = []
    for channel in status_payload["switch"]["channels"]:
        status_switch_rows.append(
            '<tr><td>{}</td><td id="switch_{}">{}</td></tr>'.format(
                _html_escape(channel["label"] or channel["channel_id"]),
                _html_escape(channel["key"]),
                "ON" if channel.get("state") else "OFF",
            )
        )
    for channel in payload["switch"]["channels"]:
        switch_controls.append(
            """<div class="row action-row"><label>{key}</label>
<input id="{key}_label" value="{label}">
<button class="secondary" onclick="setSwitch('{channel}',true)">On</button>
<button class="secondary" onclick="setSwitch('{channel}',false)">Off</button></div>""".format(
                key=_html_escape(channel["key"]),
                label=_html_escape(channel["label"]),
                channel=_html_escape(channel["channel_id"]),
            )
        )
    if not status_switch_rows:
        status_switch_rows.append("<tr><td colspan='2'>No switch channels</td></tr>")

    display_rows = []
    for index, metric in enumerate(payload["sensor"]["display_metrics"], start=1):
        display_rows.append(
            """<div class="metric-row"><label>Metric {index}</label>
<input id="metric_{index}" value="{metric}"><label>Style</label>
<input id="style_{index}" value="{style}"></div>""".format(
                index=index,
                metric=_html_escape(metric),
                style=_html_escape(payload["sensor"]["display_styles"][index - 1]),
            )
        )

    calibration_rows = []
    device_calibration = payload["sensor"]["calibration_device"]
    for key in _calibration_keys(payload["sensor"]):
        calibration_rows.append(
            """<div class="row"><label>{label}</label>
<input id="cal_{key}" type="number" step="any" value="{value}"></div>""".format(
                label=_html_escape(_calibration_label(key)),
                key=_html_escape(key),
                value=_html_escape(device_calibration.get(key, 0)),
            )
        )

    profile_options = []
    for profile in ("nodusweb", "sensorius", "weewx", "homeassistant"):
        selected = " selected" if profile == payload["profile"] else ""
        profile_options.append(
            '<option value="{}"{}>{}</option>'.format(profile, selected, profile)
        )

    sensor = payload["sensor"]
    switch = payload["switch"]
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nodus {hostname}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef8f3;color:#1b1f24;font-family:Helvetica,Arial,sans-serif}}
.layout{{max-width:1120px;margin:auto;padding:18px;display:grid;grid-template-columns:190px 1fr;gap:18px}}
.side,.panel{{background:#fff;border:1px solid #d4ded8;border-radius:12px;box-shadow:0 2px 8px #0001;height:min(640px,calc(100vh - 36px));overflow-y:auto}}
.side{{padding:14px;background:#e6f1ea}}.brand{{font-size:21px;font-weight:700;padding:8px 9px 16px;border-bottom:1px solid #d7e0db;margin-bottom:12px}}
.nav{{display:grid;gap:12px}}.nav button{{text-align:left;background:#fff;color:#1b1f24;border:1px solid #fff;border-radius:8px;padding:14px 16px}}.nav button.active{{background:#dceaff;border-color:#2d6cdf}}
.panel{{display:none;overflow:hidden}}.panel.active{{display:flex;flex-direction:column}}.panel h2{{margin:0;padding:18px 20px;border-bottom:1px solid #e5ebe8;flex:0 0 auto}}.panel>.body{{display:block;flex:1 1 auto;min-height:0;overflow-y:scroll}}.panel>.body>.section,.panel>.body>.group{{margin-bottom:14px}}.panel>.actions{{flex:0 0 auto}}
.body{{padding:18px;display:grid;gap:14px}}.section,.group{{border:1px solid #dfe6ea;border-radius:10px;overflow:hidden}}
.section{{padding:15px}}details summary{{cursor:pointer;font-size:18px;font-weight:700;padding:13px 16px;background:#f7faf9}}.group-body table{{table-layout:fixed}}.group-body table td:first-child{{width:50%}}
.group-body{{padding:16px}}.row{{display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:center;margin-bottom:10px}}
.action-row{{grid-template-columns:170px 1fr 70px 70px}}.metric-row{{display:grid;grid-template-columns:100px 1fr 60px 1fr;gap:8px;align-items:center;margin-bottom:10px}}
input,select{{width:100%;min-width:0;padding:9px;border:1px solid #cfd8e3;border-radius:8px;font-size:16px;background:#fff}}
.password{{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center}}.password label{{white-space:nowrap}}
.password input[type=checkbox],.tls input{{width:auto}}button{{padding:10px 14px;border:0;border-radius:8px;background:#2d5bea;color:#fff;font-weight:700}}
button.secondary{{background:#fff;color:#1b1f24;border:1px solid #cfd8e3}}button.danger{{background:#e4344f}}
.actions{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 20px;border-top:1px solid #d6e1dc}}
.status{{font-size:13px;color:#586574;word-break:break-word}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #edf1f5;text-align:left}}.status-table{{table-layout:fixed}}.status-table th:first-child,.status-table td:first-child{{width:50%}}
.sample-time{{padding:0 9px 6px 50%;font-size:14px;font-weight:700;color:#586574}}
@media(max-width:760px){{.layout{{grid-template-columns:1fr;padding:10px}}.side,.panel{{height:auto;overflow-y:visible}}.panel.active{{display:block}}.panel>.body{{overflow-y:visible}}.nav{{grid-template-columns:repeat(4,1fr)}}.nav button{{text-align:center;padding:9px 4px}}.row,.action-row,.metric-row{{grid-template-columns:1fr}}}}
</style><script>
function byId(id){{return document.getElementById(id)}}
function panel(id,button){{document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));byId(id).classList.add('active');if(button)button.classList.add('active')}}
async function postJson(path,payload){{const r=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});return await r.json()}}
function add(updates,section,key,id){{const e=byId(id);if(e)updates.push({{section:section,key:key,value:e.type==='checkbox'?e.checked:e.value}})}}
function togglePassword(id,box){{byId(id).type=box.checked?'text':'password'}}
async function saveSetup(restart){{
 const u=[];add(u,'Network','SSID','ssid');add(u,'Network','PASSWORD','wifi_password');add(u,'Network','HOSTNAME','hostname');
 add(u,'Time','TZ','time_tz');add(u,'Time','TZ_OFFSET','time_offset');add(u,'Time','TZ_NAME','time_name');add(u,'Time','NTP_SERVER','ntp_server');add(u,'Time','NTP_SERVER_IP','ntp_ip');
 add(u,'Profile','ACTIVE_PROFILE','profile');add(u,'MQTT','BROKER','mqtt_broker');add(u,'MQTT','USERNAME','mqtt_user');add(u,'MQTT','PASSWORD','mqtt_password');add(u,'MQTT','PORT','mqtt_port');add(u,'MQTT','USE_TLS','mqtt_tls');
 add(u,'Sensor','LOCATION','location');add(u,'Switch','SWITCH_LOCATION','location');
 for(let i=1;i<=6;i++){{add(u,'Display','METRIC_'+i,'metric_'+i);add(u,'Display.Style','METRIC_'+i,'style_'+i)}}
 for(let i=1;i<=2;i++)add(u,'Switch','SWITCH_'+i+'_LABEL','SWITCH_'+i+'_label');
 const result=await postJson('/config',{{updates:u}});byId('setup_status').textContent=JSON.stringify(result);
 if(restart&&result.success)await postJson('/restart',{{mode:'soft'}})
}}
async function saveCalibration(){{const u=[];document.querySelectorAll('[id^=cal_]').forEach(e=>u.push({{section:'Calibration.Device',key:e.id.slice(4),value:e.value}}));const r=await postJson('/config',{{updates:u}});byId('cal_status').textContent=JSON.stringify(r)}}
async function setSwitch(channel,state){{const r=await postJson('/set-switch-state',{{channel_id:channel,state:state}});byId('switch_status').textContent=JSON.stringify(r)}}
async function restartDevice(){{await postJson('/restart',{{mode:'soft'}})}}
function sampleValue(value){{return value===null||value===undefined?'--':(typeof value==='number'?value.toFixed(2):String(value))}}
async function refreshStatus(){{try{{const r=await fetch('/current-data',{{cache:'no-store'}});const p=await r.json();const sensor=p.sensor||{{}};const stamp=byId('sample_timestamp');if(stamp)stamp.textContent=sensor.display_timestamp||'';(sensor.display_metrics||[]).forEach(item=>{{const cell=byId('sample_'+item.index);if(cell)cell.textContent=sampleValue(item.value)}});((p.switch||{{}}).channels||[]).forEach(item=>{{const cell=byId('switch_'+item.key);if(cell)cell.textContent=item.state?'ON':'OFF'}})}}catch(error){{}}}}
setInterval(refreshStatus,15000);setTimeout(refreshStatus,1000);
</script></head><body><main class="layout">
<aside class="side"><div class="brand">{hostname}</div><div class="nav">
<button class="active" onclick="panel('status',this)">Status</button><button onclick="panel('setup',this)">Setup</button><button onclick="panel('calibration',this)">Calibration</button><button onclick="panel('info',this)">Nodus Info</button></div></aside>
<section id="status" class="panel active"><h2>Status</h2><div class="body"><div class="section"><div id="sample_timestamp" class="sample-time">{sample_timestamp}</div><table class="status-table"><tr><th>Metric</th><th>Current Sample</th></tr>{metric_rows}</table></div><div class="section"><h3>Switches</h3><table class="status-table">{status_switch_rows}</table></div></div></section>
<section id="setup" class="panel"><h2>Setup</h2><div class="body">
<details class="group" open><summary>Network</summary><div class="group-body"><div class="row"><label>SSID</label><input id="ssid" value="{ssid}"></div><div class="row"><label>Password</label><div class="password"><input id="wifi_password" type="password" value="{wifi_password}"><label><input type="checkbox" onchange="togglePassword('wifi_password',this)"> Show</label></div></div><div class="row"><label>Hostname</label><input id="hostname" value="{hostname}"></div><div class="row"><label>Web</label><input value="http://{hostname}.local:{http_port}" readonly></div></div></details>
<details class="group" open><summary>Time</summary><div class="group-body"><div class="row"><label>Time Zone</label><input id="time_tz" value="{time_tz}"></div><div class="row"><label>TZ Offset</label><input id="time_offset" type="number" value="{time_offset}"></div><div class="row"><label>TZ Name</label><input id="time_name" value="{time_name}"></div><div class="row"><label>NTP Server</label><input id="ntp_server" value="{ntp_server}"></div><div class="row"><label>NTP Server IP</label><input id="ntp_ip" value="{ntp_ip}"></div></div></details>
<details class="group" open><summary>Profile</summary><div class="group-body"><div class="row"><label>Profile</label><select id="profile">{profile_options}</select></div><div class="row"><label>MQTT Broker</label><input id="mqtt_broker" value="{mqtt_broker}"></div><div class="row"><label>Username</label><input id="mqtt_user" value="{mqtt_user}"></div><div class="row"><label>Password</label><div class="password"><input id="mqtt_password" type="password" value="{mqtt_password}"><label><input type="checkbox" onchange="togglePassword('mqtt_password',this)"> Show</label></div></div><div class="row"><label>Port</label><input id="mqtt_port" type="number" value="{mqtt_port}"></div><div class="row tls"><label>Use TLS</label><input id="mqtt_tls" type="checkbox"{mqtt_tls}></div></div></details>
<details class="group" open><summary>Sensor Display</summary><div class="group-body"><div class="row"><label>Location</label><input id="location" value="{location}"></div>{display_rows}</div></details>
<details class="group" open><summary>Switches</summary><div class="group-body">{switch_controls}<span id="switch_status" class="status"></span></div></details>
</div><footer class="actions"><button onclick="saveSetup(true)">Save &amp; Restart</button><span id="setup_status" class="status"></span><button onclick="saveSetup(false)">Save</button></footer></section>
<section id="calibration" class="panel"><h2>Device Calibration</h2><div class="body"><div class="section">{calibration_rows}</div></div><footer class="actions"><button class="danger" onclick="restartDevice()">Restart Device</button><span id="cal_status" class="status"></span><button onclick="saveCalibration()">Save</button></footer></section>
<section id="info" class="panel"><h2>Nodus Info</h2><div class="body"><details class="group" open><summary>Net Info</summary><div class="group-body"><table><tr><td>Hostname</td><td>{hostname}</td></tr><tr><td>SSID</td><td>{ssid}</td></tr><tr><td>IPv4</td><td>{ip}</td></tr><tr><td>Profile</td><td>{profile}</td></tr><tr><td>WiFi Recovery Count</td><td>{wifi_recovery_count}</td></tr></table></div></details><details class="group" open><summary>Device Info</summary><div class="group-body"><table><tr><td>Firmware</td><td>{version}</td></tr><tr><td>Sensor ID</td><td>{sensor_id}</td></tr><tr><td>Sensor</td><td>{sensor_device}</td></tr><tr><td>Interface</td><td>{sensor_interface}</td></tr><tr><td>Switch ID</td><td>{switch_id}</td></tr><tr><td>Switch Channels</td><td>{switch_count}</td></tr></table></div></details></div></section>
</main></body></html>""".format(
        hostname=_html_escape(payload["network"]["hostname"]), version=_html_escape(payload["version"]),
        metric_rows="".join(metric_rows), status_switch_rows="".join(status_switch_rows), ssid=_html_escape(payload["network"]["ssid"]),
        sample_timestamp=_html_escape(status_payload["sensor"]["display_timestamp"]),
        wifi_password=_html_escape(payload["network"]["password"]), http_port=_html_escape(payload["network"]["http_port"]),
        time_tz=_html_escape(payload["time"]["tz"]), time_offset=_html_escape(payload["time"]["tz_offset"]), time_name=_html_escape(payload["time"]["tz_name"]),
        ntp_server=_html_escape(payload["time"]["ntp_server"]), ntp_ip=_html_escape(payload["time"]["ntp_server_ip"]), profile_options="".join(profile_options),
        mqtt_broker=_html_escape(payload["mqtt"]["broker"]), mqtt_user=_html_escape(payload["mqtt"]["username"]), mqtt_password=_html_escape(payload["mqtt"]["password"]),
        mqtt_port=_html_escape(payload["mqtt"]["port"]), mqtt_tls=" checked" if payload["mqtt"]["use_tls"] else "", location=_html_escape(sensor["location"] or switch["location"]),
        display_rows="".join(display_rows), switch_controls="".join(switch_controls), calibration_rows="".join(calibration_rows), ip=_html_escape(status_payload["network"]["ipv4addr"]),
        profile=_html_escape(payload["profile"]), sensor_id=_html_escape(sensor["sensor_id"]), sensor_device=_html_escape(sensor["hardware"] or sensor["device"]),
        sensor_interface=_html_escape(sensor["interface"]), switch_id=_html_escape(switch["device_id"]), switch_count=_html_escape(switch["channel_count"]),
        wifi_recovery_count=_html_escape(status_payload["network"]["wifi_recovery_count"]),
    )


def _status_value(value):
    if value is None:
        return "--"
    if isinstance(value, float):
        return "{:.2f}".format(value)
    return value


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


def _calibration_keys(sensor):
    device = str(sensor.get("device", "") or "").lower()
    if device == "co2":
        return ("TEMP_OFFSET", "RH_OFFSET", "CO2_OFFSET", "ALTITUDE_METERS")
    if device == "aqi":
        return ("TEMP_OFFSET", "RH_OFFSET", "AQI_OFFSET", "GAS_OFFSET", "ALTITUDE_METERS")
    if device == "lux":
        return ("LUX_OFFSET", "PPFD_OFFSET")
    if device in {"apvpd", "apvpd_aht"}:
        return ("APVPD_TEMP_CAL_VAL", "APVPD_RH_CAL_VAL", "ALTITUDE_METERS")
    if device == "soil":
        return ("SOIL_TEMP_CAL_VAL", "SOIL_MOIST_CAL_VAL", "SOIL_PH_CAL_VAL", "SOIL_EC_CAL_VAL")
    return ("TEMP_OFFSET", "RH_OFFSET", "ALTITUDE_METERS")


def _calibration_label(key):
    """Return a readable label using CircuitPython-supported string methods."""
    return str(key or "").replace("_", " ")
