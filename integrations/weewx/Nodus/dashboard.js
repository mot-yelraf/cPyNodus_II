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

function nodusAstronomyData() {
  const root = document.querySelector(".astro-grid[data-astronomy]");
  if (!root) return null;
  try {
    return JSON.parse(window.atob(root.dataset.astronomy));
  } catch (_error) {
    return null;
  }
}

function nodusClock(raw) {
  const match = String(raw || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return "--";
  const hour = Number(match[1]);
  return `${hour % 12 || 12}:${match[2]}${hour < 12 ? "A" : "P"}`;
}

function nodusMinute(raw) {
  const match = String(raw || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  return (Number(match[1]) * 60) + Number(match[2]);
}

function nodusPlaceTimeLabel(id, raw) {
  const element = document.getElementById(id);
  const minute = nodusMinute(raw);
  if (!element || !Number.isFinite(minute)) return;
  const canvasWidth = document.getElementById("sunMoonPositionCanvas")?.width || 880;
  const plotPad = 20;
  const x = plotPad + ((canvasWidth - (2 * plotPad)) * minute / 1440);
  element.style.left = `${(x * 100 / canvasWidth).toFixed(2)}%`;
}

function nodusMoonDate(raw) {
  const match = String(raw || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return "--";
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  return `${match[3]}${months[Number(match[2]) - 1]}${match[1].slice(-2)}`;
}

function nodusMoonView() {
  return document.querySelector('.moon-view-button[data-moon-view="reference"].active')
    ? "reference"
    : "local";
}

function nodusDrawMoon(data) {
  const canvas = document.getElementById("moonPhaseCanvas");
  if (!canvas || !data?.ok) return;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const radius = (Math.min(width, height) / 2) - 3;
  const centerX = width / 2;
  const centerY = height / 2;
  const phase = Number(data.moon_phase_degrees) * Math.PI / 180;
  const lightX = Math.abs(Math.sin(phase));
  const lightZ = -Math.cos(phase);
  const pixels = context.createImageData(width, height);
  for (let py = 0; py < height; py += 1) {
    for (let px = 0; px < width; px += 1) {
      const dx = (px + 0.5 - centerX) / radius;
      const dy = (py + 0.5 - centerY) / radius;
      const rr = (dx * dx) + (dy * dy);
      const offset = ((py * width) + px) * 4;
      if (rr > 1) continue;
      const dz = Math.sqrt(Math.max(0, 1 - rr));
      const edge = Math.max(-1, Math.min(1, ((dx * lightX) + (dz * lightZ)) / 0.045));
      const mix = Math.pow((edge + 1) / 2, 0.82);
      const rim = Math.pow(dz, 0.65);
      pixels.data[offset] = Math.round(76 + (170 * mix) + (7 * rim));
      pixels.data[offset + 1] = Math.round(80 + (166 * mix) + (6 * rim));
      pixels.data[offset + 2] = Math.round(88 + (155 * mix) + (4 * rim));
      pixels.data[offset + 3] = 255;
    }
  }
  const phaseCanvas = document.createElement("canvas");
  phaseCanvas.width = width;
  phaseCanvas.height = height;
  phaseCanvas.getContext("2d").putImageData(pixels, 0, 0);
  const view = nodusMoonView();
  const referenceAngle = Number(data.moon_phase_degrees) > 180 ? 180 : 0;
  const angle = view === "reference"
    ? referenceAngle
    : (Number(data.moon_visible_angle) || 0);
  context.clearRect(0, 0, width, height);
  context.save();
  context.translate(centerX, centerY);
  context.rotate(angle * Math.PI / 180);
  context.drawImage(phaseCanvas, -centerX, -centerY);
  context.restore();
  context.strokeStyle = "#58524a";
  context.lineWidth = 2;
  context.beginPath();
  context.arc(centerX, centerY, radius, 0, Math.PI * 2);
  context.stroke();
  document.getElementById("moonMeta").textContent =
    `${data.moon_phase_label} · ${view === "reference" ? "Reference diagram" : "Local sky view"}`;
}

function nodusDrawPositions(data) {
  const canvas = document.getElementById("sunMoonPositionCanvas");
  if (!canvas || !data?.ok || !Array.isArray(data.points) || !data.points.length) return;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const pad = 20;
  const innerWidth = width - (2 * pad);
  const innerHeight = height - (2 * pad);
  const horizon = pad + (innerHeight * 0.54);
  const elevations = data.points.flatMap(point => [Number(point[1]), Number(point[2])])
    .filter(Number.isFinite);
  const maxElevation = Math.max(20, ...elevations.filter(value => value > 0));
  const minElevation = Math.min(-18, ...elevations.filter(value => value < 0));
  const abovePixels = horizon - pad - 2;
  const belowPixels = height - pad - horizon - 2;
  const x = minute => pad + (innerWidth * minute / 1440);
  const y = elevation => elevation >= 0
    ? horizon - ((Math.min(elevation, maxElevation) / maxElevation) * abovePixels)
    : horizon + ((Math.min(Math.abs(elevation), Math.abs(minElevation)) / Math.abs(minElevation)) * belowPixels);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#d8ecfa";
  context.fillRect(0, 0, width, height);
  context.fillStyle = "#000";
  context.fillRect(pad, horizon, innerWidth, height - pad - horizon);
  context.strokeStyle = "#b8c4c9";
  context.lineWidth = 2;
  context.strokeRect(pad, pad, innerWidth, innerHeight);

  const draw = (index, color) => {
    context.strokeStyle = color;
    context.lineWidth = 6;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.beginPath();
    data.points.forEach((point, pointIndex) => {
      context[pointIndex ? "lineTo" : "moveTo"](x(point[0]), y(point[index]));
    });
    context.stroke();
  };
  draw(1, "#f5d438");
  draw(2, "#63b9ec");

  const current = data.points.reduce((best, point) =>
    Math.abs(point[0] - data.current_minute) < Math.abs(best[0] - data.current_minute) ? point : best,
  data.points[0]);
  [[current[1], "#ffe426", "#ffad16"], [current[2], "#d9eef9", "#4685a9"]].forEach(marker => {
    context.fillStyle = marker[1];
    context.strokeStyle = marker[2];
    context.lineWidth = 5;
    context.beginPath();
    context.arc(x(current[0]), y(marker[0]), 15, 0, Math.PI * 2);
    context.fill();
    context.stroke();
  });
}

function nodusDrawTinyMoon(context, centerX, centerY, phaseDegrees, angle) {
  const size = 24;
  const radius = 10;
  const center = size / 2;
  const phase = Number(phaseDegrees) * Math.PI / 180;
  const lightX = Math.abs(Math.sin(phase));
  const lightZ = -Math.cos(phase);
  const moon = document.createElement("canvas");
  moon.width = size;
  moon.height = size;
  const moonContext = moon.getContext("2d");
  const pixels = moonContext.createImageData(size, size);
  for (let py = 0; py < size; py += 1) {
    for (let px = 0; px < size; px += 1) {
      const dx = (px + 0.5 - center) / radius;
      const dy = (py + 0.5 - center) / radius;
      const rr = (dx * dx) + (dy * dy);
      const offset = ((py * size) + px) * 4;
      if (rr > 1) continue;
      const dz = Math.sqrt(Math.max(0, 1 - rr));
      const edge = Math.max(-1, Math.min(1, ((dx * lightX) + (dz * lightZ)) / 0.08));
      const mix = Math.pow((edge + 1) / 2, 0.82);
      pixels.data[offset] = Math.round(75 + (175 * mix));
      pixels.data[offset + 1] = Math.round(79 + (171 * mix));
      pixels.data[offset + 2] = Math.round(87 + (160 * mix));
      pixels.data[offset + 3] = 255;
    }
  }
  moonContext.putImageData(pixels, 0, 0);
  moonContext.strokeStyle = "#68645d";
  moonContext.lineWidth = 1;
  moonContext.beginPath();
  moonContext.arc(center, center, radius, 0, Math.PI * 2);
  moonContext.stroke();
  context.save();
  context.translate(centerX, centerY);
  context.rotate((Number(angle) || 0) * Math.PI / 180);
  context.drawImage(moon, -center, -center);
  context.restore();
}

function nodusDraw29Days(data) {
  const canvas = document.getElementById("sunMoon29Canvas");
  const meta = document.getElementById("sunMoon29Meta");
  const days = Array.isArray(data?.position_29d) ? data.position_29d : [];
  if (!canvas || !meta || days.length < 2) return;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const padX = 18;
  const plotTop = 12;
  const plotBottom = 168;
  const horizon = plotTop + ((plotBottom - plotTop) * 0.5);
  const labelY = 181;
  const moonY = 222;
  const totalMinutes = days.length * 1440;
  const x = minute => padX + ((width - (2 * padX)) * minute / totalMinutes);
  const elevations = days.flatMap(day => [...(day.sun || []), ...(day.moon || [])])
    .map(point => Number(point[1]))
    .filter(Number.isFinite);
  const maxElevation = Math.max(20, ...elevations.filter(value => value > 0));
  const minElevation = Math.min(-18, ...elevations.filter(value => value < 0));
  const y = elevation => elevation >= 0
    ? horizon - ((Math.min(elevation, maxElevation) / maxElevation) * (horizon - plotTop - 2))
    : horizon + ((Math.min(Math.abs(elevation), Math.abs(minElevation)) / Math.abs(minElevation)) * (plotBottom - horizon - 2));

  context.clearRect(0, 0, width, height);
  context.fillStyle = "#d8ecfa";
  context.fillRect(padX, plotTop, width - (2 * padX), horizon - plotTop);
  context.fillStyle = "#000";
  context.fillRect(padX, horizon, width - (2 * padX), plotBottom - horizon);
  context.strokeStyle = "rgba(80, 92, 98, 0.22)";
  context.lineWidth = 1;
  for (let dayIndex = 0; dayIndex <= days.length; dayIndex += 1) {
    const dayX = x(dayIndex * 1440);
    context.beginPath();
    context.moveTo(dayX, plotTop);
    context.lineTo(dayX, height - 8);
    context.stroke();
  }

  const drawPath = (key, color) => {
    context.strokeStyle = color;
    context.lineWidth = 3;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.beginPath();
    let started = false;
    days.forEach((day, dayIndex) => {
      (day[key] || []).forEach(point => {
        const pointX = x((dayIndex * 1440) + Number(point[0]));
        const pointY = y(Number(point[1]));
        context[started ? "lineTo" : "moveTo"](pointX, pointY);
        started = true;
      });
    });
    context.stroke();
  };
  drawPath("sun", "#f3d34a");
  drawPath("moon", "#69bdf2");

  context.fillStyle = "#4d5055";
  context.font = "13px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "top";
  days.forEach((day, dayIndex) => {
    const centerX = x((dayIndex * 1440) + 720);
    context.fillText(String(day.label || ""), centerX, labelY);
    nodusDrawTinyMoon(context, centerX, moonY, day.phase, day.angle);
  });
  context.font = "bold 13px system-ui, sans-serif";
  context.textAlign = "right";
  context.fillStyle = "#9a7b17";
  context.fillText("Sun", width - 74, plotTop + 6);
  context.fillStyle = "#327eae";
  context.fillText("Moon", width - 22, plotTop + 6);
  const first = String(days[0].label || "");
  const last = String(days[days.length - 1].label || "");
  meta.textContent = `${first} - ${last}`;
}

function nodusSet29DayOpen(open) {
  const grid = document.querySelector(".astro-grid");
  const daily = document.getElementById("sunMoonPositionCard");
  const expanded = document.getElementById("sunMoon29Card");
  if (!grid || !daily || !expanded) return;
  grid.classList.toggle("astronomy-expanded", open);
  expanded.setAttribute("aria-hidden", open ? "false" : "true");
  if (open) {
    nodusDraw29Days(nodusAstronomyData());
    expanded.focus({preventScroll: true});
  } else {
    daily.focus({preventScroll: true});
  }
}

function nodusRenderAstronomy() {
  const data = nodusAstronomyData();
  if (!data?.ok) return;
  const values = {
    sunRiseTime: data.sunrise,
    sunNoonTime: data.sun_noon,
    sunSetTime: data.sunset,
    moonRiseTime: data.moonrise,
    moonSetTime: data.moonset,
    positionMoonRise: data.moonrise,
    positionMoonSet: data.moonset
  };
  Object.entries(values).forEach(([id, value]) => {
    document.getElementById(id).textContent = nodusClock(value);
    nodusPlaceTimeLabel(id, value);
  });
  document.getElementById("moonLitPct").textContent = `${data.moon_lit_pct}%`;
  document.getElementById("moonNextPhaseLabel").textContent = data.moon_next_phase_label || "Next Phase";
  document.getElementById("moonNextPhaseDate").textContent = nodusMoonDate(data.moon_next_phase_date);
  nodusDrawMoon(data);
  nodusDrawPositions(data);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".switch-current[data-channel-id]").forEach(element => {
    element.addEventListener("click", nodusToggle);
  });
  nodusRefreshSwitches();
  nodusRenderAstronomy();
  document.querySelectorAll(".moon-view-button").forEach(button => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".moon-view-button").forEach(candidate => {
        const active = candidate === button;
        candidate.classList.toggle("active", active);
        candidate.setAttribute("aria-pressed", active ? "true" : "false");
      });
      nodusDrawMoon(nodusAstronomyData());
    });
  });
  const positionCard = document.getElementById("sunMoonPositionCard");
  const expandedCard = document.getElementById("sunMoon29Card");
  positionCard?.addEventListener("click", () => nodusSet29DayOpen(true));
  expandedCard?.addEventListener("click", () => nodusSet29DayOpen(false));
  [positionCard, expandedCard].forEach((card, index) => {
    card?.addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      nodusSet29DayOpen(index === 0);
    });
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.querySelector(".astro-grid.astronomy-expanded")) {
      nodusSet29DayOpen(false);
    }
  });
  window.setInterval(nodusRefreshSwitches, 5000);
});
