# ruff: noqa: E501
"""Render the on-demand NodusWeb local automation editor."""

from cpynodus_ii.features.web_ui_common import navigation, render_page


def render_automation_html(hostname):
    """Render a compact JSON editor backed by the automation API."""
    body = """<p class="hint">Local rules run only in the NodusWeb profile. Conditions support sensor, time, timer, and or; actions target this Nodus only.</p>
<div class="row"><label>Rule</label><select id="rule" onchange="selectRule()"><option value="">New rule</option></select></div>
<div class="row"><label>Rule ID</label><input id="rule_id" maxlength="48" placeholder="lights_on"></div>
<div class="row"><label>Enabled</label><input id="enabled" type="checkbox"></div>
<div class="row"><label>Script JSON</label><textarea id="automation_script" rows="16" spellcheck="false"></textarea></div>
<div class="actions"><button class="danger" onclick="deleteRule()">Delete</button><span id="automation_status" class="status"></span><button onclick="saveRule()">Save</button></div>
<details><summary>Example</summary><div class="group"><pre>{"name":"Lights","conditions":[{"type":"time","start":"07:00","end":"19:00"}],"actions":[{"switch_key":"DEVICE::CHANNEL","set":true,"revert_action":"previous_state"}]}</pre></div></details>"""
    script = """let rules=[];const q=id=>document.getElementById(id);function status(v){q('automation_status').textContent=v}function blank(){q('rule_id').value='';q('enabled').checked=false;q('automation_script').value=JSON.stringify({name:'',conditions:[],actions:[]},null,2)}function selectRule(){const id=q('rule').value,r=rules.find(x=>x.rule_id===id);if(!r){blank();return}q('rule_id').value=r.rule_id;q('enabled').checked=!!r.enabled;q('automation_script').value=JSON.stringify(r.script,null,2)}async function request(path,body){const options=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{cache:'no-store'};const r=await fetch(path,options);return r.json()}async function loadAutomations(){try{const p=await request('/automations');rules=p.rules||[];q('rule').innerHTML='<option value="">New rule</option>'+rules.map(r=>'<option value="'+r.rule_id+'">'+r.rule_id+'</option>').join('');blank();status(rules.length+' rule(s); limits '+p.limits.rules+'/'+p.limits.conditions+'/'+p.limits.actions)}catch(e){status('Unable to load rules.')}}async function saveRule(){let value;try{value=JSON.parse(q('automation_script').value)}catch(e){status('Script JSON is invalid.');return}const p=await request('/automations',{rule_id:q('rule_id').value,enabled:q('enabled').checked,script:value});status(p.success?'Rule saved.':(p.errors||[]).join(', '));if(p.success)await loadAutomations()}async function deleteRule(){const id=q('rule_id').value;if(!id){status('Select a saved rule.');return}const p=await request('/automations/delete',{rule_id:id});status(p.success?'Rule deleted.':(p.errors||[]).join(', '));if(p.success)await loadAutomations()}loadAutomations();"""
    return render_page(
        hostname,
        "Switch Automations",
        body,
        nav=navigation(switch_present=True, automations=True),
        script=script,
    )
