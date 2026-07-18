"use strict";

const nodusApiOrigin = `${location.protocol}//${location.hostname}:8767`;
const nodusGuardUntil = {};

function nodusStateElement(channelId) {
  return document.querySelector(
    `.switch-current[data-channel-id="${CSS.escape(channelId)}"]`
  );
}

function nodusApplyChannel(channel) {
  const element = nodusStateElement(channel.channel_id);
  if (!element) return;
  const automated = Boolean(channel.automation_enabled);
  element.dataset.automationEnabled = automated ? "true" : "false";
  element.disabled = automated || Date.now() < (nodusGuardUntil[channel.channel_id] || 0);
  element.classList.toggle("mode-automated", automated);
  element.classList.toggle("mode-manual", !automated);
  element.classList.toggle("state-on", channel.state === "ON");
  element.classList.toggle("state-off", channel.state === "OFF");
  element.querySelector(".switch-state-value").textContent = channel.state;
  element.querySelector(".switch-control-mode").textContent = channel.automation;
}

async function nodusApi(path, options = {}) {
  const response = await fetch(`${nodusApiOrigin}${path}`, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

async function nodusRefreshSwitches() {
  try {
    const status = await nodusApi("/api/status");
    (status.switch?.channels || []).forEach(nodusApplyChannel);
  } catch (_error) {
    // The archived dashboard remains readable when the live helper is offline.
  }
}

async function nodusToggle(event) {
  const element = event.currentTarget;
  if (element.dataset.automationEnabled === "true" || element.disabled) return;
  const channelId = element.dataset.channelId;
  element.disabled = true;
  try {
    const result = await nodusApi("/api/switch/toggle", {
      method: "POST",
      body: JSON.stringify({channel_id: channelId})
    });
    nodusGuardUntil[channelId] = Date.now() + (result.guard_seconds || 5) * 1000;
    nodusApplyChannel(result.channel);
  } catch (error) {
    window.alert(error.message);
    element.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".switch-current[data-channel-id]").forEach(element => {
    element.addEventListener("click", nodusToggle);
  });
  nodusRefreshSwitches();
  window.setInterval(nodusRefreshSwitches, 5000);
});
