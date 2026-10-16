# ruff: noqa: E501
"""Render NodusWeb setup, calibration, switch, and information pages.

``render_config_html`` selects an independently loaded page and wraps it with
the shared UI helpers. Page renderers collect temporary objects before the
final document allocation to reduce fragmented-heap failures on Pico2 W.
"""

import gc

from cpynodus_ii.features.web_ui_common import (
    html_escape,
    navigation,
    render_page,
)


def render_config_html(page, payload, *, status_payload=None, event_logger=None):
    """Render one requested setup, calibration, switch, or info page."""
    switch_present = bool(payload["switch"]["present"])
    nav = navigation(
        switch_present=switch_present,
        automations=payload["profile"] == "nodusweb" and switch_present,
        current={
            "time": "/time-setup",
            "mqtt": "/mqtt-setup",
            "display": "/display-setup",
            "calibration": "/calibration",
            "switch": "/switch-setup",
            "info": "/info",
        }.get(page, "/setup"),
    )
    if page == "calibration":
        title, body, script = _calibration_page(payload)
    elif page == "switch":
        title, body, script = _switch_page(payload)
    elif page == "info":
        title, body, script = _info_page(payload, status_payload or {})
    else:
        title, body, script = _setup_page(page, payload, event_logger=event_logger)
    # CircuitPython does not immediately reclaim all temporary list and format
    # objects created by the page-specific renderer. Release them before the
    # largest contiguous allocation: the final full-page wrapper.
    try:
        gc.collect()
    except Exception:
        pass
    return render_page(
        payload["network"]["hostname"],
        title,
        body,
        nav=nav,
        script=script,
        style=_page_style(page),
    )


def _setup_page(page, payload, *, event_logger=None):
    """Build only the requested settings form and its save controls."""
    stage = "fields"
    try:
        rows = []
        fields = ()
        if page == "setup":
            title = "Network"
            section = "Network"
            values = payload["network"]
            fields = (
                ("SSID", "SSID", "ssid", values["ssid"], "text"),
                (
                    "Password",
                    "PASSWORD",
                    "wifi_password",
                    values["password"],
                    "password",
                ),
                ("Hostname", "HOSTNAME", "hostname", values["hostname"], "text"),
            )
        elif page == "time":
            title = "Time"
            section = "Time"
            values = payload["time"]
            fields = (
                ("Time Zone", "TZ", "time_tz", values["tz"], "text"),
                (
                    "TZ Offset",
                    "TZ_OFFSET",
                    "time_offset",
                    values["tz_offset"],
                    "number",
                ),
                ("TZ Name", "TZ_NAME", "time_name", values["tz_name"], "text"),
                (
                    "NTP Server",
                    "NTP_SERVER",
                    "ntp_server",
                    values["ntp_server"],
                    "text",
                ),
                (
                    "NTP Server IP",
                    "NTP_SERVER_IP",
                    "ntp_ip",
                    values["ntp_server_ip"],
                    "text",
                ),
            )
        elif page == "mqtt":
            title = "Profile & MQTT"
            section = "MQTT"
            values = payload["mqtt"]
            stage = "profiles"
            profiles = []
            for profile in ("nodusweb", "sensorius", "weewx", "homeassistant"):
                selected = " selected" if profile == payload["profile"] else ""
                profiles.append(
                    '<option value="{}"{}>{}</option>'.format(
                        profile, selected, profile
                    )
                )
            rows.append(
                '<div class="row"><label>Profile</label><select id="profile" data-section="Profile" data-key="ACTIVE_PROFILE">{}</select></div>'.format(
                    "".join(profiles)
                )
            )
            del profiles
            gc.collect()
            fields = (
                ("Broker", "BROKER", "mqtt_broker", values["broker"], "text"),
                ("Username", "USERNAME", "mqtt_user", values["username"], "text"),
                (
                    "Password",
                    "PASSWORD",
                    "mqtt_password",
                    values["password"],
                    "password",
                ),
                ("Port", "PORT", "mqtt_port", values["port"], "number"),
                ("Use TLS", "USE_TLS", "mqtt_tls", values["use_tls"], "checkbox"),
            )
        else:
            title = "Sensor Display"
            section = "Sensor"
            values = payload["sensor"]
            rows.append(
                _setting_input(
                    section, "Location", "LOCATION", "location", values["location"]
                )
            )
            rows.append('<div class="g">')
            stage = "display_rows"
            for index, metric in enumerate(values["display_metrics"], 1):
                suffix = str(index)
                rows.append(
                    _setting_input(
                        "Display",
                        "Metric " + suffix,
                        "METRIC_" + suffix,
                        "metric_" + suffix,
                        metric,
                        row_class="f",
                    )
                )
                rows.append(
                    _setting_input(
                        "Display.Style",
                        "Style " + suffix,
                        "METRIC_" + suffix,
                        "style_" + suffix,
                        values["display_styles"][index - 1],
                        row_class="f",
                    )
                )
            rows.append("</div>")
        for field in fields:
            rows.append(_setting_input(section, *field))
        stage = "body_join"
        body = "".join(rows)
        del rows, fields
        gc.collect()
        restart = (
            '<button onclick="saveSetup(true)">Save &amp; Restart</button>'
            if page in ("setup", "mqtt")
            else ""
        )
        body += '<div class="actions">{}<span id="setup_status" class="status"></span><button onclick="saveSetup(false)">Save</button></div>'.format(
            restart
        )
        script = """let saving=false;const e=id=>document.getElementById(id);async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function saveSetup(restart){if(saving)return;saving=true;try{const u=[];document.querySelectorAll('[data-section]').forEach(x=>u.push({section:x.dataset.section,key:x.dataset.key,value:x.type==='checkbox'?x.checked:x.value}));const r=await post('/config',{updates:u}),ok=r.success&&r.persistence_mode==='persisted';e('setup_status').textContent=ok?'Saved':r.success?'Applied until reboot; not saved':'Not fully saved';if(restart&&ok)await post('/restart',{mode:'soft'})}catch(err){e('setup_status').textContent='Save response interrupted; check settings.'}finally{saving=false}}"""
        return title, body, script
    except MemoryError:
        _log_setup_stage(event_logger, page, "memory_error_" + stage)
        raise


def _setting_input(
    section, label, key, field_id, value, input_type="text", row_class="row"
):
    """Render one scalar setting with its section and key for form submission."""
    if input_type == "checkbox":
        value_attribute = " checked" if value else ""
    else:
        value_attribute = ' value="{}"'.format(html_escape(value))
    return '<div class="{}"><label>{}</label><input id="{}" type="{}"{} data-section="{}" data-key="{}"></div>'.format(
        row_class,
        html_escape(label),
        field_id,
        input_type,
        value_attribute,
        section,
        key,
    )


def _log_setup_stage(event_logger, page, phase):
    if not callable(event_logger):
        return
    path = {
        "time": "/time-setup",
        "mqtt": "/mqtt-setup",
        "display": "/display-setup",
    }.get(page, "/setup")
    try:
        event_logger(
            "request path={} phase=setup_{} free_mem={}".format(
                path, phase, _free_mem_text()
            )
        )
    except Exception:
        pass


def _free_mem_text():
    mem_free = getattr(gc, "mem_free", None)
    if not callable(mem_free):
        return "unknown"
    try:
        return str(mem_free())
    except Exception:
        return "unknown"


def _calibration_page(payload):
    sensor = payload["sensor"]
    values = sensor["calibration_device"]
    rows = []
    for key in _calibration_keys(sensor):
        rows.append(
            '<div class="cal"><label>{}</label><input id="cal_{}" type="number" step="any" value="{}"></div>'.format(
                html_escape(_calibration_label(key)),
                html_escape(key),
                html_escape(values.get(key, 0)),
            )
        )
    body = '<div class="g">{}</div><div class="actions"><button class="danger" onclick="restartDevice()">Restart Device</button><span id="cal_status" class="status"></span><button onclick="saveCalibration()">Save</button></div>'.format(
        "".join(rows)
    )
    script = """async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function saveCalibration(){const u=[];document.querySelectorAll('[id^=cal_]').forEach(e=>u.push({section:'Calibration.Device',key:e.id.slice(4),value:e.value}));const r=await post('/config',{updates:u});document.getElementById('cal_status').textContent=r.success?'Calibration saved.':JSON.stringify(r)}async function restartDevice(){await post('/restart',{mode:'soft'})}"""
    return "Device Calibration", body, script


def _switch_page(payload):
    switch = payload["switch"]
    controls = []
    for channel in switch["channels"]:
        state = bool(channel["state"])
        controls.append(
            '<div class="sf"><label>{}</label><div class="ctl"><button class="{}" data-channel="{}" data-state="{}" onclick="toggleSwitch(this)">{}</button><input id="{}_label" value="{}"></div></div>'.format(
                html_escape(channel["key"]),
                "on" if state else "off",
                html_escape(channel["channel_id"]),
                "true" if state else "false",
                "ON" if state else "OFF",
                html_escape(channel["key"]),
                html_escape(channel["label"]),
            )
        )
    body = '<div class="loc"><label>Location</label><input id="switch_location" value="{}"></div><div class="g">{}</div><div id="switch_status" class="status"></div><div class="actions"><span id="save_status" class="status"></span><button onclick="saveSwitch()">Save</button></div>'.format(
        html_escape(switch["location"]),
        "".join(controls),
    )
    script = """async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function toggleSwitch(b){const state=b.dataset.state!=='true',r=await post('/set-switch-state',{channel_id:b.dataset.channel,state:state});if(r.success){b.dataset.state=state?'true':'false';b.textContent=state?'ON':'OFF';b.className=state?'on':'off'}document.getElementById('switch_status').textContent=r.success?(state?'Switch turned on.':'Switch turned off.'):JSON.stringify(r)}async function saveSwitch(){const u=[{section:'Switch',key:'SWITCH_LOCATION',value:document.getElementById('switch_location').value}];for(let i=1;i<=2;i++){const e=document.getElementById('SWITCH_'+i+'_label');if(e)u.push({section:'Switch',key:'SWITCH_'+i+'_LABEL',value:e.value})}const r=await post('/config',{updates:u});document.getElementById('save_status').textContent=r.success?'Switch settings saved.':JSON.stringify(r)}"""
    return "Switch Settings", body, script


def _info_page(payload, status):
    network = status.get("network") or {}
    sensor = payload["sensor"]
    switch = payload["switch"]
    rows = (
        ("Firmware", payload["version"]),
        ("Profile", payload["profile"]),
        ("SSID", payload["network"]["ssid"]),
        ("IPv4", network.get("ipv4addr", "")),
        ("WiFi Recovery Count", network.get("wifi_recovery_count", 0)),
        ("Sensor ID", sensor["sensor_id"]),
        ("Sensor", sensor["hardware"] or sensor["device"]),
        ("Interface", sensor["interface"]),
        ("Switch ID", switch["device_id"]),
        ("Switch Channels", switch["channel_count"]),
    )
    network_rows = rows[:5]
    device_rows = rows[5:]
    body = '<details open><summary>Network Info</summary><div class="group"><table>{}</table></div></details><details open><summary>Device Info</summary><div class="group"><table>{}</table></div></details>'.format(
        "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(
                html_escape(label), html_escape(value)
            )
            for label, value in network_rows
        ),
        "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(
                html_escape(label), html_escape(value)
            )
            for label, value in device_rows
        ),
    )
    return "Nodus Info", body, ""


def _calibration_keys(sensor):
    device = str(sensor.get("device", "") or "").lower()
    if device == "co2":
        return ("TEMP_OFFSET", "RH_OFFSET", "CO2_OFFSET", "ALTITUDE_METERS")
    if device == "aqi":
        return (
            "TEMP_OFFSET",
            "RH_OFFSET",
            "AQI_OFFSET",
            "GAS_OFFSET",
            "ALTITUDE_METERS",
        )
    if device == "lux":
        return ("LUX_OFFSET", "PPFD_OFFSET")
    if device in {"apvpd", "apvpd_aht"}:
        return ("APVPD_TEMP_CAL_VAL", "APVPD_RH_CAL_VAL", "ALTITUDE_METERS")
    if device == "soil":
        return (
            "SOIL_TEMP_CAL_VAL",
            "SOIL_MOIST_CAL_VAL",
            "SOIL_PH_CAL_VAL",
            "SOIL_EC_CAL_VAL",
        )
    return ("TEMP_OFFSET", "RH_OFFSET", "ALTITUDE_METERS")


def _calibration_label(key):
    """Return a compact human-readable calibration field label."""
    if key == "TEMP_OFFSET":
        return "Temperature Offset"
    if key == "RH_OFFSET":
        return "Relative Humidity Offset"
    if key == "CO2_OFFSET":
        return "CO2 Offset"
    if key == "AQI_OFFSET":
        return "AQI Offset"
    if key == "GAS_OFFSET":
        return "Gas Offset"
    if key == "ALTITUDE_METERS":
        return "Altitude Meters"
    if key == "LUX_OFFSET":
        return "Lux Offset"
    if key == "PPFD_OFFSET":
        return "PPFD Offset"
    if key == "APVPD_TEMP_CAL_VAL":
        return "APVPD Temperature"
    if key == "APVPD_RH_CAL_VAL":
        return "APVPD Relative Humidity"
    if key == "SOIL_TEMP_CAL_VAL":
        return "Soil Temperature"
    if key == "SOIL_MOIST_CAL_VAL":
        return "Soil Moisture"
    if key == "SOIL_PH_CAL_VAL":
        return "Soil pH"
    if key == "SOIL_EC_CAL_VAL":
        return "Soil EC"
    return str(key).replace("_", " ")


def _page_style(page):
    """Return only the compact CSS needed by one configuration page."""
    if page == "switch":
        return ".loc{margin-bottom:9px}.loc label,.sf>label{display:block;margin-bottom:5px}.g{display:grid;grid-template-columns:1fr 1fr;gap:9px}.ctl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px}.ctl input{width:90%}"
    if page == "calibration":
        return ".g{display:grid;grid-template-columns:1fr 1fr;gap:9px}.cal{grid-column:1}.cal label{display:block;margin-bottom:5px}"
    if page == "display":
        return ".g{display:grid;grid-template-columns:1fr 1fr;gap:9px}.f label{display:block;margin-bottom:5px}.loc{margin-bottom:9px}"
    return ""
