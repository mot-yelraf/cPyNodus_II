"""Serve the limited LAN setup UI for a Nodus WeeWX instance.

The extension exposes authenticated system settings and fixed manager actions
while keeping privileged installation work behind the root-owned helper.
"""

import base64
import hmac
import json
import logging
import mimetypes
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_MAX_BODY = 65536
_MAX_RULES = 32
_MAX_CONDITIONS = 24
_MAX_ACTIONS = 8
_RULE_NAME = re.compile(r"^[A-Za-z0-9_. -]{1,48}$")
_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_SETUP_FILES = {
    "/setup/": ("index.html", "text/html; charset=utf-8"),
    "/setup/index.html": ("index.html", "text/html; charset=utf-8"),
    "/setup/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/setup/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
}
_CALIBRATIONS = {
    "aht": (
        ("Calibration.Device.TEMP_OFFSET", "Temperature offset", "°C"),
        ("Calibration.Device.RH_OFFSET", "Humidity offset", "%"),
    ),
    "avpd": (
        ("Calibration.Device.TEMP_OFFSET", "Temperature offset", "°C"),
        ("Calibration.Device.RH_OFFSET", "Humidity offset", "%"),
        ("Calibration.Device.ALTITUDE_METERS", "Altitude", "m"),
    ),
    "co2": (
        ("Calibration.Device.TEMP_OFFSET", "Temperature offset", "°C"),
        ("Calibration.Device.RH_OFFSET", "Humidity offset", "%"),
        ("Calibration.Device.CO2_OFFSET", "CO₂ offset", "ppm"),
        ("Calibration.Device.ALTITUDE_METERS", "Altitude", "m"),
    ),
    "aqi": (
        ("Calibration.Device.TEMP_OFFSET", "Temperature offset", "°C"),
        ("Calibration.Device.RH_OFFSET", "Humidity offset", "%"),
        ("Calibration.Device.AQI_OFFSET", "AQI offset", ""),
        ("Calibration.Device.GAS_OFFSET", "Gas offset", "Ω"),
        ("Calibration.Device.ALTITUDE_METERS", "Altitude", "m"),
    ),
    "soil": (
        ("Calibration.Device.SOIL_TEMP_CAL_VAL", "Soil temperature", "°C"),
        ("Calibration.Device.SOIL_MOIST_CAL_VAL", "Soil moisture", "%"),
        ("Calibration.Device.SOIL_PH_CAL_VAL", "Soil pH", "pH"),
        ("Calibration.Device.SOIL_EC_CAL_VAL", "Soil EC", "mS/cm"),
    ),
}


def _http_route(raw_path):
    """Resolve a request to its public or authenticated content family."""
    path = urlparse(raw_path).path
    if path == "/api/status":
        return "api", None, None, True
    if path == "/setup":
        return "redirect", "/setup/", None, False
    if path in _SETUP_FILES:
        target, content_type = _SETUP_FILES[path]
        return "setup", target, content_type, True
    if path.startswith("/setup/") or path.startswith("/api/"):
        return "missing", None, None, True
    return "dashboard", path.lstrip("/") or "index.html", None, False


def _static_path(root, relative):
    """Return a traversal-safe path below a static content root."""
    root = Path(root).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


_METRICS = {
    "inTemp": ("Inside temperature", "°C"),
    "inHumidity": ("Inside humidity", "%"),
    "dewpoint": ("Dew point", "°C"),
    "pressure": ("Station pressure", "hPa"),
    "vpd": ("VPD", "kPa"),
    "absoluteHumidity": ("Absolute humidity", "g/m³"),
    "dewpointDepression": ("Dewpoint depression", "°C"),
    "dewVpdRisk": ("DewVPD risk", "%"),
    "gasResistance": ("Gas resistance", "Ω"),
    "airQuality": ("Air quality index", "index"),
    "co2": ("CO2", "ppm"),
    "soilTemperature": ("Soil temperature", "°C"),
    "soilMoisturePct": ("Soil moisture", "%"),
    "soilMoistureDeficit": ("Soil moisture deficit", "%"),
    "soilStressIndex": ("Soil stress index", "%"),
    "soilPh": ("Soil pH", "pH"),
    "soilEc": ("Soil EC", "mS/cm"),
    "soilNitrogen": ("Soil nitrogen", "mg/kg"),
    "soilPhosphorus": ("Soil phosphorus", "mg/kg"),
    "soilPotassium": ("Soil potassium", "mg/kg"),
    "soilFertilityIndex": ("Soil fertility index", "%"),
}


def _family(device_id):
    """Return the limited calibration family for a Nodus device ID."""
    value = str(device_id or "").strip().lower().partition("-")[0]
    if value in ("aht", "apvpd_aht"):
        return "aht"
    if value in ("avpd", "apvpd"):
        return "avpd"
    if value in ("co2", "aqi", "soil"):
        return value
    return "avpd"


def calibration_fields(device_id):
    """Return JSON-safe change-only calibration field definitions."""
    return [
        {"key": key, "label": label, "unit": unit}
        for key, label, unit in _CALIBRATIONS[_family(device_id)]
    ]


def metric_fields(names):
    """Return labels and canonical metric units for automation selectors."""
    fields = []
    for name in names:
        metric = str(name or "").strip()
        if not metric:
            continue
        label, unit = _METRICS.get(metric, (metric, ""))
        fields.append({"name": metric, "label": label, "unit": unit})
    return fields


def _clock(value):
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("time must use HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time is outside 00:00-23:59")
    return "{:02d}:{:02d}".format(hour, minute)


def _valid_days(value, required=True):
    days = [str(day).strip().lower()[:3] for day in value or ()]
    if (required and not days) or any(day not in _DAYS for day in days):
        raise ValueError("at least one valid weekday is required")
    return days


def _legacy_item(item):
    """Convert the original single-condition UI shape to an advanced rule."""
    if "conditions" in item or "actions" in item:
        return item
    days = _valid_days(item.get("days") or list(_DAYS))
    return {
        "name": item.get("name"),
        "enabled": item.get("enabled", True),
        "conditions": [
            {
                "type": "metric",
                "metric": item.get("metric"),
                "direction": item.get("direction"),
                "on_threshold": item.get("on_threshold"),
                "off_threshold": item.get("off_threshold"),
            },
            {
                "type": "time",
                "start": item.get("start_time") or "00:00",
                "end": item.get("end_time") or "00:00",
                "days": days,
            },
        ],
        "actions": [
            {
                "channel_id": item.get("channel_id"),
                "state": "on",
                "false_action": "off"
                if item.get("outside_window", "off") == "off"
                else "hold",
                "delay_seconds": 0,
            }
        ],
        "stale_action": item.get("stale_action") or "off",
        "minimum_on_seconds": item.get("minimum_on_seconds") or 0,
        "minimum_off_seconds": item.get("minimum_off_seconds") or 0,
        "retry_seconds": item.get("retry_seconds") or 60,
    }


def _normalize_condition(condition, channel_ids, metric_ids):
    """Validate and canonicalize one host-side automation condition."""
    if not isinstance(condition, dict):
        raise ValueError("automation condition must be an object")
    kind = str(condition.get("type") or "metric").strip().lower()
    if kind == "or":
        return {"type": "or"}
    if kind == "metric":
        metric = str(condition.get("metric") or "").strip()
        if metric not in metric_ids:
            raise ValueError("automation metric is invalid")
        direction = str(condition.get("direction") or "above").lower()
        if direction not in ("above", "below"):
            raise ValueError("direction must be above or below")
        on_value = float(condition.get("on_threshold"))
        off_value = float(condition.get("off_threshold"))
        if direction == "above" and off_value >= on_value:
            raise ValueError("OFF threshold must be below ON threshold")
        if direction == "below" and off_value <= on_value:
            raise ValueError("OFF threshold must be above ON threshold")
        return {
            "type": "metric",
            "metric": metric,
            "direction": direction,
            "on_threshold": on_value,
            "off_threshold": off_value,
        }
    if kind == "time":
        return {
            "type": "time",
            "start": _clock(condition.get("start") or "00:00"),
            "end": _clock(condition.get("end") or "00:00"),
            "days": _valid_days(condition.get("days") or list(_DAYS)),
        }
    if kind == "timer":
        duration = int(condition.get("duration_minutes") or 0)
        period = int(condition.get("period_minutes") or 0)
        if duration < 1 or period < 2 or duration >= period:
            raise ValueError("timer duration must be positive and less than period")
        return {
            "type": "timer",
            "duration_minutes": duration,
            "period_minutes": period,
            "anchor_epoch": max(0, int(condition.get("anchor_epoch") or 0)),
        }
    if kind == "astral":
        event = str(condition.get("event") or "sunrise").strip().lower()
        allowed = {
            "sunrise",
            "sunset",
            "sunrise_to_sunset",
            "sunset_to_sunrise",
        }
        if event not in allowed:
            raise ValueError("astral event is invalid")
        offset = int(condition.get("offset_minutes") or 0)
        if not -720 <= offset <= 720:
            raise ValueError("astral offset must be between -720 and 720 minutes")
        return {
            "type": "astral",
            "event": event,
            "offset_minutes": offset,
            "days": _valid_days(condition.get("days") or list(_DAYS)),
        }
    if kind == "switch":
        channel_id = str(condition.get("channel_id") or "").strip()
        state = str(condition.get("state") or "on").strip().lower()
        if channel_id not in channel_ids or state not in ("on", "off"):
            raise ValueError("switch-state condition is invalid")
        return {"type": "switch", "channel_id": channel_id, "state": state}
    raise ValueError("unsupported automation condition type: {}".format(kind))


def _normalize_action(action, channel_ids):
    if not isinstance(action, dict):
        raise ValueError("automation action must be an object")
    channel_id = str(action.get("channel_id") or "").strip()
    state = str(action.get("state") or "on").strip().lower()
    false_action = str(action.get("false_action") or "opposite").strip().lower()
    if channel_id not in channel_ids or state not in ("on", "off"):
        raise ValueError("automation action channel or state is invalid")
    if false_action not in ("opposite", "on", "off", "previous_state", "hold"):
        raise ValueError("automation false action is invalid")
    return {
        "channel_id": channel_id,
        "state": state,
        "false_action": false_action,
        "delay_seconds": max(0, min(3600, int(action.get("delay_seconds") or 0))),
    }


def _reject_switch_cycles(rules):
    graph = {}
    for rule in rules.values():
        if not rule.get("enabled", True):
            continue
        dependencies = {
            item["channel_id"]
            for item in rule["conditions"]
            if item["type"] == "switch"
        }
        for action in rule["actions"]:
            target = action["channel_id"]
            if target in dependencies:
                raise ValueError("automation cannot depend on its target switch")
            graph.setdefault(target, set()).update(dependencies)

    visiting = set()
    visited = set()

    def visit(channel_id):
        if channel_id in visiting:
            raise ValueError("switch-state automation dependency cycle detected")
        if channel_id in visited:
            return
        visiting.add(channel_id)
        for dependency in graph.get(channel_id, ()):
            if dependency in graph:
                visit(dependency)
        visiting.remove(channel_id)
        visited.add(channel_id)

    for channel_id in graph:
        visit(channel_id)


def validate_rules(items, channels, metrics):
    """Validate and normalize version-two advanced automation rules."""
    if not isinstance(items, list):
        raise ValueError("automations must be a list")
    if len(items) > _MAX_RULES:
        raise ValueError("too many automation rules")
    channel_ids = set(channels)
    metric_ids = set(metrics)
    used_names = set()
    used_channels = set()
    rules = {}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("automation must be an object")
        item = _legacy_item(raw_item)
        name = str(item.get("name") or "").strip()
        if not _RULE_NAME.match(name) or name in used_names:
            raise ValueError("automation name is invalid or duplicated")
        raw_conditions = item.get("conditions") or []
        raw_actions = item.get("actions") or []
        if not raw_conditions or len(raw_conditions) > _MAX_CONDITIONS:
            raise ValueError("automation requires 1-24 conditions")
        if not raw_actions or len(raw_actions) > _MAX_ACTIONS:
            raise ValueError("automation requires 1-8 actions")
        conditions = [
            _normalize_condition(entry, channel_ids, metric_ids)
            for entry in raw_conditions
        ]
        if conditions[0]["type"] == "or" or conditions[-1]["type"] == "or":
            raise ValueError("OR must separate two condition groups")
        if any(
            left["type"] == right["type"] == "or"
            for left, right in zip(conditions, conditions[1:])
        ):
            raise ValueError("OR separators cannot be consecutive")
        actions = [_normalize_action(entry, channel_ids) for entry in raw_actions]
        action_channels = [entry["channel_id"] for entry in actions]
        if len(action_channels) != len(set(action_channels)):
            raise ValueError("automation action channels are duplicated")
        enabled = bool(item.get("enabled", True))
        if enabled and any(channel in used_channels for channel in action_channels):
            raise ValueError("an enabled automation already owns this switch")
        stale_action = str(item.get("stale_action") or "off").strip().lower()
        if stale_action not in ("off", "hold"):
            raise ValueError("stale action must be off or hold")
        rules[name] = {
            "enabled": enabled,
            "conditions": conditions,
            "actions": actions,
            "stale_action": stale_action,
            "minimum_on_seconds": max(0, int(item.get("minimum_on_seconds") or 0)),
            "minimum_off_seconds": max(
                0, int(item.get("minimum_off_seconds") or 0)
            ),
            "retry_seconds": max(5, int(item.get("retry_seconds") or 60)),
        }
        used_names.add(name)
        if enabled:
            used_channels.update(action_channels)
    _reject_switch_cycles(rules)
    return {"schema": "nodus-weewx-automation/v2", "rules": rules}


def _legacy_rule(name, rule):
    condition = [part.strip() for part in str(rule.get("condition_1") or "").split(",")]
    if len(condition) != 4:
        return None
    return _legacy_item(
        {
            "name": name,
            "enabled": rule.get("enabled", True),
            "channel_id": rule.get("channel_id"),
            "metric": condition[0],
            "direction": condition[1],
            "on_threshold": float(condition[2]),
            "off_threshold": float(condition[3]),
            "start_time": rule.get("start_time") or "00:00",
            "end_time": rule.get("end_time") or "00:00",
            "days": [
                part.strip()
                for part in str(rule.get("days") or "").split(",")
                if part.strip()
            ],
            "outside_window": rule.get("outside_window") or "off",
            "stale_action": rule.get("stale_action") or "off",
            "minimum_on_seconds": rule.get("minimum_on_seconds") or 0,
            "minimum_off_seconds": rule.get("minimum_off_seconds") or 0,
            "retry_seconds": rule.get("retry_seconds") or 60,
        }
    )


def rules_for_ui(document):
    """Return advanced UI rules, converting the original schema when needed."""
    rows = []
    for name, stored in (document.get("rules") or {}).items():
        if not isinstance(stored, dict):
            continue
        if "conditions" not in stored or "actions" not in stored:
            item = _legacy_rule(name, stored)
            if item:
                rows.append(item)
            continue
        item = dict(stored)
        item["name"] = name
        rows.append(item)
    return rows


def read_rules(path):
    """Read a UI-managed automation document or return an empty one."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"rules": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict):
        raise ValueError("automation rules file is invalid")
    return data


def write_rules(path, document):
    """Atomically write UI-managed automation rules."""
    temporary = "{}.tmp.{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


class NodusAdminHTTP:
    """Run a small authenticated HTTP server around a WeeWX backend."""

    def __init__(self, backend, section):
        self.backend = backend
        self.host = str(section.get("admin_host") or "0.0.0.0")
        port = section.get("admin_port")
        self.port = int(8767 if port in (None, "") else port)
        self.username = str(section.get("admin_username") or "admin")
        self.password = str(section.get("admin_password") or "")
        self.require_auth = str(section.get("admin_require_auth") or "false").lower() \
            in ("1", "true", "yes", "on")
        self.web_root = Path(
            str(section.get("admin_web_root") or "/etc/weewx/skins/Nodus/admin")
        )
        self.dashboard_root = Path(
            str(
                section.get("dashboard_web_root")
                or "/var/www/html/weewx/nodus"
            )
        )
        if self.require_auth and not self.password:
            raise ValueError("Nodus admin password is required")
        self.server = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="nodus-admin", daemon=True
        )
        self.thread.start()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _authorized(self):
                value = self.headers.get("Authorization", "")
                if not value.startswith("Basic "):
                    return False
                try:
                    decoded = base64.b64decode(value[6:]).decode("utf-8")
                    username, password = decoded.split(":", 1)
                except Exception:
                    return False
                return hmac.compare_digest(
                    username, outer.username
                ) and hmac.compare_digest(password, outer.password)

            def _require_auth(self):
                if not outer.require_auth:
                    return True
                if self._authorized():
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Nodus WeeWX Setup"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def _json(self, status, data):
                payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length < 1 or length > _MAX_BODY:
                    raise ValueError("request body size is invalid")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("request body must be an object")
                return data

            def _file(self, root, relative, content_type=None):
                target = _static_path(root, relative)
                if target is None:
                    self.send_error(404)
                    return
                try:
                    payload = target.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                guessed = mimetypes.guess_type(str(target))[0]
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    content_type or guessed or "application/octet-stream",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                kind, target, content_type, authenticated = _http_route(self.path)
                if authenticated and not self._require_auth():
                    return
                if kind == "api":
                    self._json(200, outer.backend.admin_snapshot())
                    return
                if kind == "redirect":
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if kind == "setup":
                    self._file(outer.web_root, target, content_type)
                    return
                if kind == "missing":
                    self.send_error(404)
                    return
                self._file(outer.dashboard_root, target)

            def do_POST(self):
                if not self._require_auth():
                    return
                try:
                    body = self._body()
                    path = urlparse(self.path).path
                    if path == "/api/sensor":
                        result = outer.backend.admin_update_sensor(body)
                    elif path == "/api/switch":
                        result = outer.backend.admin_update_switch(body)
                    elif path == "/api/switch/toggle":
                        result = outer.backend.admin_toggle_switch(body)
                    elif path == "/api/automations":
                        result = outer.backend.admin_update_automations(body)
                    else:
                        self._json(404, {"ok": False, "error": "not found"})
                        return
                    self._json(200, result)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    log.exception("Nodus admin request failed")
                    self._json(500, {"ok": False, "error": str(exc)})

            def do_OPTIONS(self):
                path = urlparse(self.path).path
                if not path.startswith("/api/"):
                    self.send_error(404)
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Access-Control-Allow-Methods", "GET, POST, OPTIONS"
                )
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                log.info("Nodus admin: " + fmt, *args)

        return Handler

    def shutdown(self):
        """Stop the admin HTTP server."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
