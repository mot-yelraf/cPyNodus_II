"""Discover WeeWX-profile Nodus devices and maintain a passive registry.

``DiscoveryManager`` consumes retained metadata, validates device identities
and topics, and atomically updates the registry. The service, HTTP surface,
settings helpers, and ``main`` entry point support WeeWX and host management.
"""

import argparse
import json
import logging
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_TOPIC = re.compile(r"^[A-Za-z0-9_./+-]{1,240}$")
_SAFE_SUBSCRIPTION_TOPIC = re.compile(r"^[A-Za-z0-9_./+#-]{1,240}$")
_COMMON_FIELDS = (
    ("Temperature", "inTemp"),
    ("Rel-Humidity", "inHumidity"),
    ("Dew Point", "dewpoint"),
    ("Ambient VPD", "vpd"),
    ("Humidity", "absoluteHumidity"),
    ("Dew Point Deficit", "dewpointDepression"),
    ("DewVPD Risk", "dewVpdRisk"),
)
_FAMILY_FIELDS = {
    "aht": _COMMON_FIELDS,
    "apvpd_aht": _COMMON_FIELDS,
    "avpd": _COMMON_FIELDS + (("Baro-Pressure", "pressure"),),
    "apvpd": _COMMON_FIELDS + (("Baro-Pressure", "pressure"),),
    "aqi": _COMMON_FIELDS
    + (
        ("Baro-Pressure", "pressure"),
        ("Gas", "gasResistance"),
        ("Air Quality", "airQuality"),
    ),
    "co2": _COMMON_FIELDS + (("CO2", "co2"),),
    "soil": (
        ("Soil Temp_C", "soilTemperature"),
        ("Soil Moisture", "soilMoisturePct"),
        ("Soil Moisture Deficit", "soilMoistureDeficit"),
        ("Soil Stress Index", "soilStressIndex"),
        ("Soil pH", "soilPh"),
        ("Soil EC", "soilEc"),
        ("Soil Nitrogen", "soilNitrogen"),
        ("Soil Phosphorus", "soilPhosphorus"),
        ("Soil Potassium", "soilPotassium"),
        ("Soil Fertility Index", "soilFertilityIndex"),
    ),
    "lux": (),
}


def _clean_topic(value, name):
    topic = str(value or "").strip().strip("/")
    if not topic or not _SAFE_TOPIC.fullmatch(topic) or "//" in topic:
        raise ValueError("{} is invalid".format(name))
    return topic


def _clean_subscription_topic(value):
    topic = str(value or "").strip().strip("/")
    if not topic or not _SAFE_SUBSCRIPTION_TOPIC.fullmatch(topic) or "//" in topic:
        raise ValueError("subscription topic is invalid")
    levels = topic.split("/")
    if any(("+" in level and level != "+") for level in levels):
        raise ValueError("subscription topic is invalid")
    if any(("#" in level and level != "#") for level in levels):
        raise ValueError("subscription topic is invalid")
    if "#" in levels and levels[-1] != "#":
        raise ValueError("subscription topic is invalid")
    return topic


def _fallback_family(sensor):
    """Infer older retained metadata only when sensor.device is absent."""
    hardware = str(sensor.get("hardware") or "").strip().lower()
    if hardware == "ahtx0":
        return "aht"
    if hardware == "bme280":
        return "avpd"
    if hardware == "bme680":
        return "aqi"
    if hardware.startswith("scd"):
        return "co2"
    if hardware == "veml7700":
        return "lux"
    if hardware.startswith("soil") or hardware in {
        "canonical",
        "soil_2in1",
        "soil_4in1",
        "soil_7in1",
    }:
        return "soil"
    return ""


def parse_meta(payload, topic, base_topic):
    """Validate one retained metadata message and return a device descriptor."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("schema") != "nodus-meta/v1":
        raise ValueError("unexpected Nodus metadata schema")
    if (data.get("profile") or {}).get("active_profile") != "weewx":
        raise ValueError("Nodus is not using the weewx profile")

    device_id = str(data.get("device_id") or "").strip()
    if not _SAFE_ID.fullmatch(device_id):
        raise ValueError("device_id is invalid")
    base_topic = _clean_topic(base_topic, "base_topic")
    expected_meta = "{}/{}/meta".format(base_topic, device_id)
    if str(topic or "").strip("/") != expected_meta:
        raise ValueError("metadata topic does not match device_id")

    sensor = data.get("sensor") or {}
    switch = data.get("switch") or {}
    network = data.get("network") or {}
    status = data.get("status") or {}

    def optional_topic(value, name):
        return _clean_topic(value, name) if str(value or "").strip() else ""

    family = str(sensor.get("device") or "").strip().lower()
    if not family:
        family = _fallback_family(sensor)
    if sensor and family not in _FAMILY_FIELDS:
        raise ValueError("unsupported sensor device: {}".format(family or "unknown"))

    sensor_id = str(sensor.get("sensor_id") or "").strip()
    if sensor_id and not _SAFE_ID.fullmatch(sensor_id):
        raise ValueError("sensor_id is invalid")
    data_topic = sensor.get("data_topic")
    if data_topic:
        data_topic = _clean_topic(data_topic, "sensor.data_topic")
        if not data_topic.endswith("/data"):
            raise ValueError("sensor.data_topic must end with /data")
    else:
        data_topic = "{}/{}/data".format(base_topic, sensor_id or device_id)

    switch_meta_topic = str(switch.get("meta_topic") or "").strip()
    if switch_meta_topic:
        switch_meta_topic = _clean_topic(switch_meta_topic, "switch.meta_topic")
    elif int(switch.get("channel_count") or 0) > 0:
        switch_meta_topic = "{}/{}/meta/switch".format(base_topic, device_id)
    switch_id = str(
        switch.get("switch_device_id") or switch.get("device_id") or ""
    ).strip()
    if switch_id and not _SAFE_ID.fullmatch(switch_id):
        raise ValueError("switch device_id is invalid")

    return {
        "device_id": device_id,
        "family": family,
        "hardware": str(sensor.get("hardware") or "").strip(),
        "sensor_id": sensor_id,
        "data_topic": data_topic,
        "sensor_event_topic": optional_topic(
            sensor.get("event_topic"), "sensor.event_topic"
        ),
        "sensor_availability_topic": optional_topic(
            sensor.get("availability_topic"), "sensor.availability_topic"
        ),
        "switch_meta_topic": switch_meta_topic,
        "switch_count": max(0, int(switch.get("channel_count") or 0)),
        "switch_id": switch_id,
        "location": str(sensor.get("location") or "").strip(),
        "version": str(data.get("version") or "").strip(),
        "address": str(network.get("ipv4addr") or "").strip(),
        "heartbeat_topic": optional_topic(
            status.get("heartbeat_topic"), "status.heartbeat_topic"
        ),
        "heartbeat_interval": max(5, int(status.get("heartbeat_interval") or 60)),
    }


def _atomic_json(path, document):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".nodus-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DiscoveryManager:
    """Persist validated discoveries without changing WeeWX configuration."""

    def __init__(self, settings):
        self.settings = settings
        self.registry_file = settings.get(
            "registry_file", "/var/lib/weewx/nodus_discovery.json"
        )
        self.registry = self._load_registry()

    def _load_registry(self):
        try:
            with open(self.registry_file, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            if document.get("schema") == "nodus-weewx-discovery/v1":
                return document
        except (OSError, ValueError):
            pass
        return {"schema": "nodus-weewx-discovery/v1", "devices": {}}

    def _save_registry(self):
        self.registry["updated"] = int(time.time())
        _atomic_json(self.registry_file, self.registry)

    def register(self, descriptor):
        """Record one validated descriptor and return its registry entry."""
        device_id = descriptor["device_id"]
        if device_id not in self.registry["devices"] and len(
            self.registry["devices"]
        ) >= int(self.settings.get("max_devices") or 32):
            raise ValueError("maximum discovered device count reached")
        previous = self.registry["devices"].get(device_id) or {}
        entry = dict(descriptor)
        now = int(time.time())
        entry["first_seen"] = int(previous.get("first_seen") or now)
        entry["last_seen"] = now
        self.registry["devices"][device_id] = entry
        descriptor_changed = any(
            previous.get(key) != value for key, value in descriptor.items()
        )
        last_persisted = int(previous.get("last_seen") or 0)
        if not previous or descriptor_changed or now - last_persisted >= 60:
            self._save_registry()
        return entry

    def remove(self, device_id):
        """Remove one exact registry entry without suppressing rediscovery."""
        removed = self.registry["devices"].pop(device_id, None)
        if removed is not None:
            self._save_registry()
        return removed


class DiscoveryService:
    """Maintain the broker subscription that feeds the discovery manager."""

    def __init__(self, settings, manager=None, mqtt_module=None):
        self.settings = settings
        self.manager = manager or DiscoveryManager(settings)
        if mqtt_module is None:
            import paho.mqtt.client as mqtt_module
        callback_api = getattr(mqtt_module, "CallbackAPIVersion", None)
        if callback_api is not None:
            self.client = mqtt_module.Client(
                callback_api.VERSION1, client_id="nodus-weewx-discovery"
            )
        else:
            self.client = mqtt_module.Client(client_id="nodus-weewx-discovery")
        username = str(settings.get("username") or "")
        if username:
            self.client.username_pw_set(username, str(settings.get("password") or ""))
        if settings.get("use_tls"):
            self.client.tls_set(ca_certs=settings.get("ca_certs") or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.manager_backend = None
        self._subscriptions = set()

    def subscribe_once(self, topic, qos=0):
        """Subscribe once per broker connection to avoid retained-message loops."""
        clean_topic = _clean_subscription_topic(topic)
        if clean_topic in self._subscriptions:
            return False
        self.client.subscribe(clean_topic, qos=qos)
        self._subscriptions.add(clean_topic)
        return True

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if int(reason_code) == 0:
            base = _clean_topic(self.settings.get("base_topic"), "base_topic")
            self._subscriptions.clear()
            self.subscribe_once("{}/+/meta".format(base), qos=0)
        else:
            log.error("Nodus discovery MQTT connect failed: %s", reason_code)

    def _on_message(self, client, _userdata, message):
        try:
            base = _clean_topic(self.settings.get("base_topic"), "base_topic")
            if message.topic.endswith("/meta") and not message.topic.endswith(
                "/meta/switch"
            ):
                descriptor = parse_meta(message.payload, message.topic, base)
                entry = self.manager.register(descriptor)
                device_id = descriptor["device_id"]
                self.subscribe_once("{}/{}/#".format(base, device_id), qos=0)
                for topic in (
                    entry.get("data_topic"),
                    entry.get("sensor_event_topic"),
                    entry.get("sensor_availability_topic"),
                    entry.get("switch_meta_topic"),
                    entry.get("heartbeat_topic"),
                ):
                    if topic:
                        self.subscribe_once(topic, qos=0)
                if self.manager_backend:
                    self.manager_backend.observe(device_id, message, entry)
            elif self.manager_backend:
                self.manager_backend.observe_topic(message)
        except Exception as exc:
            log.warning(
                "Ignored Nodus discovery message topic=%s: %s",
                message.topic,
                exc,
            )

    def run(self):
        """Connect to the configured broker and run until stopped."""
        self.client.connect(
            str(self.settings.get("broker") or "localhost"),
            int(self.settings.get("port") or 1883),
            keepalive=30,
        )
        self.client.loop_forever()


_SAFE_SETTING = re.compile(r"^[\x20-\x7e]{0,160}$")


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else fallback
    except (OSError, ValueError):
        return fallback


def _toml_quote(value):
    return '"{}"'.format(str(value).replace("\\", "\\\\").replace('"', '\\"'))


def _broker_ipv4(value):
    """Return the broker's first resolved IPv4 address when available."""
    host = str(value or "localhost").strip() or "localhost"
    try:
        rows = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        for row in rows:
            address = str(row[4][0] or "").strip()
            if address:
                return address
    except OSError:
        pass
    return host


def _host_timezone(configured=""):
    """Return the host's configured IANA time zone when it can be identified."""
    value = str(configured or "").strip()
    if value:
        return value
    value = str(os.environ.get("TZ") or "").strip().lstrip(":")
    if value:
        return value
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    names = [str(name or "").strip() for name in time.tzname]
    return next((name for name in names if name), "Unknown")


def read_system_settings(path):
    """Read the small host-owned Nodus system TOML document."""
    defaults = {
        "title": "Nodus Automation Instrumentorum",
        "online_timeout_seconds": 150,
        "auto_provision": True,
    }
    try:
        import tomllib

        with open(path, "rb") as handle:
            section = tomllib.load(handle).get("System") or {}
        defaults["title"] = str(section.get("TITLE") or defaults["title"])
        defaults["online_timeout_seconds"] = max(
            30, min(3600, int(section.get("ONLINE_TIMEOUT_SECONDS") or 150))
        )
        defaults["auto_provision"] = bool(section.get("AUTO_PROVISION", True))
    except (OSError, ValueError, TypeError, ImportError):
        pass
    return defaults


def write_system_settings(path, settings):
    """Atomically save validated host-local system settings as TOML."""
    title = str(settings.get("title") or "").strip()
    if not title or len(title) > 80 or not _SAFE_SETTING.fullmatch(title):
        raise ValueError("system title is invalid")
    timeout = max(30, min(3600, int(settings.get("online_timeout_seconds") or 150)))
    enabled = bool(settings.get("auto_provision", True))
    template = (
        "[System]\nTITLE = {}\nONLINE_TIMEOUT_SECONDS = {}\nAUTO_PROVISION = {}\n"
    )
    payload = template.format(
        _toml_quote(title), timeout, "true" if enabled else "false"
    )
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".nodus-system-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NodusManager:
    """Own persistent discovery, liveness, removal, and system settings."""

    def __init__(self, settings, discovery):
        self.settings = settings
        self.discovery = discovery
        self.discovery_service = None
        self.lock = threading.RLock()
        self.live_seen = {}
        self.topic_ids = {}
        self.system_file = str(
            settings.get("system_settings_file") or "/var/lib/weewx/nodus_system.toml"
        )
        self.request_file = str(
            settings.get("request_file") or "/var/lib/weewx/nodus_manager_request.json"
        )
        self.result_file = str(
            settings.get("result_file") or "/var/lib/weewx/nodus_manager_result.json"
        )
        self.installed_file = str(
            settings.get("installed_file") or "/var/lib/weewx/nodus_installed.json"
        )
        self.web_root = Path(
            str(settings.get("system_web_root") or "/etc/weewx/skins/Nodus/system")
        )
        self.host = str(settings.get("admin_host") or "0.0.0.0")
        self.port = int(settings.get("admin_port") or 8768)
        self.server = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="nodus-manager-http", daemon=True
        )
        self.thread.start()

    def observe(self, device_id, message, entry):
        """Record a metadata observation and request installation when vacant."""
        if not bool(getattr(message, "retain", False)):
            self.live_seen[device_id] = time.time()
        self.topic_ids.setdefault(device_id, set()).update(
            topic
            for topic in (
                entry.get("data_topic"),
                entry.get("switch_meta_topic"),
                entry.get("heartbeat_topic"),
                entry.get("sensor_event_topic"),
                entry.get("sensor_availability_topic"),
                "{}/{}/meta".format(
                    self.settings.get("base_topic") or "nodus", device_id
                ),
            )
            if topic
        )
        installed = _read_json(self.installed_file, {}).get("device_id")
        if not installed and read_system_settings(self.system_file)["auto_provision"]:
            try:
                self._action("install", device_id)
            except Exception as exc:
                log.error(
                    "Automatic Nodus provisioning failed for %s: %s", device_id, exc
                )

    def observe_topic(self, message):
        """Track live traffic and exact switch/channel topics for removal."""
        topic = str(message.topic or "")
        devices = self.discovery.registry.get("devices") or {}
        for device_id, entry in devices.items():
            base = "{}/{}/".format(
                self.settings.get("base_topic") or "nodus", device_id
            )
            known = self.topic_ids.setdefault(device_id, set())
            if topic.startswith(base) or topic in known:
                known.add(topic)
                if not bool(getattr(message, "retain", False)):
                    self.live_seen[device_id] = time.time()
                if topic == entry.get("switch_meta_topic"):
                    try:
                        data = json.loads(message.payload.decode("utf-8"))
                        switch_id = str(data.get("switch_device_id") or "").strip()
                        if switch_id and _SAFE_ID.fullmatch(switch_id):
                            entry["switch_id"] = switch_id
                            self.discovery._save_registry()
                        for channel in data.get("channels") or ():
                            channel_id = str(channel.get("channel_id") or "").strip()
                            if channel_id and _SAFE_ID.fullmatch(channel_id):
                                self.topic_ids.setdefault(device_id, set()).add(
                                    "{}/{}/state".format(
                                        self.settings.get("base_topic") or "nodus",
                                        channel_id,
                                    )
                                )
                            for key in (
                                "state_topic",
                                "availability_topic",
                                "set_topic",
                                "ack_topic",
                                "result_topic",
                                "event_topic",
                            ):
                                if channel.get(key):
                                    channel_topic = _clean_topic(
                                        channel[key], "switch channel topic"
                                    )
                                    known.add(channel_topic)
                                    self.discovery_service.subscribe_once(channel_topic)
                    except Exception:
                        pass
                break

    def _online(self, device_id, entry):
        timeout = read_system_settings(self.system_file)["online_timeout_seconds"]
        timeout = max(timeout, int(entry.get("heartbeat_interval") or 60) * 2 + 15)
        seen = float(self.live_seen.get(device_id) or 0)
        return bool(seen and time.time() - seen <= timeout)

    def snapshot(self):
        """Return system information and explicit installed/discovered devices."""
        installed = _read_json(self.installed_file, {})
        installed_id = str(installed.get("device_id") or "")
        devices = dict(self.discovery.registry.get("devices") or {})
        if installed_id and installed_id not in devices:
            devices[installed_id] = {"device_id": installed_id}
        rows = []
        for device_id, entry in sorted(devices.items()):
            rows.append(
                {
                    "device_id": device_id,
                    "switch_id": str(entry.get("switch_id") or ""),
                    "address": str(entry.get("address") or ""),
                    "last_seen": int(entry.get("last_seen") or 0),
                    "online": self._online(device_id, entry),
                    "installed": device_id == installed_id,
                    "discovered": device_id
                    in self.discovery.registry.get("devices", {}),
                }
            )
        return {
            "ok": True,
            "settings": read_system_settings(self.system_file),
            "system": {
                "hostname": socket.gethostname(),
                "http_port": self.port,
                "broker": _broker_ipv4(self.settings.get("broker")),
                "mqtt_port": int(self.settings.get("port") or 1883),
                "tls": bool(self.settings.get("use_tls")),
                "timezone": _host_timezone(self.settings.get("timezone")),
                "location": str(self.settings.get("location") or ""),
                "latitude": self.settings.get("latitude"),
                "longitude": self.settings.get("longitude"),
                "altitude": self.settings.get("altitude"),
                "weewx_service": str(
                    self.settings.get("operational_service") or "weewx@nodus.service"
                ),
                "weewx_config": str(
                    self.settings.get("operational_config") or "/etc/weewx/nodus.conf"
                ),
                "database": str(
                    self.settings.get("database") or "/var/lib/weewx/nodus.sdb"
                ),
                "dashboard": str(
                    self.settings.get("dashboard_web_root")
                    or "/var/www/html/weewx/nodus"
                ),
            },
            "devices": rows,
        }

    def _action(self, action, device_id):
        if not _SAFE_ID.fullmatch(str(device_id or "")):
            raise ValueError("device_id is invalid")
        token = "{}-{}-{}".format(action, int(time.time() * 1000), os.getpid())
        _atomic_json(
            self.request_file,
            {"action": action, "device_id": device_id, "token": token},
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = _read_json(self.result_file, {})
            if result.get("token") == token:
                if not result.get("ok"):
                    raise ValueError(
                        str(result.get("error") or "manager action failed")
                    )
                return result
            time.sleep(0.1)
        raise ValueError("manager action timed out")

    def remove_devices(self, device_ids):
        """Clear exact retained topics, local records, and installed state."""
        results = {}
        total_cleared = 0
        total_failed = 0
        for device_id in device_ids:
            entry = (self.discovery.registry.get("devices") or {}).get(device_id) or {}
            installed = (
                _read_json(self.installed_file, {}).get("device_id") == device_id
            )
            if not entry and not installed:
                raise ValueError("selected device is not installed or discovered")
            topics = set(self.topic_ids.get(device_id) or ())
            base = self.settings.get("base_topic") or "nodus"
            exact_ids = [device_id]
            switch_id = str(entry.get("switch_id") or "").strip()
            if switch_id and switch_id not in exact_ids:
                exact_ids.append(switch_id)
            for exact_id in exact_ids:
                for suffix in (
                    "meta",
                    "meta/switch",
                    "meta/patch",
                    "availability",
                    "state",
                    "status/heartbeat",
                ):
                    topics.add("{}/{}/{}".format(base, exact_id, suffix))
            for topic in (
                entry.get("data_topic"),
                entry.get("switch_meta_topic"),
                entry.get("heartbeat_topic"),
                entry.get("sensor_event_topic"),
                entry.get("sensor_availability_topic"),
            ):
                if topic:
                    topics.add(topic)
            cleared = 0
            failed = 0
            for topic in sorted(topics):
                result = self.discovery_service.client.publish(
                    topic, payload="", qos=0, retain=True
                )
                waiter = getattr(result, "wait_for_publish", None)
                if callable(waiter):
                    waiter(timeout=5)
                if getattr(result, "rc", 1) == 0 and (
                    not hasattr(result, "is_published") or result.is_published()
                ):
                    cleared += 1
                else:
                    failed += 1
            action = self._action("remove", device_id) if installed else {"ok": True}
            self.discovery.remove(device_id)
            self.live_seen.pop(device_id, None)
            self.topic_ids.pop(device_id, None)
            results[device_id] = {
                "installed": installed,
                "retained_topics_cleared": cleared,
                "retained_topic_failures": failed,
                "action": action,
            }
            total_cleared += cleared
            total_failed += failed
        message = "Removed {} device(s); cleared {} retained MQTT topic(s).".format(
            len(results), total_cleared
        )
        if total_failed:
            message += " {} retained cleanup publish(es) failed.".format(total_failed)
        return {
            "ok": True,
            "message": message,
            "results": results,
        }

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
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
                if length < 1 or length > 65536:
                    raise ValueError("request body size is invalid")
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/system":
                    self._json(200, outer.snapshot())
                    return
                relative = (
                    "index.html"
                    if path in ("/", "/system", "/system/")
                    else path.removeprefix("/system/")
                )
                target = (outer.web_root / relative).resolve()
                try:
                    target.relative_to(outer.web_root.resolve())
                    payload = target.read_bytes()
                except (ValueError, OSError):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                try:
                    body = self._body()
                    path = urlparse(self.path).path
                    if path == "/api/system":
                        write_system_settings(outer.system_file, body)
                        data = {"ok": True, "message": "System settings saved."}
                    elif path == "/api/remove":
                        if body.get("confirm") is not True:
                            raise ValueError("removal confirmation is required")
                        ids = [str(value) for value in body.get("device_ids") or ()]
                        if not ids:
                            raise ValueError("select at least one device")
                        data = outer.remove_devices(ids)
                    else:
                        self._json(404, {"ok": False, "error": "not found"})
                        return
                    self._json(200, data)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    log.exception("Nodus manager request failed")
                    self._json(500, {"ok": False, "error": str(exc)})

            def log_message(self, fmt, *args):
                log.info("Nodus manager: " + fmt, *args)

        return Handler

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def main(argv=None):
    """Run the standalone Nodus WeeWX discovery service."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/weewx/nodus-discovery.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with open(args.config, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
    discovery = DiscoveryManager(settings)
    service = DiscoveryService(settings, manager=discovery)
    manager = NodusManager(settings, discovery)
    manager.discovery_service = service
    service.manager_backend = manager
    try:
        service.run()
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
