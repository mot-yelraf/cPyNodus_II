"use strict";

const $ = id => document.getElementById(id);
const dayNames = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
let state = {};
let automations = [];
let metricOptions = {};
let channelOptions = [];
const apiOrigin = location.port === "8767"
  ? ""
  : `${location.protocol}//${location.hostname}:8767`;

function selectSetupPanel() {
  const selected = location.hash === "#switch" ? "switch" : "sensor";
  document.querySelectorAll(".setup-tabs [data-panel]").forEach(link => {
    if (link.dataset.panel === selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });
  ["sensor", "switch"].forEach(name => {
    $(name).hidden = name !== selected;
  });
}

function notice(message, error = false) {
  const element = $("notice");
  element.textContent = message || "";
  element.className = message ? (error ? "error" : "ok") : "";
}

async function api(path, options = {}) {
  const response = await fetch(`${apiOrigin}${path}`, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function option(select, value, label = value) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = label;
  select.appendChild(element);
}

function field(labelText, control) {
  const label = document.createElement("label");
  const text = document.createElement("span");
  text.textContent = labelText;
  label.append(text, control);
  return {label, text};
}

function button(text, className = "secondary compact") {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = text;
  element.className = className;
  return element;
}

function numberInput(value = 0, min = null) {
  const input = document.createElement("input");
  input.type = "number";
  input.step = "any";
  input.value = value;
  if (min !== null) input.min = min;
  return input;
}

function metricInfo(name) {
  return metricOptions[name] || {name, label: name, unit: ""};
}

function metricLabel(item) {
  return item.unit ? `${item.label} — ${item.unit}` : item.label;
}

function channelSelect(value = "") {
  const select = document.createElement("select");
  channelOptions.forEach(item => option(select, item.channel_id, item.label));
  select.value = value || channelOptions[0]?.channel_id || "";
  return select;
}

function metricSelect(value = "") {
  const select = document.createElement("select");
  Object.values(metricOptions).forEach(item => {
    option(select, item.name, metricLabel(item));
  });
  select.value = value || Object.keys(metricOptions)[0] || "";
  return select;
}

function daysPicker(selected = dayNames) {
  const wrapper = document.createElement("div");
  wrapper.className = "days";
  dayNames.forEach(day => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = day;
    input.checked = selected.includes(day);
    label.append(input, day.toUpperCase());
    wrapper.appendChild(label);
  });
  return wrapper;
}

function readDays(row) {
  return [...row.querySelectorAll(".days input:checked")].map(item => item.value);
}

function conditionTypeSelect(value) {
  const select = document.createElement("select");
  select.className = "condition-type";
  [
    ["metric", "Metric threshold"],
    ["time", "Time of day / days"],
    ["timer", "Repeating timer"],
    ["astral", "Astral"],
    ["switch", "Switch state"],
    ["or", "OR separator"]
  ].forEach(item => option(select, item[0], item[1]));
  select.value = value || "metric";
  return select;
}

function addCondition(condition = {type: "metric"}) {
  const row = document.createElement("article");
  row.className = "builder-row condition-row";
  const head = document.createElement("div");
  head.className = "builder-row-head";
  const type = conditionTypeSelect(condition.type);
  const remove = button("Remove", "danger compact");
  remove.onclick = () => row.remove();
  head.append(field("Condition type", type).label, remove);
  const body = document.createElement("div");
  body.className = "condition-fields fields";
  row.append(head, body);

  function render() {
    body.replaceChildren();
    const kind = type.value;
    if (kind === "or") {
      const chip = document.createElement("strong");
      chip.className = "or-chip";
      chip.textContent = "OR — begin another condition group";
      body.appendChild(chip);
      return;
    }
    if (kind === "metric") {
      const metric = metricSelect(condition.metric);
      metric.className = "condition-metric";
      const direction = document.createElement("select");
      direction.className = "condition-direction";
      option(direction, "above", "Above");
      option(direction, "below", "Below");
      direction.value = condition.direction || "above";
      const on = numberInput(condition.on_threshold ?? 1);
      on.className = "condition-on";
      const off = numberInput(condition.off_threshold ?? 0);
      off.className = "condition-off";
      const onField = field("ON threshold", on);
      const offField = field("OFF threshold", off);
      function units() {
        const unit = metricInfo(metric.value).unit;
        onField.text.textContent = unit ? `ON threshold (${unit})` : "ON threshold";
        offField.text.textContent = unit ? `OFF threshold (${unit})` : "OFF threshold";
      }
      metric.onchange = units;
      units();
      body.append(
        field("Metric", metric).label,
        field("Direction", direction).label,
        onField.label,
        offField.label
      );
      return;
    }
    if (kind === "time") {
      const start = document.createElement("input");
      start.type = "time";
      start.className = "condition-start";
      start.value = condition.start || "00:00";
      const end = document.createElement("input");
      end.type = "time";
      end.className = "condition-end";
      end.value = condition.end || "00:00";
      const days = daysPicker(condition.days || dayNames);
      body.append(field("Start time", start).label, field("End time", end).label);
      const daysField = field("Days", days).label;
      daysField.className = "wide";
      body.appendChild(daysField);
      return;
    }
    if (kind === "timer") {
      const period = numberInput(condition.period_minutes ?? 60, 2);
      period.className = "condition-period";
      const duration = numberInput(condition.duration_minutes ?? 30, 1);
      duration.className = "condition-duration";
      body.append(
        field("Repeat every (minutes)", period).label,
        field("Active duration (minutes)", duration).label
      );
      return;
    }
    if (kind === "astral") {
      const event = document.createElement("select");
      event.className = "condition-event";
      option(event, "sunrise_to_sunset", "Sunrise to sunset");
      option(event, "sunset_to_sunrise", "Sunset to sunrise");
      option(event, "sunrise", "At/after sunrise");
      option(event, "sunset", "At/after sunset");
      event.value = condition.event || "sunrise_to_sunset";
      const offset = numberInput(condition.offset_minutes ?? 0);
      offset.className = "condition-offset";
      const days = daysPicker(condition.days || dayNames);
      body.append(
        field("Astral event", event).label,
        field("Offset (minutes)", offset).label
      );
      const daysField = field("Days", days).label;
      daysField.className = "wide";
      body.appendChild(daysField);
      return;
    }
    const channel = channelSelect(condition.channel_id);
    channel.className = "condition-channel";
    const stateSelect = document.createElement("select");
    stateSelect.className = "condition-state";
    option(stateSelect, "on", "ON");
    option(stateSelect, "off", "OFF");
    stateSelect.value = condition.state || "on";
    body.append(
      field("Switch", channel).label,
      field("Required state", stateSelect).label
    );
  }

  type.onchange = () => {
    condition = {type: type.value};
    render();
  };
  render();
  $("conditions").appendChild(row);
}

function readCondition(row) {
  const type = row.querySelector(".condition-type").value;
  if (type === "or") return {type};
  if (type === "metric") {
    return {
      type,
      metric: row.querySelector(".condition-metric").value,
      direction: row.querySelector(".condition-direction").value,
      on_threshold: Number(row.querySelector(".condition-on").value),
      off_threshold: Number(row.querySelector(".condition-off").value)
    };
  }
  if (type === "time") {
    return {
      type,
      start: row.querySelector(".condition-start").value,
      end: row.querySelector(".condition-end").value,
      days: readDays(row)
    };
  }
  if (type === "timer") {
    return {
      type,
      period_minutes: Number(row.querySelector(".condition-period").value),
      duration_minutes: Number(row.querySelector(".condition-duration").value),
      anchor_epoch: 0
    };
  }
  if (type === "astral") {
    return {
      type,
      event: row.querySelector(".condition-event").value,
      offset_minutes: Number(row.querySelector(".condition-offset").value),
      days: readDays(row)
    };
  }
  return {
    type,
    channel_id: row.querySelector(".condition-channel").value,
    state: row.querySelector(".condition-state").value
  };
}

function addAction(action = {}) {
  const row = document.createElement("article");
  row.className = "builder-row action-row";
  const fields = document.createElement("div");
  fields.className = "fields";
  const channel = channelSelect(action.channel_id);
  channel.className = "action-channel";
  const activeState = document.createElement("select");
  activeState.className = "action-state";
  option(activeState, "on", "ON");
  option(activeState, "off", "OFF");
  activeState.value = action.state || "on";
  const whenFalse = document.createElement("select");
  whenFalse.className = "action-false";
  option(whenFalse, "opposite", "Set opposite state");
  option(whenFalse, "off", "Set OFF");
  option(whenFalse, "on", "Set ON");
  option(whenFalse, "previous_state", "Restore previous state");
  option(whenFalse, "hold", "Hold current state");
  whenFalse.value = action.false_action || "opposite";
  const delay = numberInput(action.delay_seconds ?? 0, 0);
  delay.className = "action-delay";
  const remove = button("Remove", "danger compact");
  remove.onclick = () => {
    if ($("automation-actions").children.length > 1) row.remove();
  };
  fields.append(
    field("Switch", channel).label,
    field("State when active", activeState).label,
    field("When conditions are false", whenFalse).label,
    field("Action delay (seconds)", delay).label
  );
  row.append(fields, remove);
  $("automation-actions").appendChild(row);
}

function readAction(row) {
  return {
    channel_id: row.querySelector(".action-channel").value,
    state: row.querySelector(".action-state").value,
    false_action: row.querySelector(".action-false").value,
    delay_seconds: Number(row.querySelector(".action-delay").value)
  };
}

function conditionSummary(condition) {
  if (condition.type === "or") return "OR";
  if (condition.type === "metric") {
    const metric = metricInfo(condition.metric);
    const unit = metric.unit ? ` ${metric.unit}` : "";
    return `${metric.label} ${condition.direction} ${condition.on_threshold}${unit}`;
  }
  if (condition.type === "time") {
    return `${condition.start}–${condition.end} ${condition.days.join("/").toUpperCase()}`;
  }
  if (condition.type === "timer") {
    return `${condition.duration_minutes} min every ${condition.period_minutes} min`;
  }
  if (condition.type === "astral") {
    return `${condition.event.replaceAll("_", " ")} ${condition.offset_minutes >= 0 ? "+" : ""}${condition.offset_minutes} min`;
  }
  return `${condition.channel_id} is ${condition.state.toUpperCase()}`;
}

function renderAutomations() {
  const list = $("automation-list");
  list.replaceChildren();
  if (!automations.length) {
    list.textContent = "No automations configured.";
    return;
  }
  automations.forEach(rule => {
    const row = document.createElement("div");
    row.className = "automation-row";
    const text = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("div");
    title.textContent = `${rule.name}${rule.enabled ? "" : " (disabled)"}`;
    detail.textContent = (rule.conditions || []).map(conditionSummary).join(" AND ").replace("AND OR AND", "OR");
    text.append(title, detail);
    const actions = document.createElement("div");
    const edit = button("Edit", "secondary compact");
    edit.onclick = () => editAutomation(rule);
    const remove = button("Delete", "danger compact");
    remove.onclick = () => saveAutomations(
      automations.filter(item => item.name !== rule.name)
    );
    actions.append(edit, remove);
    row.append(text, actions);
    list.appendChild(row);
  });
}

function clearAutomation() {
  $("automation-form").reset();
  $("editing-name").value = "";
  $("minimum-on").value = "300";
  $("minimum-off").value = "300";
  $("automation-enabled").checked = true;
  $("conditions").replaceChildren();
  $("automation-actions").replaceChildren();
  addCondition({type: "metric"});
  addAction({state: "on", false_action: "opposite", delay_seconds: 0});
}

function editAutomation(rule) {
  $("editing-name").value = rule.name;
  $("automation-name").value = rule.name;
  $("automation-enabled").checked = rule.enabled !== false;
  $("minimum-on").value = rule.minimum_on_seconds ?? 300;
  $("minimum-off").value = rule.minimum_off_seconds ?? 300;
  $("stale-action").value = rule.stale_action || "off";
  $("conditions").replaceChildren();
  $("automation-actions").replaceChildren();
  (rule.conditions || []).forEach(addCondition);
  (rule.actions || []).forEach(addAction);
  $("automation-name").focus();
}

async function saveAutomations(next) {
  try {
    notice("Saving automations…");
    await api("/api/automations", {
      method: "POST",
      body: JSON.stringify({automations: next})
    });
    automations = next;
    renderAutomations();
    clearAutomation();
    notice("Automations saved. WeeWX reloads them within five seconds.");
  } catch (error) {
    notice(error.message, true);
  }
}

function renderStatus() {
  $("device").textContent = state.device_id || "Unknown device";
  $("sensor-location").value = state.sensor?.location || "";
  $("switch-location").value = state.switch?.location || "";
  const calibrations = $("calibrations");
  calibrations.replaceChildren();
  (state.sensor?.calibrations || []).forEach(item => {
    const input = numberInput("");
    input.dataset.key = item.key;
    input.placeholder = "No change";
    calibrations.appendChild(
      field(`${item.label}${item.unit ? ` (${item.unit})` : ""}`, input).label
    );
  });
  metricOptions = {};
  (state.metric_options || (state.metrics || []).map(name => ({
    name,
    label: name,
    unit: ""
  }))).forEach(item => { metricOptions[item.name] = item; });
  channelOptions = state.switch?.channels || [];
  const manualControls = $("manual-controls");
  manualControls.replaceChildren();
  channelOptions.forEach(channel => {
    const input = numberInput(channel.countdown_seconds ?? 0, 0);
    input.max = "86400";
    input.step = "1";
    input.dataset.channelId = channel.channel_id;
    manualControls.appendChild(
      field(`${channel.label} countdown (seconds)`, input).label
    );
  });
  renderAutomations();
  clearAutomation();
}

document.addEventListener("DOMContentLoaded", async () => {
  selectSetupPanel();
  window.addEventListener("hashchange", selectSetupPanel);
  try {
    state = await api("/api/status");
    automations = state.automations || [];
    renderStatus();
  } catch (error) {
    notice(error.message, true);
  }

  $("condition-add").onclick = () => addCondition({type: "metric"});
  $("action-add").onclick = () => addAction({state: "on", false_action: "opposite"});
  $("automation-clear").onclick = clearAutomation;

  $("sensor-form").onsubmit = async event => {
    event.preventDefault();
    const calibrations = [...document.querySelectorAll("#calibrations input")]
      .filter(element => element.value !== "")
      .map(element => ({key: element.dataset.key, value: Number(element.value)}));
    try {
      notice("Applying sensor settings…");
      await api("/api/sensor", {
        method: "POST",
        body: JSON.stringify({location: $("sensor-location").value, calibrations})
      });
      notice("Sensor settings confirmed by Nodus.");
    } catch (error) {
      notice(error.message, true);
    }
  };

  $("switch-form").onsubmit = async event => {
    event.preventDefault();
    try {
      notice("Applying switch settings…");
      await api("/api/switch", {
        method: "POST",
        body: JSON.stringify({
          location: $("switch-location").value,
          manual_controls: [...document.querySelectorAll("#manual-controls input")]
            .map(element => ({
              channel_id: element.dataset.channelId,
              countdown_seconds: Number(element.value)
            }))
        })
      });
      notice("Switch settings saved.");
    } catch (error) {
      notice(error.message, true);
    }
  };

  $("automation-form").onsubmit = event => {
    event.preventDefault();
    const rule = {
      name: $("automation-name").value,
      enabled: $("automation-enabled").checked,
      conditions: [...document.querySelectorAll("#conditions .condition-row")]
        .map(readCondition),
      actions: [...document.querySelectorAll("#automation-actions .action-row")]
        .map(readAction),
      stale_action: $("stale-action").value,
      minimum_on_seconds: Number($("minimum-on").value),
      minimum_off_seconds: Number($("minimum-off").value),
      retry_seconds: 60
    };
    const old = $("editing-name").value;
    saveAutomations([...automations.filter(item => item.name !== old), rule]);
  };
});
