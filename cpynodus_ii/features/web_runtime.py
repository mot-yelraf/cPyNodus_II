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
    ):
        self.runtime_config = runtime_config
        self.network_stack = network_stack
        self.version = version
        self.sensor_service = sensor_service
        self.switch_service = switch_service
        self.settings_root = settings_root
        self.server_module = server_module
        self.reboot_callbacks = dict(reboot_callbacks or {})
        self.server = None
        self.phase = "new"
        self.errors = ()
        self._route_paths = ()

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
                payload = _parse_json_body(request)
                if payload is None:
                    return self._json_response(
                        request,
                        {"success": False, "error": "invalid_json"},
                        status_code=400,
                    )
                result = apply_itaot_init_payload(
                    payload,
                    self.runtime_config,
                    settings_root=self.settings_root or ".",
                )
                self.runtime_config = result.runtime_config
                if result.accepted:
                    callback = self.reboot_callbacks.get(
                        "hard"
                    ) or self.reboot_callbacks.get("soft")
                    if callback is not None:
                        callback()
                return self._json_response(
                    request,
                    result.body,
                    status_code=result.status_code,
                )

            @route("/itaot-meta", methods=["GET"])
            def _itaot_meta(request):
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
                return self._json_response(request, payload)

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
    metric_tiles = []
    for item in payload["sensor"]["current_metrics"]:
        metric_tiles.append(
            "<div class='metric'><label>{}</label><strong>{}</strong></div>".format(
                _html_escape(item["metric"]),
                _html_escape(item.get("value", "")),
            )
        )
    if not metric_tiles:
        metric_tiles.append("<div class='empty'>No sensor data</div>")
    profile_options = []
    for option in payload["profile_options"]:
        selected = " selected" if option == payload["profile"] else ""
        profile_options.append(
            "<option value='{0}'{1}>{0}</option>".format(_html_escape(option), selected)
        )
    switch_controls = _switch_controls_html(
        payload["switch"]["channels"],
        editable=True,
    )
    menu_items = _menu_items_html(
        payload["sensor"]["present"],
        payload["switch"]["present"],
    )
    return _page_html(
        "Data",
        menu_items,
        """
<section id="data" class="view active">
<div class="topline"><p>{ip}</p><p id="measurement_time">{timestamp}</p></div>
<div class="metrics">{metric_tiles}</div>
</section>
<section id="nodus" class="view">
<p>{device_id}</p><p>{version}</p>
<div class="form">
<label>Location</label><input id="location" value="{location}">
<label>Profile</label><select id="profile">{profiles}</select>
<label>SSID</label><input id="ssid" value="{ssid}">
<label>Password</label><input id="password" type="password" value="{password}">
<button onclick="saveNodus()">Save</button><span id="nodus_status"></span>
</div></section>
<section id="sensor" class="view">{sensor_fields}</section>
<section id="switch" class="view">{switch_controls}</section>
""".format(
            hostname=_html_escape(payload["network"]["hostname"]),
            ip=_html_escape(payload["network"]["ipv4addr"]),
            timestamp=_html_escape(payload["measurement"]["timestamp"]),
            metric_tiles="".join(metric_tiles),
            device_id=_html_escape(_primary_device_id(payload)),
            version=_html_escape(payload["version"]),
            location=_html_escape(
                payload["sensor"]["location"] or payload["switch"]["location"]
            ),
            profiles="".join(profile_options),
            ssid=_html_escape(payload["network"]["ssid"]),
            password=_html_escape(payload["network"]["password"]),
            sensor_fields=_sensor_config_html(payload["sensor"]),
            switch_controls=switch_controls,
        ),
    )


def _render_setup_html(payload):
    menu_items = _menu_items_html(
        payload["sensor"]["present"],
        payload["switch"]["present"],
    )
    profile_options = []
    for option in payload["profile_options"]:
        selected = " selected" if option == payload["profile"] else ""
        profile_options.append(
            "<option value='{0}'{1}>{0}</option>".format(_html_escape(option), selected)
        )
    return _page_html(
        "Nodus",
        menu_items,
        """
<section id="data" class="view active"><div class="topline"><p>{ip}</p></div>
</section><section id="nodus" class="view">
<p>{device_id}</p><p>{version}</p>
<div class="form">
<label>Location</label><input id="location" value="{location}">
<label>Profile</label><select id="profile">{profiles}</select>
<label>SSID</label><input id="ssid" value="{ssid}">
<label>Password</label><input id="password" type="password" value="{password}">
<button onclick="saveNodus()">Save</button><span id="nodus_status"></span>
</div></section>
<section id="sensor" class="view">{sensor_fields}</section>
<section id="switch" class="view">{switch_controls}</section>
""".format(
            hostname=_html_escape(payload["network"]["hostname"]),
            ip="",
            device_id=_html_escape(_primary_device_id(payload)),
            version=_html_escape(payload["version"]),
            location=_html_escape(
                payload["sensor"]["location"] or payload["switch"]["location"]
            ),
            profiles="".join(profile_options),
            ssid=_html_escape(payload["network"]["ssid"]),
            password=_html_escape(payload["network"]["password"]),
            sensor_fields=_sensor_config_html(payload["sensor"]),
            switch_controls=_switch_controls_html(
                payload["switch"]["channels"],
                editable=True,
            ),
        ),
    )


def _page_html(title, menu_items, body):
    return """
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<meta charset="utf-8"><title>Nodus {title}</title><style>
body{{font-family:Helvetica,Arial,sans-serif;background:#f3f5f7;
color:#17212b;margin:0;padding:18px;display:flex;justify-content:center}}
main{{position:relative;width:min(100%,620px);background:#f8fafc;
border:1px solid #cbd5df;border-radius:8px;padding:18px;box-shadow:0 1px 2px #0001}}
header{{display:flex;align-items:flex-start;justify-content:space-between;
gap:12px;margin-bottom:12px}}
.hamb{{font-size:28px;background:#17212b;color:white;border:0;
border-radius:6px;width:44px;height:40px}}
nav{{display:none;position:absolute;right:0;top:48px;background:white;
border:1px solid #ccd4dd;border-radius:8px;box-shadow:0 6px 18px #0002}}
nav.open{{display:block}}
nav button{{display:block;width:150px;padding:13px;border:0;background:white;
color:#17212b;text-align:left;font-size:16px}}
.view{{display:none}}.view.active{{display:block}}
h1{{font-size:30px;margin:0}}
p{{margin:4px 0;color:#51606f}}
.topline{{margin:8px 0 20px}}
.metrics{{display:grid;grid-template-columns:1fr 1fr;gap:7px;max-width:500px;
max-height:430px;overflow-y:auto;padding-right:4px}}
.metric{{background:white;border:1px solid #d8dee5;border-radius:8px;
padding:9px;min-height:48px}}
.metric label,.form label{{display:block;font-size:13px;color:#607080;
margin-bottom:7px}}
.metric strong{{font-size:22px;font-weight:650;overflow-wrap:anywhere}}
.form{{display:grid;grid-template-columns:110px minmax(150px,300px);gap:10px;
align-items:center;background:white;border:1px solid #d8dee5;
border-radius:8px;padding:14px;max-width:460px}}
input,select{{font-size:16px;padding:10px;border:1px solid #b8c3cf;
border-radius:6px;min-width:0;max-width:300px}}
button{{font-size:15px;border:1px solid #17212b;border-radius:6px;
background:#17212b;color:white;padding:10px 14px}}
.switch{{display:grid;grid-template-columns:minmax(150px,300px) 84px;gap:8px;
align-items:center;background:white;border:1px solid #d8dee5;
border-radius:8px;padding:12px;margin-bottom:10px;max-width:460px}}
.state-on{{background:#17803d;border-color:#17803d}}
.state-off{{background:#17212b;border-color:#17212b}}
.empty{{background:white;border:1px solid #d8dee5;border-radius:8px;
padding:16px;color:#607080}}
@media(max-width:560px){{body{{padding:10px}}main{{padding:12px}}
.metrics{{gap:10px}}
.metric strong{{font-size:20px}}.form{{grid-template-columns:1fr}}
.switch{{grid-template-columns:1fr 1fr}}}}
</style><script>
function menu(){{document.getElementById('menu').classList.toggle('open')}}
function show(id){{for(const v of document.querySelectorAll('.view'))
v.classList.remove('active');document.getElementById(id).classList.add('active');
const t=document.getElementById('view_title');
if(t)t.textContent=id.charAt(0).toUpperCase()+id.slice(1);
menu()}}
async function postJson(path,payload){{const r=await fetch(path,{{method:'POST',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
return await r.json()}}
async function saveNodus(){{const u=[
{{section:'Sensor',key:'LOCATION',value:document.getElementById('location').value}},
{{section:'Switch',key:'SWITCH_LOCATION',
value:document.getElementById('location').value}},
{{section:'Profile',key:'ACTIVE_PROFILE',
value:document.getElementById('profile').value}},
{{section:'Network',key:'SSID',value:document.getElementById('ssid').value}},
{{section:'Network',key:'PASSWORD',value:document.getElementById('password').value}}];
document.getElementById('nodus_status').textContent=JSON.stringify(
await postJson('/config',{{updates:u}}))}}
async function saveSensor(){{const u=[];for(const e of
document.querySelectorAll('[data-cal]'))u.push(
{{section:'Calibration.Device',key:e.dataset.cal,value:e.value}});
document.getElementById('sensor_status').textContent=JSON.stringify(
await postJson('/config',{{updates:u}}))}}
async function saveSwitchLabels(){{const u=[];for(const e of
document.querySelectorAll('[data-switch-label]'))u.push(
{{section:'Switch',key:e.dataset.switchLabel,value:e.value}});
document.getElementById('switch_status').textContent=JSON.stringify(
await postJson('/config',{{updates:u}}))}}
async function setSwitch(id,state){{document.getElementById('switch_status')
.textContent=JSON.stringify(await postJson('/set-switch-state',
{{channel_id:id,state:state}}))}}
async function toggleSwitch(id,state){{const r=await postJson('/set-switch-state',
{{channel_id:id,state:!state}});const b=document.getElementById('sw_'+id);
if(b&&r.success){{b.textContent=r.state?'On':'Off';
b.className=r.state?'state-on':'state-off';
b.setAttribute('onclick',"toggleSwitch('"+id+"',"+(r.state?'true':'false')+")")}}
const s=document.getElementById('switch_status');
if(s)s.textContent=r.success?'':(r.errors||['switch update failed']).join(', ')}}
function esc(s){{return String(s==null?'':s).replace(/[&<>"]/g,function(c){{
return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]}})}}
async function refreshData(){{try{{const r=await fetch('/current-data');
const p=await r.json();const t=document.getElementById('measurement_time');
if(t)t.textContent=p.measurement.timestamp||'';
const g=document.querySelector('.metrics');
if(!g)return;let h='';for(const m of p.sensor.current_metrics||[]){{h+=
"<div class='metric'><label>"+esc(m.metric)+"</label><strong>"+
esc(m.value)+"</strong></div>"}}
g.innerHTML=h||"<div class='empty'>No sensor data</div>"}}
catch(e){{}}}}
setInterval(refreshData,15000)
</script></head><body><main><header><h1 id="view_title">{title}</h1>
<div><button class="hamb" onclick="menu()">&#9776;</button>
<nav id="menu">{menu_items}</nav></div></header>{body}</main>
</body></html>
""".format(title=_html_escape(title), menu_items=menu_items, body=body)


def _menu_items_html(sensor_present, switch_present):
    items = [
        "<button onclick=\"show('data')\">Data</button>",
        "<button onclick=\"show('nodus')\">Nodus</button>",
    ]
    if sensor_present:
        items.append("<button onclick=\"show('sensor')\">Sensor</button>")
    if switch_present:
        items.append("<button onclick=\"show('switch')\">Switch</button>")
    return "".join(items)


def _primary_device_id(payload):
    return payload["sensor"]["sensor_id"] or payload["switch"]["device_id"]


def _sensor_config_html(sensor):
    if not sensor["present"]:
        return "<div class='empty'>No sensor</div>"
    rows = []
    for key in _calibration_keys_for_device(sensor["device"]):
        rows.append(
            "<label>{}</label><input data-cal='{}' value='{}'>".format(
                _html_escape(key),
                _html_escape(key),
                _html_escape(sensor["calibration_device"].get(key, 0.0)),
            )
        )
    return (
        "<div class='form'>{}<button onclick='saveSensor()'>Save</button>"
        "<span id='sensor_status'></span></div>"
    ).format("".join(rows))


def _calibration_keys_for_device(device):
    mapping = {
        "aqi": ("TEMP_OFFSET", "RH_OFFSET", "GAS_OFFSET", "AQI_OFFSET"),
        "co2": ("TEMP_OFFSET", "RH_OFFSET", "CO2_OFFSET"),
        "lux": ("LUX_OFFSET",),
        "aht": ("TEMP_OFFSET", "RH_OFFSET"),
        "avpd": ("TEMP_OFFSET", "RH_OFFSET"),
        "apvpd": (
            "TEMP_OFFSET",
            "RH_OFFSET",
            "APVPD_TEMP_CAL_VAL",
            "APVPD_RH_CAL_VAL",
        ),
        "apvpd_aht": (
            "TEMP_OFFSET",
            "RH_OFFSET",
            "APVPD_TEMP_CAL_VAL",
            "APVPD_RH_CAL_VAL",
        ),
        "soil": (
            "SOIL_TEMP_CAL_VAL",
            "SOIL_TEMP_MOIST_VAL",
            "SOIL_PH_CAL_VAL",
            "SOIL_EC_CAL_VAL",
        ),
    }
    return mapping.get(str(device or "").lower(), ())


def _switch_controls_html(channels, *, editable=False):
    if not channels:
        return "<div class='empty'>No switches</div>"
    rows = []
    for channel in channels:
        label = channel["label"] or channel["channel_id"]
        name = _html_escape(label)
        if editable:
            name = (
                "<input data-switch-label='{key}_LABEL' value='{label}'>"
            ).format(
                key=_html_escape(channel["key"]),
                label=_html_escape(label),
            )
        rows.append(
            (
                "<div class='switch'>{}<button id='sw_{}' class='{}' "
                "onclick=\"toggleSwitch('{}',{})\">{}</button></div>"
            ).format(
                name,
                _html_escape(channel["channel_id"]),
                "state-on" if channel.get("state") else "state-off",
                _html_escape(channel["channel_id"]),
                "true" if channel.get("state") else "false",
                "On" if channel.get("state") else "Off",
            )
        )
    if editable:
        rows.append(
            "<button onclick='saveSwitchLabels()'>Save</button>"
            "<span id='switch_status'></span>"
        )
    else:
        rows.append("<span id='switch_status'></span>")
    return "".join(rows)
