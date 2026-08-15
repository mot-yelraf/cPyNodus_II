"use strict";

const nodusApiOrigin = `${location.protocol}//${location.hostname}:8767`;
const nodusManagerOrigin = `${location.protocol}//${location.hostname}:8768`;
const nodusGuardUntil = {};
let nodusSwitchTimer = null;
let nodusManagerTimer = null;
let nodusReportTimer = null;
let nodusReportValidator = "";
let nodusReportModified = Date.parse(document.lastModified) || 0;
let nodusAstronomyDetail = null;
let nodusAstronomyDetailRequest = null;
let nodusGraphumRange = 86400;
let nodusGraphumReady = false;
let nodusGraphumTimer = null;
let nodusGraphumRefresh = null;
let nodusGraphumAbort = null;
let nodusGraphumPayload = null;
const nodusMoonSurfaceImage = new Image();

nodusMoonSurfaceImage.addEventListener("load", () => {
  nodusDrawMoon(nodusAstronomyData());
});
nodusMoonSurfaceImage.src = "moon-surface.png?v=1";

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

async function nodusRefreshManagerState() {
  const identity = document.getElementById("device-manager-state");
  document.querySelectorAll(".system-setup-gear").forEach(link => {
    link.href = `${nodusManagerOrigin}/system/`;
  });
  if (!identity) return;
  try {
    const response = await fetch(`${nodusManagerOrigin}/api/system`, {cache: "no-store"});
    const data = await response.json();
    const device = (data.devices || []).find(item => item.device_id === identity.dataset.deviceId);
    const dot = identity.querySelector(".device-status-dot");
    dot.classList.toggle("online", Boolean(device?.online));
    dot.title = device?.online ? "Online" : "Offline";
    const badges = document.getElementById("device-manager-badges");
    badges.replaceChildren();
    [[device?.installed, "Installed", "installed"], [device?.discovered, "Discovered", "discovered"]]
      .forEach(([show, label, kind]) => {
        if (!show) return;
        const badge = document.createElement("span");
        badge.className = `device-manager-badge ${kind}`;
        badge.textContent = label;
        badges.appendChild(badge);
      });
  } catch (_error) {
    // The generated report remains usable when the persistent manager is unavailable.
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

async function nodusLoadAstronomyDetail() {
  if (nodusAstronomyDetail) return nodusAstronomyDetail;
  if (nodusAstronomyDetailRequest) return nodusAstronomyDetailRequest;
  const root = document.querySelector(".astro-grid[data-astronomy-detail]");
  if (!root) throw new Error("Astronomy detail is unavailable");
  nodusAstronomyDetailRequest = fetch(root.dataset.astronomyDetail)
    .then(response => {
      if (!response.ok) throw new Error("Astronomy detail request failed");
      return response.text();
    })
    .then(text => {
      nodusAstronomyDetail = JSON.parse(window.atob(text.trim()));
      return nodusAstronomyDetail;
    })
    .finally(() => {
      nodusAstronomyDetailRequest = null;
    });
  return nodusAstronomyDetailRequest;
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
  const phaseCanvas = document.createElement("canvas");
  phaseCanvas.width = width;
  phaseCanvas.height = height;
  const phaseContext = phaseCanvas.getContext("2d");
  let surfacePixels = null;
  if (nodusMoonSurfaceImage.complete && nodusMoonSurfaceImage.naturalWidth > 0) {
    const surfaceCanvas = document.createElement("canvas");
    surfaceCanvas.width = width;
    surfaceCanvas.height = height;
    const surfaceContext = surfaceCanvas.getContext("2d", {willReadFrequently: true});
    if (surfaceContext) {
      surfaceContext.drawImage(nodusMoonSurfaceImage, 0, 0, width, height);
      surfacePixels = surfaceContext.getImageData(0, 0, width, height).data;
    }
  }
  for (let py = 0; py < height; py += 1) {
    for (let px = 0; px < width; px += 1) {
      const dx = (px + 0.5 - centerX) / radius;
      const dy = (py + 0.5 - centerY) / radius;
      const rr = (dx * dx) + (dy * dy);
      const offset = ((py * width) + px) * 4;
      if (rr > 1) {
        pixels.data[offset + 3] = 0;
        continue;
      }
      const dz = Math.sqrt(Math.max(0, 1 - rr));
      const edge = Math.max(-1, Math.min(1, ((dx * lightX) + (dz * lightZ)) / 0.045));
      const mix = Math.pow((edge + 1) / 2, 0.82);
      const rim = Math.pow(dz, 0.65);
      if (surfacePixels) {
        const textureRadius = Math.min(width, height) * 0.44;
        const sourceX = Math.max(0, Math.min(width - 1, Math.round(centerX + dx * textureRadius)));
        const sourceY = Math.max(0, Math.min(height - 1, Math.round(centerY + dy * textureRadius)));
        const sourceOffset = ((sourceY * width) + sourceX) * 4;
        const brightness = 0.035 + (mix * (0.9 + (dz * 0.065)));
        pixels.data[offset] = Math.min(255, Math.round(surfacePixels[sourceOffset] * brightness));
        pixels.data[offset + 1] = Math.min(255, Math.round(surfacePixels[sourceOffset + 1] * brightness));
        pixels.data[offset + 2] = Math.min(
          255,
          Math.round((surfacePixels[sourceOffset + 2] * brightness) + ((1 - mix) * 5)),
        );
      } else {
        pixels.data[offset] = Math.round(76 + (170 * mix) + (7 * rim));
        pixels.data[offset + 1] = Math.round(80 + (166 * mix) + (6 * rim));
        pixels.data[offset + 2] = Math.round(88 + (155 * mix) + (4 * rim));
      }
      pixels.data[offset + 3] = Math.round(
        255 * Math.max(0, Math.min(1, ((1 - Math.sqrt(rr)) * radius) / 1.35)),
      );
    }
  }
  phaseContext.putImageData(pixels, 0, 0);
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

function nodusMinute(raw) {
  if (typeof raw === "number" && Number.isFinite(raw)) {
    return Math.max(0, Math.min(1440, raw));
  }
  const match = String(raw || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return NaN;
  return Math.max(0, Math.min(1440, (Number(match[1]) * 60) + Number(match[2])));
}

function nodusCurveHermite(t, y0, slope0, y1, slope1, span) {
  const clamped = Math.max(0, Math.min(1, t));
  const squared = clamped * clamped;
  const cubed = squared * clamped;
  return ((2 * cubed - 3 * squared + 1) * y0)
    + ((cubed - 2 * squared + clamped) * span * slope0)
    + ((-2 * cubed + 3 * squared) * y1)
    + ((cubed - squared) * span * slope1);
}

function nodusSmoothSkyYMapper(horizon, top, bottom, maxElevation, minElevation) {
  const abovePixels = Math.max(1, horizon - top);
  const belowPixels = Math.max(1, bottom - horizon);
  const maxElev = Math.max(1, Math.abs(Number(maxElevation) || 1));
  const minElev = Math.max(1, Math.abs(Number(minElevation) || 1));
  const positiveScale = abovePixels / maxElev;
  const negativeScale = belowPixels / minElev;
  const bandLimit = Math.max(1, Math.min(maxElev, minElev));
  const band = Math.max(0.5, Math.min(bandLimit, 12, Math.max(5, bandLimit * 0.35)));
  const horizonSlope = -((positiveScale + negativeScale) / 2);
  return elevation => {
    const value = Number(elevation);
    if (!Number.isFinite(value)) return horizon;
    if (value >= band) return horizon - (Math.min(value, maxElev) * positiveScale);
    if (value <= -band) return horizon - (Math.max(value, -minElev) * negativeScale);
    if (value < 0) {
      return nodusCurveHermite(
        (value + band) / band,
        horizon + (band * negativeScale),
        -negativeScale,
        horizon,
        horizonSlope,
        band,
      );
    }
    return nodusCurveHermite(
      value / band,
      horizon,
      horizonSlope,
      horizon - (band * positiveScale),
      -positiveScale,
      band,
    );
  };
}

function nodusOrbitDisplayPoints(rise, set, peakMinute, topY, horizon, bottomY) {
  if (!Number.isFinite(rise) || !Number.isFinite(set) || rise >= set) return [];
  const peak = Math.max(rise + 1, Math.min(set - 1, peakMinute));
  const previousSet = set - 1440;
  const nextRise = rise + 1440;
  const previousTrough = previousSet + ((rise - previousSet) / 2);
  const nextTrough = set + ((nextRise - set) / 2);
  const riseSlope = -Math.min(
    ((horizon - topY) * Math.PI) / (2 * Math.max(1, peak - rise)),
    ((bottomY - horizon) * Math.PI) / (2 * Math.max(1, rise - previousTrough)),
  );
  const setSlope = Math.min(
    ((horizon - topY) * Math.PI) / (2 * Math.max(1, set - peak)),
    ((bottomY - horizon) * Math.PI) / (2 * Math.max(1, nextTrough - set)),
  );
  const keys = [
    {m: previousSet, y: horizon, slope: setSlope},
    {m: previousTrough, y: bottomY, slope: 0},
    {m: rise, y: horizon, slope: riseSlope},
    {m: peak, y: topY, slope: 0},
    {m: set, y: horizon, slope: setSlope},
    {m: nextTrough, y: bottomY, slope: 0},
    {m: nextRise, y: horizon, slope: riseSlope},
  ];
  const yAt = minute => {
    for (let index = 0; index < keys.length - 1; index += 1) {
      const start = keys[index];
      const end = keys[index + 1];
      if (minute <= end.m) {
        const span = Math.max(1, end.m - start.m);
        return nodusCurveHermite(
          (minute - start.m) / span,
          start.y,
          start.slope,
          end.y,
          end.slope,
          span,
        );
      }
    }
    return keys[keys.length - 1].y;
  };
  const points = [];
  for (let minute = 0; minute <= 1440; minute += 5) points.push({m: minute, y: yAt(minute)});
  return points;
}

function nodusSmoothElevationPoints(points, yForElevation) {
  const sorted = points
    .filter(point => Number.isFinite(point.m) && Number.isFinite(point.e))
    .sort((left, right) => left.m - right.m);
  if (sorted.length < 2) return [];
  const values = sorted.map(point => yForElevation(point.e));
  const slopes = sorted.map((_point, index) => {
    const previous = Math.max(0, index - 1);
    const next = Math.min(sorted.length - 1, index + 1);
    const before = values[index] - values[previous];
    const after = values[next] - values[index];
    if (index > 0 && index < sorted.length - 1 && before * after < 0) return 0;
    return ((values[next] - values[previous]) / Math.max(1, sorted[next].m - sorted[previous].m)) * 0.65;
  });
  const smoothed = [];
  for (let index = 0; index < sorted.length - 1; index += 1) {
    const start = sorted[index];
    const end = sorted[index + 1];
    const span = Math.max(1, end.m - start.m);
    const firstMinute = index ? Math.ceil(start.m / 2) * 2 : start.m;
    for (let minute = firstMinute; minute < end.m; minute += 2) {
      smoothed.push({
        m: minute,
        y: nodusCurveHermite(
          (minute - start.m) / span,
          values[index],
          slopes[index],
          values[index + 1],
          slopes[index + 1],
          span,
        ),
      });
    }
  }
  const last = sorted[sorted.length - 1];
  smoothed.push({m: last.m, y: values[values.length - 1]});
  return smoothed;
}

function nodusDisplayY(points, minute) {
  if (!points.length) return NaN;
  if (minute <= points[0].m) return points[0].y;
  for (let index = 1; index < points.length; index += 1) {
    if (minute <= points[index].m) {
      const start = points[index - 1];
      const end = points[index];
      const fraction = (minute - start.m) / Math.max(1, end.m - start.m);
      return start.y + ((end.y - start.y) * fraction);
    }
  }
  return points[points.length - 1].y;
}

function nodusDrawPositionPath(context, points, xForMinute, color) {
  if (points.length < 2) return;
  const coordinates = points.map(point => ({x: xForMinute(point.m), y: point.y}));
  const tangents = coordinates.map((point, index) => {
    const previous = coordinates[Math.max(0, index - 1)];
    const next = coordinates[Math.min(coordinates.length - 1, index + 1)];
    let tangentY = (next.y - previous.y) * 0.38;
    if (index > 0 && index < coordinates.length - 1) {
      const before = point.y - previous.y;
      const after = next.y - point.y;
      if (before * after < 0) tangentY = 0;
    }
    return {x: (next.x - previous.x) * 0.38, y: tangentY};
  });
  context.save();
  context.strokeStyle = color;
  context.lineWidth = 6;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(coordinates[0].x, coordinates[0].y);
  for (let index = 0; index < coordinates.length - 1; index += 1) {
    const start = coordinates[index];
    const end = coordinates[index + 1];
    const startTangent = tangents[index];
    const endTangent = tangents[index + 1];
    context.bezierCurveTo(
      start.x + (startTangent.x / 3),
      start.y + (startTangent.y / 3),
      end.x - (endTangent.x / 3),
      end.y - (endTangent.y / 3),
      end.x,
      end.y,
    );
  }
  context.stroke();
  context.restore();
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
  const top = pad + 2;
  const bottom = height - pad - 2;
  const x = minute => pad + (innerWidth * Math.max(0, Math.min(1440, minute)) / 1440);
  const y = nodusSmoothSkyYMapper(horizon, top, bottom, maxElevation, minElevation);
  const bodyPoints = index => data.points.map(point => ({m: Number(point[0]), e: Number(point[index])}));
  const peakPoint = (points, rise, set) => points.reduce((peak, point) => {
    if (point.m < rise || point.m > set || !Number.isFinite(point.e)) return peak;
    return !peak || point.e > peak.e ? point : peak;
  }, null);
  const sunrise = nodusMinute(data.sunrise);
  const sunset = nodusMinute(data.sunset);
  const sunNoon = nodusMinute(data.sun_noon);
  const moonrise = nodusMinute(data.moonrise);
  const moonset = nodusMinute(data.moonset);
  const sunSamples = bodyPoints(1);
  const moonSamples = bodyPoints(2);
  let sunDisplay = nodusOrbitDisplayPoints(
    sunrise,
    sunset,
    Number.isFinite(sunNoon) ? sunNoon : ((sunrise + sunset) / 2),
    top,
    horizon,
    bottom,
  );
  if (sunDisplay.length < 2) sunDisplay = nodusSmoothElevationPoints(sunSamples, y);
  const moonPeak = peakPoint(moonSamples, moonrise, moonset);
  let moonDisplay = nodusOrbitDisplayPoints(
    moonrise,
    moonset,
    moonPeak ? moonPeak.m : ((moonrise + moonset) / 2),
    moonPeak ? Math.max(top, Math.min(horizon - 6, y(moonPeak.e))) : top,
    horizon,
    bottom,
  );
  if (moonDisplay.length < 2) moonDisplay = nodusSmoothElevationPoints(moonSamples, y);

  context.clearRect(0, 0, width, height);
  context.fillStyle = "#d8ecfa";
  context.fillRect(0, 0, width, height);
  context.fillStyle = "#000";
  context.fillRect(pad, horizon, innerWidth, height - pad - horizon);
  context.strokeStyle = "#b8c4c9";
  context.lineWidth = 2;
  context.strokeRect(pad, pad, innerWidth, innerHeight);
  nodusDrawPositionPath(context, sunDisplay, x, "#f5d438");
  nodusDrawPositionPath(context, moonDisplay, x, "#63b9ec");

  const currentMinute = nodusMinute(data.current_minute);
  [
    [nodusDisplayY(sunDisplay, currentMinute), "#ffe426", "#ffad16"],
    [nodusDisplayY(moonDisplay, currentMinute), "#d9eef9", "#4685a9"],
  ].forEach(marker => {
    if (!Number.isFinite(marker[0])) return;
    context.fillStyle = marker[1];
    context.strokeStyle = marker[2];
    context.lineWidth = 5;
    context.beginPath();
    context.arc(x(currentMinute), marker[0], 15, 0, Math.PI * 2);
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
    const meta = document.getElementById("sunMoon29Meta");
    if (meta) meta.textContent = "Loading 29-day position data";
    nodusLoadAstronomyDetail()
      .then(data => {
        if (expanded.getAttribute("aria-hidden") === "false") nodusDraw29Days(data);
      })
      .catch(() => {
        if (meta) meta.textContent = "29-day position data unavailable";
      });
    expanded.focus({preventScroll: true});
  } else {
    daily.focus({preventScroll: true});
  }
}

async function nodusCheckReportUpdate() {
  if (document.hidden) return;
  try {
    const response = await fetch("index.html", {method: "HEAD", cache: "no-cache"});
    if (!response.ok) return;
    const modified = Date.parse(response.headers.get("Last-Modified") || "") || 0;
    const validator = response.headers.get("ETag") || response.headers.get("Last-Modified") || "";
    if (
      (nodusReportModified && modified > nodusReportModified + 500) ||
      (nodusReportValidator && validator && validator !== nodusReportValidator)
    ) {
      location.reload();
      return;
    }
    if (modified) nodusReportModified = modified;
    if (validator) nodusReportValidator = validator;
  } catch (_error) {
    // Keep the currently generated report visible through transient LAN faults.
  }
}

function nodusStartPresentationRefresh() {
  if (document.hidden) return;
  nodusRefreshSwitches();
  nodusRefreshManagerState();
  nodusCheckReportUpdate();
  clearInterval(nodusSwitchTimer);
  clearInterval(nodusManagerTimer);
  clearInterval(nodusReportTimer);
  nodusSwitchTimer = window.setInterval(nodusRefreshSwitches, 5000);
  nodusManagerTimer = window.setInterval(nodusRefreshManagerState, 15000);
  nodusReportTimer = window.setInterval(nodusCheckReportUpdate, 60000);
}

function nodusStopPresentationRefresh() {
  clearInterval(nodusSwitchTimer);
  clearInterval(nodusManagerTimer);
  clearInterval(nodusReportTimer);
  nodusSwitchTimer = null;
  nodusManagerTimer = null;
  nodusReportTimer = null;
}

function nodusHandleVisibility() {
  if (document.hidden) nodusStopPresentationRefresh();
  else nodusStartPresentationRefresh();
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

function nodusGraphumSelections() {
  return Array.from(document.querySelectorAll(".graphum-observation:checked")).map(input => ({
    kind: input.dataset.kind,
    value: input.value,
    label: input.dataset.label || input.value
  }));
}

function nodusGraphumRangeLabel(seconds) {
  const labels = {3600:"Last hour",10800:"Last 3 hours",21600:"Last 6 hours",43200:"Last 12 hours",86400:"Last 24 hours",259200:"Last 3 days",604800:"Last 7 days",1209600:"Last 14 days",2592000:"Last 30 days",5184000:"Last 60 days",7776000:"Last 90 days"};
  return labels[seconds] || "History";
}

function nodusGraphumUpdateSelection(changed) {
  const selected = nodusGraphumSelections();
  if (changed?.checked && selected.length > 4) {
    changed.checked = false;
    document.getElementById("graphumStatus").textContent = "Select up to four observations.";
  }
  const current = nodusGraphumSelections();
  document.getElementById("graphumSelectionCount").textContent = `${current.length} of 4`;
  document.getElementById("graphumRangeTitle").textContent = nodusGraphumRangeLabel(nodusGraphumRange);
  const chips = document.getElementById("graphumChips");
  chips.replaceChildren(...current.map(item => {
    const chip = document.createElement("span");
    chip.className = "graphum-chip";
    chip.textContent = item.label;
    return chip;
  }));
  window.clearTimeout(nodusGraphumRefresh);
  nodusGraphumRefresh = window.setTimeout(nodusLoadGraphum, 140);
}

function nodusGraphumOption(item, kind) {
  const label = document.createElement("label");
  label.className = "graphum-option-item";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = "graphum-observation";
  input.dataset.kind = kind;
  input.dataset.label = item.label || item.name || item.channel_id;
  input.value = item.name || item.channel_id;
  input.addEventListener("change", () => nodusGraphumUpdateSelection(input));
  const text = document.createElement("span");
  text.textContent = `${input.dataset.label}${item.unit ? ` (${item.unit})` : ""}`;
  label.append(input, text);
  return label;
}

function nodusGraphumGroup(title, items, kind) {
  const group = document.createElement("details");
  group.className = "graphum-option-group";
  group.open = true;
  const summary = document.createElement("summary");
  summary.textContent = title;
  const rows = document.createElement("div");
  rows.className = "graphum-option-items";
  items.forEach(item => rows.append(nodusGraphumOption(item, kind)));
  group.append(summary, rows);
  return group;
}

async function nodusPrepareGraphum() {
  if (nodusGraphumReady) return;
  const response = await fetch(`${nodusApiOrigin}/api/status`, {cache:"no-store"});
  if (!response.ok) throw new Error("Graph selections are unavailable");
  const status = await response.json();
  const metricHost = document.getElementById("graphumMetricOptions");
  const metrics = status.metric_options || [];
  metricHost.replaceChildren();
  if (metrics.length) metricHost.append(nodusGraphumGroup(status.device_id || "Nodus sensor", metrics, "metric"));
  else metricHost.innerHTML = "<p>No archived sensor metrics are available.</p>";
  const switchHost = document.getElementById("graphumSwitchOptions");
  const switches = status.switch?.channels || [];
  switchHost.replaceChildren();
  if (switches.length) switchHost.append(nodusGraphumGroup(status.switch?.switch_device_id || "Nodus switches", switches, "switch"));
  else switchHost.innerHTML = "<p>No switch channels are available.</p>";
  nodusGraphumReady = true;
}

function nodusOpenGraphum() {
  const graphum = document.getElementById("nodusGraphum");
  graphum.classList.add("active");
  graphum.setAttribute("aria-hidden", "false");
  nodusStopPresentationRefresh();
  nodusPrepareGraphum().then(() => {
    nodusResizeGraphum();
    nodusGraphumUpdateSelection();
  }).catch(error => { document.getElementById("graphumStatus").textContent = error.message; });
  window.clearInterval(nodusGraphumTimer);
  nodusGraphumTimer = window.setInterval(nodusLoadGraphum, 15000);
}

function nodusCloseGraphum() {
  const graphum = document.getElementById("nodusGraphum");
  graphum.classList.remove("active");
  graphum.setAttribute("aria-hidden", "true");
  window.clearInterval(nodusGraphumTimer);
  nodusGraphumTimer = null;
  window.clearTimeout(nodusGraphumRefresh);
  nodusGraphumAbort?.abort();
  nodusGraphumAbort = null;
  nodusStartPresentationRefresh();
}

async function nodusLoadGraphum() {
  if (!document.getElementById("nodusGraphum")?.classList.contains("active")) return;
  const selected = nodusGraphumSelections();
  if (!selected.length) {
    nodusGraphumPayload = null;
    nodusResizeGraphum();
    document.getElementById("graphumStatus").textContent = "Select a sensor metric or switch state to begin.";
    return;
  }
  nodusGraphumAbort?.abort();
  nodusGraphumAbort = new AbortController();
  const spinner = document.getElementById("graphumSpinner");
  spinner.classList.add("active");
  try {
    const response = await fetch(`${nodusApiOrigin}/api/history`, {
      method:"POST", headers:{"Content-Type":"text/plain;charset=UTF-8"}, signal:nodusGraphumAbort.signal,
      body:JSON.stringify({range_seconds:nodusGraphumRange,metrics:selected.filter(item=>item.kind==="metric").map(item=>item.value),switch_channels:selected.filter(item=>item.kind==="switch").map(item=>item.value)})
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "History request failed");
    nodusGraphumPayload = payload;
    nodusResizeGraphum();
    const points = Object.values(payload.series || {}).reduce((sum, rows) => sum + rows.length, 0);
    document.getElementById("graphumStatus").textContent = points || (payload.switches || []).some(item => item.events?.length) ? "Graph updated" : "No history is available in this range.";
  } catch (error) {
    if (error.name !== "AbortError") {
      nodusGraphumPayload = {error:error.message || "History request failed"};
      nodusResizeGraphum();
      document.getElementById("graphumStatus").textContent = nodusGraphumPayload.error;
    }
  } finally {
    spinner.classList.remove("active");
  }
}

function nodusResizeGraphum() {
  const canvas = document.getElementById("graphumCanvas");
  const frame = canvas.parentElement;
  canvas.width = Math.max(320, frame.clientWidth - 12);
  canvas.height = Math.max(260, frame.clientHeight - 12);
  nodusDrawGraphum(canvas, nodusGraphumPayload);
}

function nodusDrawGraphum(canvas, payload) {
  const context = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#fff"; context.fillRect(0, 0, width, height);
  if (!payload) {
    context.fillStyle = "#61796f"; context.font = "16px system-ui,sans-serif"; context.textAlign = "center";
    context.fillText("Select a sensor metric or switch state to begin.", width / 2, height / 2); return;
  }
  if (payload.error) {
    context.fillStyle = "#b22635"; context.font = "16px system-ui,sans-serif"; context.textAlign = "center";
    context.fillText(payload.error, width / 2, height / 2); return;
  }
  const left=64,right=28,top=48,bottom=52,x0=left,x1=width-right,y0=top,y1=height-bottom;
  const timestamps=[];
  Object.values(payload.series||{}).forEach(rows=>(rows||[]).forEach(row=>{const stamp=Number(row.t);if(Number.isFinite(stamp))timestamps.push(stamp);}));
  (payload.switches||[]).forEach(channel=>(channel.events||[]).forEach(event=>{const stamp=Number(event.t);if(Number.isFinite(stamp))timestamps.push(stamp);}));
  const requestedStart=Number(payload.start_ts)||0;
  const start=timestamps.length?Math.min(...timestamps):requestedStart;
  const duration=Math.max(1,Number(payload.range_seconds)||((Number(payload.end_ts)||start+1)-requestedStart));
  const end=start+duration;
  context.strokeStyle="#dbe3df"; context.lineWidth=1;
  for(let index=0;index<=4;index+=1){const y=y0+((y1-y0)*index/4);context.beginPath();context.moveTo(x0,y);context.lineTo(x1,y);context.stroke();}
  const tickCount=width<700?4:6; context.fillStyle="#61796f"; context.font="12px system-ui,sans-serif"; context.textAlign="center";
  for(let index=0;index<=tickCount;index+=1){const timestamp=start+((end-start)*index/tickCount),x=x0+((x1-x0)*index/tickCount),date=new Date(timestamp*1000);context.strokeStyle="#e7ede9";context.beginPath();context.moveTo(x,y0);context.lineTo(x,y1);context.stroke();context.fillText((end-start)<=86400?date.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}):date.toLocaleDateString([], {month:"short",day:"numeric"}),x,y1+22);}
  const colors=["#1769aa","#8a168f","#d97706","#16804b"];
  (payload.selected_metrics||[]).forEach((name,index)=>{const rows=(payload.series?.[name]||[]).filter(row=>Number.isFinite(Number(row.t))&&Number.isFinite(Number(row.v)));if(!rows.length)return;let min=Math.min(...rows.map(row=>Number(row.v))),max=Math.max(...rows.map(row=>Number(row.v)));if(max===min){max+=1;min-=1;}context.strokeStyle=colors[index%colors.length];context.lineWidth=2;context.beginPath();rows.forEach((row,rowIndex)=>{const x=x0+((Number(row.t)-start)/(end-start))*(x1-x0),y=y1-((Number(row.v)-min)/(max-min))*(y1-y0);if(rowIndex)context.lineTo(x,y);else context.moveTo(x,y);});context.stroke();context.fillStyle=colors[index%colors.length];rows.forEach(row=>{const x=x0+((Number(row.t)-start)/(end-start))*(x1-x0),y=y1-((Number(row.v)-min)/(max-min))*(y1-y0);context.beginPath();context.arc(x,y,2.5,0,Math.PI*2);context.fill();});context.textAlign="left";context.fillText(`${name} ${Number(min.toFixed(2))}–${Number(max.toFixed(2))}`,x0+index*150,22);});
  (payload.switches||[]).forEach((channel,channelIndex)=>(channel.events||[]).forEach(event=>{const x=x0+((event.t-start)/(end-start))*(x1-x0);context.strokeStyle=event.state?"#16804b":"#b22635";context.lineWidth=1;context.beginPath();context.moveTo(x,y0);context.lineTo(x,y1);context.stroke();if(channelIndex===0){context.fillStyle=context.strokeStyle;context.fillRect(x-2,event.state?y0:y1-6,4,6);}}));
  context.strokeStyle="#87978f";context.strokeRect(x0,y0,x1-x0,y1-y0);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nodusGraphumOpen")?.addEventListener("click", nodusOpenGraphum);
  document.getElementById("nodusGraphumClose")?.addEventListener("click", nodusCloseGraphum);
  document.querySelectorAll("#graphumRanges button").forEach(button => {
    button.addEventListener("click", () => {
      nodusGraphumRange = Number(button.dataset.range) || 86400;
      document.querySelectorAll("#graphumRanges button").forEach(candidate => candidate.classList.toggle("active", candidate === button));
      nodusGraphumUpdateSelection();
    });
  });
  document.querySelectorAll(".switch-current[data-channel-id]").forEach(element => {
    element.addEventListener("click", nodusToggle);
  });
  nodusStartPresentationRefresh();
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
    if (event.key === "Escape" && document.getElementById("nodusGraphum")?.classList.contains("active")) {
      nodusCloseGraphum();
      return;
    }
    if (event.key === "Escape" && document.querySelector(".astro-grid.astronomy-expanded")) {
      nodusSet29DayOpen(false);
    }
  });
  document.addEventListener("visibilitychange", nodusHandleVisibility);
  window.addEventListener("pageshow", nodusHandleVisibility);
  window.addEventListener("resize", () => {
    if (document.getElementById("nodusGraphum")?.classList.contains("active")) nodusResizeGraphum();
  });
});
