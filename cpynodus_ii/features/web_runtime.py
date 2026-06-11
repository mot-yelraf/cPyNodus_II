# ruff: noqa: E501
"""Integrate the lightweight web server with runtime handlers and state.

This module starts and polls the constrained web runtime, dispatches requests
to handler helpers, and reports availability back to the main application loop.
"""

import json

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
        switch_service=None,
        settings_root=None,
        server_module=None,
        reboot_callbacks=None,
        event_logger=None,
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
        self.server = None
        self.phase = "new"
        self.errors = ()
        self._route_paths = ()
        self._pending_reboot_callback = None

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
        switch_service=None,
        version=None,
    ):
        """Refresh mutable runtime context used by handlers."""
        if runtime_config is not None:
            self.runtime_config = runtime_config
        if network_stack is not None:
            self.network_stack = network_stack
        if sensor_service is not None:
            self.sensor_service = sensor_service
        if switch_service is not None:
            self.switch_service = switch_service
        if version is not None:
            self.version = version

    def poll(self):
        """Poll the server once if active."""
        if self.phase != "ready" or self.server is None:
            return self
        poll = getattr(self.server, "poll", None)
        if not callable(poll):
            self.phase = "error"
            self.errors = ("web_server_poll_unavailable",)
            return self
        try:
            poll()
        except Exception as exc:
            self.phase = "error"
            self.errors = ("web_poll_failed", str(exc))
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
            payload = build_status_payload(
                self.runtime_config,
                version=self.version,
                sensor_service=self.sensor_service,
                switch_service=self.switch_service,
                ip_address=getattr(self.network_stack, "ip_address", ""),
            )
            return self._html_response(request, _render_status_html(payload))

        @route("/current-data", methods=["GET"])
        def _current_data(request):
            payload = build_status_payload(
                self.runtime_config,
                version=self.version,
                sensor_service=self.sensor_service,
                switch_service=self.switch_service,
                ip_address=getattr(self.network_stack, "ip_address", ""),
            )
            return self._json_response(request, payload)

        @route("/setup", methods=["GET"])
        def _setup(request):
            payload = build_setup_payload(self.runtime_config, version=self.version)
            return self._html_response(request, _render_setup_html(payload))

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


def _html_escape(value):
    text = str(value if value is not None else "")
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text


def _render_status_html(payload):
    rows = []
    for item in payload["sensor"]["display_metrics"]:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _html_escape(item["metric"]),
                _html_escape(item.get("value", "")),
                _html_escape(item.get("style", "")),
            )
        )
    if not rows:
        rows.append("<tr><td colspan='3'>No display metrics configured</td></tr>")
    switch_rows = []
    for channel in payload["switch"]["channels"]:
        switch_rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _html_escape(channel["label"] or channel["channel_id"]),
                _html_escape(channel["channel_id"]),
                "ON" if channel.get("state") else "OFF",
            )
        )
    if not switch_rows:
        switch_rows.append("<tr><td colspan='3'>No switch channels</td></tr>")
    return """
<html><head><meta charset="utf-8"><title>Nodus Status</title>
<style>
body{{font-family:Helvetica,Arial,sans-serif;background:#f7f8fa;color:#1b1f24;margin:0;padding:24px;}}
.wrap{{max-width:960px;margin:0 auto;display:grid;gap:16px;}}
.card{{background:#fff;border:1px solid #d8dee4;border-radius:12px;padding:16px;}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #eef2f6;text-align:left}}
.nav a{{margin-right:12px}}
</style></head><body>
<div class="wrap">
<div class="card"><h1>{hostname}</h1><div>Profile: {profile} | IP: {ip}</div><div class="nav"><a href="/setup">Setup</a><a href="/current-data">JSON</a></div></div>
<div class="card"><h2>Display</h2><table><tr><th>Metric</th><th>Value</th><th>Style</th></tr>{rows}</table></div>
<div class="card"><h2>Switches</h2><table><tr><th>Label</th><th>Channel</th><th>State</th></tr>{switch_rows}</table></div>
</div></body></html>
""".format(
        hostname=_html_escape(payload["network"]["hostname"]),
        profile=_html_escape(payload["profile"]),
        ip=_html_escape(payload["network"]["ipv4addr"]),
        rows="".join(rows),
        switch_rows="".join(switch_rows),
    )


def _render_setup_html(payload):
    switch_sections = []
    for channel in payload["switch"]["channels"]:
        switch_sections.append(
            """
<div class="row">
<label>{label}</label>
<input id="{key}_label" value="{label_value}">
<button onclick="setSwitch('{channel_id}', true)">On</button>
<button onclick="setSwitch('{channel_id}', false)">Off</button>
</div>
""".format(
                label=_html_escape(channel["key"]),
                key=_html_escape(channel["key"]),
                label_value=_html_escape(channel["label"]),
                channel_id=_html_escape(channel["channel_id"]),
            )
        )
    metric_rows = []
    for index, metric in enumerate(payload["sensor"]["display_metrics"], start=1):
        metric_rows.append(
            """
<div class="row">
<label>Metric {idx}</label><input id="metric_{idx}" value="{metric}">
<label>Style</label><input id="style_{idx}" value="{style}">
</div>
""".format(
                idx=index,
                metric=_html_escape(metric),
                style=_html_escape(payload["sensor"]["display_styles"][index - 1]),
            )
        )
    return """
<html><head><meta charset="utf-8"><title>Nodus Setup</title>
<style>
body{{font-family:Helvetica,Arial,sans-serif;background:#f7f8fa;color:#1b1f24;margin:0;padding:24px;}}
.wrap{{max-width:960px;margin:0 auto;display:grid;gap:16px;}}
.card{{background:#fff;border:1px solid #d8dee4;border-radius:12px;padding:16px;}}
.row{{display:grid;grid-template-columns:140px 1fr 100px 100px;gap:8px;align-items:center;margin-bottom:8px}}
input{{padding:8px;border:1px solid #c8d1dc;border-radius:8px}} button{{padding:8px 12px}}
.status{{font-size:13px;color:#4b5563}}
</style>
<script>
async function postJson(path, payload){{
  const res = await fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload)}});
  return await res.json();
}}
async function saveLive(){{
  const updates = [];
  updates.push({{section:'Sensor', key:'LOCATION', value:document.getElementById('location').value}});
  updates.push({{section:'Switch', key:'SWITCH_LOCATION', value:document.getElementById('location').value}});
  updates.push({{section:'Switch', key:'SWITCH_1_LABEL', value:document.getElementById('SWITCH_1_label')?.value || ''}});
  updates.push({{section:'Switch', key:'SWITCH_2_LABEL', value:document.getElementById('SWITCH_2_label')?.value || ''}});
  for (let i=1;i<=6;i++) {{
    updates.push({{section:'Display', key:'METRIC_'+i, value:document.getElementById('metric_'+i).value}});
    updates.push({{section:'Display.Style', key:'METRIC_'+i, value:document.getElementById('style_'+i).value}});
  }}
  const result = await postJson('/config', {{updates}});
  document.getElementById('live_status').textContent = JSON.stringify(result);
}}
async function saveRestart(){{
  const updates = [
    {{section:'Network', key:'SSID', value:document.getElementById('ssid').value}},
    {{section:'Network', key:'HOSTNAME', value:document.getElementById('hostname').value}},
    {{section:'Network', key:'AP_CHANNEL', value:document.getElementById('ap_channel').value}},
  ];
  const result = await postJson('/config', {{updates}});
  document.getElementById('restart_status').textContent = JSON.stringify(result);
}}
async function setSwitch(channelId, state){{
  const result = await postJson('/set-switch-state', {{channel_id:channelId, state:state}});
  document.getElementById('switch_status').textContent = JSON.stringify(result);
}}
</script></head><body>
<div class="wrap">
<div class="card"><h1>Setup {hostname}</h1><div class="status">Profile: {profile}</div></div>
<div class="card"><h2>Restart-Required</h2>
<div class="row"><label>SSID</label><input id="ssid" value="{ssid}"><span></span><span></span></div>
<div class="row"><label>AP Channel</label><input id="ap_channel" value="{ap_channel}"><span></span><span></span></div>
<div class="row"><label>Hostname</label><input id="hostname" value="{hostname}"><button onclick="saveRestart()">Save</button><span id="restart_status" class="status"></span></div>
</div>
<div class="card"><h2>Live Updates</h2>
<div class="row"><label>Location</label><input id="location" value="{location}"><button onclick="saveLive()">Apply</button><span id="live_status" class="status"></span></div>
{metric_rows}
</div>
<div class="card"><h2>Switches</h2>{switch_rows}<div id="switch_status" class="status"></div></div>
</div></body></html>
""".format(
        hostname=_html_escape(payload["network"]["hostname"]),
        profile=_html_escape(payload["profile"]),
        ssid=_html_escape(payload["network"]["ssid"]),
        ap_channel=_html_escape(payload["network"]["ap_channel"]),
        location=_html_escape(
            payload["sensor"]["location"] or payload["switch"]["location"]
        ),
        metric_rows="".join(metric_rows),
        switch_rows="".join(switch_sections),
    )
