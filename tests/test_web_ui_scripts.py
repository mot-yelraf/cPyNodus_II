"""Execute generated UI scripts against controlled browser events.

Node supplies JavaScript execution only; fake fetches and clocks test request
ordering and restart decisions without browser or device mutations.
"""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from test_web_config import _load_runtime_config

from cpynodus_ii.features.web_config_ui import render_config_html
from cpynodus_ii.features.web_handlers import (
    build_config_page_payload,
    build_status_payload,
)
from cpynodus_ii.features.web_status_ui import render_status_html


class _SettingsFields(HTMLParser):
    """Collect the actual generated controls for the JavaScript fixture."""

    def __init__(self):
        super().__init__()
        self.fields = []
        self.select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("input", "select") and "data-section" in attrs:
            field = {
                "value": attrs.get("value", ""),
                "checked": "checked" in attrs,
                "type": attrs.get("type", "select"),
                "dataset": {"section": attrs["data-section"],
                            "key": attrs["data-key"]},
            }
            self.fields.append(field)
            if tag == "select":
                self.select = field
        elif tag == "option" and self.select is not None:
            if "selected" in attrs or not self.select["value"]:
                self.select["value"] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def _execute(html, checks):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute browser-script behavior tests")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    parser = _SettingsFields()
    parser.feed(html)
    harness = """
const assert=require('node:assert/strict'),vm=require('node:vm');
let timers=new Map(),next=0,events={},calls=[],pending=[],requests=[];
const fields=FIELDS;
const elements=new Proxy({}, {get:(o,k)=>o[k]||(o[k]={
 value:'',textContent:'',type:'text',dataset:{}})});
const document={hidden:false,getElementById:k=>elements[k],
 querySelector:()=>elements.button,
 querySelectorAll:()=>fields,
 addEventListener:(k,f)=>events[k]=f};
const context={document,window:{addEventListener:(k,f)=>events[k]=f},
 console,AbortController,
 setTimeout:(f,ms)=>{timers.set(++next,{f,ms});return next},
 clearTimeout:k=>timers.delete(k),
 fetch:(path,options)=>{calls.push(path);requests.push(options);
  return new Promise((resolve,reject)=>{
  pending.push(p=>resolve({json:async()=>p}));
  options?.signal?.addEventListener('abort',()=>reject(new Error('aborted')));
 })}};
vm.createContext(context);vm.runInContext(SCRIPT,context);
async function drain(){for(let i=0;i<12;i++)await Promise.resolve()}
function fire(ms){const item=[...timers].find(([id,t])=>t.ms===ms);
 assert.ok(item,'missing timer '+ms);timers.delete(item[0]);return item[1].f()}
(async()=>{CHECKS})().catch(e=>{console.error(e);process.exitCode=1});
""".replace("SCRIPT", json.dumps(script)).replace(
        "FIELDS", json.dumps(parser.fields)
    ).replace("CHECKS", checks)
    result = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_status_serializes_refresh_and_switch_and_pauses_hidden_pages():
    html = render_status_html(build_status_payload(
        _load_runtime_config(), version="test",
    ))
    _execute(html, """
fire(1000);context.refresh();context.setSwitch('S1',true);
assert.deepEqual(calls,['/current-data']);
pending.shift()({sensor:{},switch:{channels:[]}});await drain();
assert.equal([...timers.values()].filter(t=>t.ms===15000).length,1);
context.setSwitch('S1',true);context.refresh();context.setSwitch('S2',true);
assert.deepEqual(calls,['/current-data','/set-switch-state']);
pending.shift()({success:true});await drain();fire(100);await drain();
assert.equal(calls.length,3);
document.hidden=true;events.visibilitychange();await drain();
assert.equal(timers.size,0);context.refresh();assert.equal(calls.length,3);
document.hidden=false;events.visibilitychange();fire(100);await drain();
assert.equal(calls.length,4);
fire(12000);await drain();
assert.ok([...timers.values()].some(t=>t.ms===5000||t.ms===15000));
fire(5000);await drain();events.pagehide();await drain();
assert.equal(timers.size,0);context.refresh();assert.equal(calls.length,5);
events.pageshow();fire(100);assert.equal(calls.length,6);
""")


@pytest.mark.parametrize("response,restarts", [
    ({"success": True, "persistence_mode": "volatile", "errors": []}, 0),
    ({"success": False, "persistence_mode": "volatile",
      "errors": ["settings_not_writable"]}, 0),
    ({"success": False, "persistence_mode": "partial",
      "errors": ["persistence_failed"]}, 0),
    ({"success": True, "persistence_mode": "persisted", "errors": []}, 1),
])
def test_setup_restarts_only_after_a_successful_persisted_save(response, restarts):
    runtime = _load_runtime_config()
    html = render_config_html("setup", build_config_page_payload(
        runtime, version="test", page="setup",
    ))
    _execute(html, """
context.saveSetup(true);await drain();assert.deepEqual(calls,['/config']);
pending.shift()(RESPONSE);await drain();
assert.equal(calls.filter(p=>p==='/restart').length,RESTARTS);
if(RESTARTS===0)assert.ok(
 elements.setup_status.textContent.includes('until reboot')||
 elements.setup_status.textContent.includes('Not fully saved'));
""".replace("RESPONSE", json.dumps(response)).replace("RESTARTS", str(restarts)))


@pytest.mark.parametrize("page,expected", [
    ("setup", {"Network": ["SSID", "PASSWORD", "HOSTNAME"]}),
    ("time", {"Time": ["TZ", "TZ_OFFSET", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP"]}),
    ("mqtt", {"Profile": ["ACTIVE_PROFILE"],
              "MQTT": ["BROKER", "USERNAME", "PASSWORD", "PORT", "USE_TLS"]}),
    ("display", {"Sensor": ["LOCATION"],
                 "Display": ["METRIC_" + str(i) for i in range(1, 7)],
                 "Display.Style": ["METRIC_" + str(i) for i in range(1, 7)]}),
])
def test_settings_forms_submit_only_their_own_fields(page, expected):
    runtime = _load_runtime_config()
    html = render_config_html(page, build_config_page_payload(
        runtime, version="test", page=page,
    ))
    _execute(html, """
context.saveSetup(false);context.saveSetup(false);await drain();
assert.deepEqual(calls,['/config']);
const updates=JSON.parse(requests[0].body).updates,keys={};
for(const u of updates)(keys[u.section]??=[]).push(u.key);
assert.deepEqual(keys,EXPECTED);
if(keys.MQTT)assert.equal(typeof updates.find(u=>u.key==='USE_TLS').value,'boolean');
if(keys.MQTT){
 assert.equal(updates.find(u=>u.key==='ACTIVE_PROFILE').value,PROFILE_VALUE);
 assert.equal(updates.find(u=>u.key==='USE_TLS').value,TLS_VALUE);
}
pending.shift()({success:true,persistence_mode:'persisted'});await drain();
assert.deepEqual(calls,['/config']);
""".replace("EXPECTED", json.dumps(expected)).replace(
        "PROFILE_VALUE", json.dumps(runtime.active_profile)
    ).replace("TLS_VALUE", json.dumps(runtime.mqtt.use_tls)))
