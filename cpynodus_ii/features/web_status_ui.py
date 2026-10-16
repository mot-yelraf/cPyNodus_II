# ruff: noqa: E501
"""Render the minimal initial NodusWeb status page.

``render_status_html`` emits current sensor metrics, switch controls, and the
small polling script used to refresh them. The compact page is kept separate
from configuration UI to limit the first response's heap demand.
"""

from cpynodus_ii.features.web_ui_common import (
    html_escape,
    navigation,
    render_page,
)


def render_status_html(payload):
    """Render current metrics and compact switch controls only."""
    metric_rows = []
    for item in payload["sensor"]["display_metrics"]:
        metric_rows.append(
            '<tr><td>{}</td><td id="sample_{}">{}</td></tr>'.format(
                html_escape(item["metric"]),
                html_escape(item["index"]),
                html_escape(_status_value(item.get("value"))),
            )
        )
    if not metric_rows:
        metric_rows.append("<tr><td colspan='2'>No metrics configured</td></tr>")
    body = '<div class="card"><div class="timestamp" id="sample_timestamp">{}</div><table><tr><th>Metric</th><th>Current</th></tr>{}</table></div>'.format(
        html_escape(payload["sensor"]["display_timestamp"]),
        "".join(metric_rows),
    )
    channels = payload["switch"]["channels"]
    if channels:
        rows = []
        for channel in channels:
            automation = channel.get("automation") or {}
            owned = bool(automation.get("controlled"))
            owner = ""
            if owned:
                owner = "NodusWeb: {}".format(
                    html_escape(", ".join(automation.get("automations") or ()))
                )
            rows.append(
                '<tr><td>{}</td><td><button id="switch_{}" class="{}" '
                'data-channel="{}"{} onclick="toggleSwitch(this)">{}</button>'
                '<small id="owner_{}" class="owner">{}</small></td></tr>'.format(
                    html_escape(channel["label"] or channel["channel_id"]),
                    html_escape(channel["key"]),
                    "on" if channel.get("state") else "off",
                    html_escape(channel["channel_id"]),
                    " disabled" if owned else "",
                    "ON" if channel.get("state") else "OFF",
                    html_escape(channel["key"]),
                    owner,
                )
            )
        body += '<div class="card"><h2>Switches</h2><table><tr><th>Switch</th><th>State</th></tr>{}</table><div id="switch_status" class="status"></div></div>'.format(
            "".join(rows)
        )
    body += '<form method="post" action="/halt"><button>Halt until power cycle</button></form>'
    script = """let guard={},busy=false,timer=0,active=null,gone=false;const d=document;function el(i){return d.getElementById(i)}
function value(v){return v===null||v===undefined?'--':(typeof v==='number'?v.toFixed(2):String(v))}
function schedule(ms){clearTimeout(timer);if(!d.hidden&&!gone)timer=setTimeout(refresh,ms)}
async function request(path,options){const c=new AbortController();active=c;const t=setTimeout(()=>c.abort(),12000);try{const r=await fetch(path,Object.assign({},options,{signal:c.signal}));return await r.json()}finally{clearTimeout(t);active=null}}
async function setSwitch(ch,state){if(busy){el('switch_status').textContent='Request in progress; try again.';return}if(Date.now()<Number(guard[ch]||0))return;busy=true;clearTimeout(timer);guard[ch]=Date.now()+5000;const b=d.querySelector('[data-channel="'+ch+'"]');if(b)b.disabled=true;try{const p=await request('/set-switch-state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel_id:ch,state:state})});el('switch_status').textContent=p.success?(state?'Switch turned on.':'Switch turned off.'):JSON.stringify(p);}catch(e){el('switch_status').textContent='Switch request interrupted; checking state.'}finally{busy=false;schedule(100)}}
function toggleSwitch(b){setSwitch(b.dataset.channel,b.textContent.trim()!=='ON')}
async function refresh(){if(busy||d.hidden||gone)return;busy=true;clearTimeout(timer);try{const p=await request('/current-data',{cache:'no-store'}),s=p.sensor||{};el('sample_timestamp').textContent=s.display_timestamp||'';(s.display_metrics||[]).forEach(i=>{const e=el('sample_'+i.index);if(e)e.textContent=value(i.value)});((p.switch||{}).channels||[]).forEach(i=>{const a=i.automation||{},b=el('switch_'+i.key),o=el('owner_'+i.key);if(b){b.textContent=i.state?'ON':'OFF';b.className=i.state?'on':'off';b.disabled=!!a.controlled||Date.now()<Number(guard[i.channel_id]||0)}if(o)o.textContent=a.controlled?'NodusWeb: '+(a.automations||[]).join(', '):''})}catch(e){}finally{busy=false;schedule(Object.values(guard).some(v=>v>Date.now())?5000:15000)}}
d.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(d.hidden){if(active)active.abort()}else schedule(100)});window.addEventListener('pagehide',()=>{gone=true;clearTimeout(timer);if(active)active.abort()});window.addEventListener('pageshow',()=>{if(gone){gone=false;schedule(100)}});schedule(1000);"""
    switch_present = bool(payload["switch"]["present"])
    nav = navigation(
        switch_present=switch_present,
        automations=payload["profile"] == "nodusweb" and switch_present,
        current="/",
    )
    return render_page(
        payload["network"]["hostname"], "Status", body, nav=nav, script=script
    )


def _status_value(value):
    if value is None:
        return "--"
    if isinstance(value, float):
        return "{:.2f}".format(value)
    return value
