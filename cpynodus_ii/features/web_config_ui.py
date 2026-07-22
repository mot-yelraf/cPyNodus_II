# ruff: noqa: E501
"""Render configuration pages separately from the initial NodusWeb status."""

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
        title, body, script = _setup_page(payload, event_logger=event_logger)
    # CircuitPython does not immediately reclaim all temporary list and format
    # objects created by the page-specific renderer. Release them before the
    # largest contiguous allocation: the final full-page wrapper.
    try:
        gc.collect()
    except Exception:
        pass
    return render_page(
        payload["network"]["hostname"], title, body, nav=nav, script=script
    )


def _setup_page(payload, *, event_logger=None):
    stage = "profiles"
    network = payload["network"]
    time_config = payload["time"]
    mqtt = payload["mqtt"]
    sensor = payload["sensor"]
    try:
        profiles = []
        for profile in ("nodusweb", "sensorius", "weewx", "homeassistant"):
            selected = " selected" if profile == payload["profile"] else ""
            profiles.append(
                '<option value="{}"{}>{}</option>'.format(
                    profile, selected, profile
                )
            )
        profiles_html = "".join(profiles)
        del profiles
        gc.collect()

        stage = "display_rows"
        display = []
        for index, metric in enumerate(sensor["display_metrics"], 1):
            display.append(
                '<div class="row"><label>Metric {}</label><input id="metric_{}" value="{}"></div><div class="row"><label>Style {}</label><input id="style_{}" value="{}"></div>'.format(
                    index,
                    index,
                    html_escape(metric),
                    index,
                    index,
                    html_escape(sensor["display_styles"][index - 1]),
                )
            )
        display_html = "".join(display)
        del display
        gc.collect()

        stage = "body_format"
        body = """<details open><summary>Network</summary><div class="group"><div class="row"><label>SSID</label><input id="ssid" value="{ssid}"></div><div class="row"><label>Password</label><input id="wifi_password" type="password" value="{password}"></div><div class="row"><label>Hostname</label><input id="hostname" value="{hostname}"></div></div></details>
<details open><summary>Time</summary><div class="group"><div class="row"><label>Time Zone</label><input id="time_tz" value="{tz}"></div><div class="row"><label>TZ Offset</label><input id="time_offset" type="number" value="{offset}"></div><div class="row"><label>TZ Name</label><input id="time_name" value="{tz_name}"></div><div class="row"><label>NTP Server</label><input id="ntp_server" value="{ntp}"></div><div class="row"><label>NTP Server IP</label><input id="ntp_ip" value="{ntp_ip}"></div></div></details>
<details><summary>Profile and MQTT</summary><div class="group"><div class="row"><label>Profile</label><select id="profile">{profiles}</select></div><div class="row"><label>Broker</label><input id="mqtt_broker" value="{broker}"></div><div class="row"><label>Username</label><input id="mqtt_user" value="{user}"></div><div class="row"><label>Password</label><input id="mqtt_password" type="password" value="{mqtt_password}"></div><div class="row"><label>Port</label><input id="mqtt_port" type="number" value="{port}"></div><div class="row"><label>Use TLS</label><input id="mqtt_tls" type="checkbox"{tls}></div></div></details>
<details><summary>Sensor Display</summary><div class="group"><div class="row"><label>Location</label><input id="location" value="{location}"></div>{display}</div></details><div class="actions"><button onclick="saveSetup(true)">Save &amp; Restart</button><span id="setup_status" class="status"></span><button onclick="saveSetup(false)">Save</button></div>""".format(
            ssid=html_escape(network["ssid"]),
            password=html_escape(network["password"]),
            hostname=html_escape(network["hostname"]),
            tz=html_escape(time_config["tz"]),
            offset=html_escape(time_config["tz_offset"]),
            tz_name=html_escape(time_config["tz_name"]),
            ntp=html_escape(time_config["ntp_server"]),
            ntp_ip=html_escape(time_config["ntp_server_ip"]),
            profiles=profiles_html,
            broker=html_escape(mqtt["broker"]),
            user=html_escape(mqtt["username"]),
            mqtt_password=html_escape(mqtt["password"]),
            port=html_escape(mqtt["port"]),
            tls=" checked" if mqtt["use_tls"] else "",
            location=html_escape(sensor["location"]),
            display=display_html,
        )
        script = """function e(id){return document.getElementById(id)}function add(u,s,k,id){const x=e(id);if(x)u.push({section:s,key:k,value:x.type==='checkbox'?x.checked:x.value})}async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function saveSetup(restart){const u=[];add(u,'Network','SSID','ssid');add(u,'Network','PASSWORD','wifi_password');add(u,'Network','HOSTNAME','hostname');add(u,'Time','TZ','time_tz');add(u,'Time','TZ_OFFSET','time_offset');add(u,'Time','TZ_NAME','time_name');add(u,'Time','NTP_SERVER','ntp_server');add(u,'Time','NTP_SERVER_IP','ntp_ip');add(u,'Profile','ACTIVE_PROFILE','profile');add(u,'MQTT','BROKER','mqtt_broker');add(u,'MQTT','USERNAME','mqtt_user');add(u,'MQTT','PASSWORD','mqtt_password');add(u,'MQTT','PORT','mqtt_port');add(u,'MQTT','USE_TLS','mqtt_tls');add(u,'Sensor','LOCATION','location');for(let i=1;i<=6;i++){add(u,'Display','METRIC_'+i,'metric_'+i);add(u,'Display.Style','METRIC_'+i,'style_'+i)}const r=await post('/config',{updates:u});e('setup_status').textContent=r.success?'Settings saved.':JSON.stringify(r);if(restart&&r.success)await post('/restart',{mode:'soft'})}"""
        return "Setup", body, script
    except MemoryError:
        _log_setup_stage(event_logger, "memory_error_{}".format(stage), 0)
        raise


def _log_setup_stage(event_logger, phase, chars):
    if not callable(event_logger):
        return
    try:
        event_logger(
            "request path=/setup phase=setup_{} chars={} free_mem={}".format(
                phase, chars, _free_mem_text()
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
            '<div class="row"><label>{}</label><input id="cal_{}" type="number" step="any" value="{}"></div>'.format(
                html_escape(key.replace("_", " ")),
                html_escape(key),
                html_escape(values.get(key, 0)),
            )
        )
    body = '{}<div class="actions"><button class="danger" onclick="restartDevice()">Restart Device</button><span id="cal_status" class="status"></span><button onclick="saveCalibration()">Save</button></div>'.format(
        "".join(rows)
    )
    script = """async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function saveCalibration(){const u=[];document.querySelectorAll('[id^=cal_]').forEach(e=>u.push({section:'Calibration.Device',key:e.id.slice(4),value:e.value}));const r=await post('/config',{updates:u});document.getElementById('cal_status').textContent=r.success?'Calibration saved.':JSON.stringify(r)}async function restartDevice(){await post('/restart',{mode:'soft'})}"""
    return "Device Calibration", body, script


def _switch_page(payload):
    switch = payload["switch"]
    controls = []
    for channel in switch["channels"]:
        controls.append(
            '<div class="row"><label>{}</label><input id="{}_label" value="{}"></div><div class="actions"><button class="secondary" onclick="setSwitch(\'{}\',true)">On</button><button class="secondary" onclick="setSwitch(\'{}\',false)">Off</button></div>'.format(
                html_escape(channel["key"]),
                html_escape(channel["key"]),
                html_escape(channel["label"]),
                html_escape(channel["channel_id"]),
                html_escape(channel["channel_id"]),
            )
        )
    body = '<div class="row"><label>Location</label><input id="switch_location" value="{}"></div>{}<table><tr><td>Switch ID</td><td>{}</td></tr><tr><td>Serial Number</td><td>{}</td></tr><tr><td>Enabled Channels</td><td>{}</td></tr></table><div id="switch_status" class="status"></div><div class="actions"><span id="save_status" class="status"></span><button onclick="saveSwitch()">Save</button></div>'.format(
        html_escape(switch["location"]),
        "".join(controls),
        html_escape(switch["device_id"]),
        html_escape(switch["serial_number"]),
        html_escape(switch["channel_count"]),
    )
    script = """async function post(path,p){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});return r.json()}async function setSwitch(ch,state){const r=await post('/set-switch-state',{channel_id:ch,state:state});document.getElementById('switch_status').textContent=r.success?(state?'Switch turned on.':'Switch turned off.'):JSON.stringify(r)}async function saveSwitch(){const u=[{section:'Switch',key:'SWITCH_LOCATION',value:document.getElementById('switch_location').value}];for(let i=1;i<=2;i++){const e=document.getElementById('SWITCH_'+i+'_label');if(e)u.push({section:'Switch',key:'SWITCH_'+i+'_LABEL',value:e.value})}const r=await post('/config',{updates:u});document.getElementById('save_status').textContent=r.success?'Switch settings saved.':JSON.stringify(r)}"""
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
