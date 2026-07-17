"""Run threshold-and-time Nodus switch automations from WeeWX."""

import json
import logging
import os
import re
import threading
import time

try:
    import weewx
    from weewx.cheetahgenerator import SearchList
    from weewx.engine import StdService
except ImportError:  # Allow host-side tests without a WeeWX installation.
    weewx = None

    class StdService:
        """Minimal test fallback for the WeeWX service base class."""

        def __init__(self, engine, config_dict):
            self.engine = engine

        def bind(self, *_args, **_kwargs):
            return None

    class SearchList:
        """Minimal test fallback for the WeeWX SearchList base class."""

        def __init__(self, generator):
            self.generator = generator


log = logging.getLogger(__name__)

_DEFAULT_STATUS_FILE = "/var/lib/weewx/nodus_automation.json"
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _to_bool(value, default=False):
    """Return a permissive boolean for ConfigObj scalar values."""
    if value is None:
        return bool(default)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_list(value):
    """Normalize ConfigObj scalar/list values to stripped strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = str(value).split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def _parse_clock(value):
    """Return minutes after midnight for a HH:MM value."""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("time must use HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time is outside 00:00-23:59")
    return hour * 60 + minute


def _time_window_active(timestamp, start_minute, end_minute, days=None):
    """Return whether a local timestamp is inside a daily time window."""
    local = time.localtime(timestamp)
    allowed_days = set(days or _DAY_NAMES)
    if _DAY_NAMES[local.tm_wday] not in allowed_days:
        return False
    minute = local.tm_hour * 60 + local.tm_min
    if start_minute == end_minute:
        return True
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def _condition_active(condition, value, previous=False):
    """Apply one above/below condition with hysteresis."""
    value = float(value)
    direction = condition["direction"]
    on_threshold = condition["on_threshold"]
    off_threshold = condition["off_threshold"]
    if direction == "above":
        if previous:
            return value > off_threshold
        return value >= on_threshold
    if previous:
        return value < off_threshold
    return value <= on_threshold


def _parse_condition(value, name):
    """Parse ``observation, direction, on, off`` from one rule setting."""
    parts = _as_list(value)
    if len(parts) != 4:
        raise ValueError(
            "{} must contain observation, above|below, on, off".format(name)
        )
    direction = parts[1].lower()
    if direction not in ("above", "below"):
        raise ValueError("{} direction must be above or below".format(name))
    on_threshold = float(parts[2])
    off_threshold = float(parts[3])
    if direction == "above" and off_threshold >= on_threshold:
        raise ValueError("{} off threshold must be below on threshold".format(name))
    if direction == "below" and off_threshold <= on_threshold:
        raise ValueError("{} off threshold must be above on threshold".format(name))
    return {
        "name": name,
        "observation": parts[0],
        "direction": direction,
        "on_threshold": on_threshold,
        "off_threshold": off_threshold,
        "active": False,
    }


def _section_names(section):
    """Return ConfigObj subsection names or plain mapping keys."""
    names = getattr(section, "sections", None)
    if names is not None:
        return list(names)
    return [name for name, value in section.items() if hasattr(value, "items")]


def _parse_rules(section):
    """Build validated automation rules from a ConfigObj-like section."""
    rules_section = section.get("rules", {})
    rules = []
    used_channels = set()
    for rule_name in _section_names(rules_section):
        source = rules_section[rule_name]
        if not _to_bool(source.get("enabled"), True):
            continue
        conditions = []
        for key in sorted(source):
            if re.match(r"^condition_[1-9][0-9]*$", str(key)):
                conditions.append(_parse_condition(source[key], str(key)))
        if not conditions:
            raise ValueError("rule {} has no conditions".format(rule_name))
        channel_id = str(source.get("channel_id") or "").strip()
        if not channel_id:
            raise ValueError("rule {} has no channel_id".format(rule_name))
        if channel_id in used_channels:
            raise ValueError(
                "more than one enabled rule targets {}".format(channel_id)
            )
        used_channels.add(channel_id)
        days = [item[:3].lower() for item in _as_list(source.get("days"))]
        invalid_days = [item for item in days if item not in _DAY_NAMES]
        if invalid_days:
            raise ValueError(
                "rule {} has invalid days: {}".format(
                    rule_name, ", ".join(invalid_days)
                )
            )
        outside = str(source.get("outside_window") or "off").strip().lower()
        stale = str(source.get("stale_action") or "off").strip().lower()
        if outside not in ("off", "hold") or stale not in ("off", "hold"):
            raise ValueError("outside_window and stale_action must be off or hold")
        rules.append(
            {
                "name": str(rule_name),
                "channel_id": channel_id,
                "conditions": conditions,
                "start_minute": _parse_clock(source.get("start_time") or "00:00"),
                "end_minute": _parse_clock(source.get("end_time") or "00:00"),
                "days": days or list(_DAY_NAMES),
                "outside_window": outside,
                "stale_action": stale,
                "stale_after": int(
                    source.get("stale_after") or section.get("stale_after") or 180
                ),
                "minimum_on": int(source.get("minimum_on_seconds") or 300),
                "minimum_off": int(source.get("minimum_off_seconds") or 300),
                "retry_seconds": int(source.get("retry_seconds") or 60),
                "retry_not_before": 0,
                "last_decision": "waiting",
                "last_action": "",
                "last_error": "",
            }
        )
    return rules


def _mqtt_section(config_dict):
    """Return the configured MQTTSubscribe driver or service section."""
    for name in ("MQTTSubscribeDriver", "MQTTSubscribeService"):
        section = config_dict.get(name)
        if section:
            return section
    raise ValueError("MQTTSubscribe configuration not found")


def _derive_meta_topic(config_dict, configured_topic=""):
    """Resolve the exact retained switch metadata topic."""
    if configured_topic:
        return str(configured_topic).strip()
    topics = _mqtt_section(config_dict).get("topics", {})
    data_topics = []
    for topic in _section_names(topics):
        text = str(topic).strip()
        if text.endswith("/data") and "+" not in text and "#" not in text:
            data_topics.append(text)
    if len(data_topics) != 1:
        raise ValueError(
            "expected one exact MQTTSubscribe /data topic, found {}".format(
                len(data_topics)
            )
        )
    return data_topics[0][:-5] + "/meta/switch"


def _normalize_switch_state(payload):
    """Read raw ON/OFF or a nodus-switch-state/v1 JSON payload."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    text = str(payload or "").strip()
    if text.upper() in ("ON", "OFF"):
        return text.upper() == "ON"
    data = json.loads(text)
    state = str(data.get("state") or "").strip().upper()
    if data.get("schema") != "nodus-switch-state/v1" or state not in ("ON", "OFF"):
        raise ValueError("unexpected Nodus switch state payload")
    return state == "ON"


def _command_payload(channel, desired, message_id):
    """Build the canonical Nodus switch config/set envelope."""
    return {
        "message_id": message_id,
        "payload": {
            "updates": [
                {
                    "section": "Switch",
                    "key": "SWITCH_{}_LAST_STATE".format(channel["index"]),
                    "value": bool(desired),
                    "name": "switch.toml",
                }
            ]
        },
        "restart": False,
    }


class AutomationController:
    """Evaluate rules and coordinate correlated Nodus switch commands."""

    def __init__(self, rules, publisher, command_timeout=15):
        self.rules = rules
        self.publisher = publisher
        self.command_timeout = max(1, int(command_timeout))
        self.channels = {}
        self.observations = {}
        self.pending = {}
        self.sequence = 0

    def update_meta(self, payload, now=None):
        """Load authoritative retained switch metadata and initial states."""
        now = float(now if now is not None else time.time())
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        data = json.loads(payload) if isinstance(payload, str) else payload
        if data.get("schema") != "nodus-meta-switch/v1":
            raise ValueError("unexpected Nodus switch metadata schema")
        subscriptions = []
        for item in data.get("channels") or ():
            channel_id = str(item.get("channel_id") or "").strip()
            if not channel_id or not item.get("set_topic"):
                continue
            previous = self.channels.get(channel_id, {})
            channel = dict(item)
            channel["state"] = bool(item.get("state"))
            channel["last_changed"] = previous.get("last_changed", now)
            if previous and previous.get("state") != channel["state"]:
                channel["last_changed"] = now
            self.channels[channel_id] = channel
            subscriptions.extend(
                topic
                for topic in (
                    channel.get("state_topic"),
                    channel.get("ack_topic"),
                    channel.get("result_topic"),
                )
                if topic
            )
        return subscriptions

    def update_state(self, topic, payload, now=None):
        """Apply a retained or live channel state message."""
        now = float(now if now is not None else time.time())
        for channel in self.channels.values():
            if channel.get("state_topic") != topic:
                continue
            state = _normalize_switch_state(payload)
            if channel.get("state") != state:
                channel["last_changed"] = now
            channel["state"] = state
            for pending in self.pending.values():
                if pending["channel_id"] == channel["channel_id"]:
                    pending["state_seen"] = state == pending["desired"]
            self._finish_completed(now)
            return True
        return False

    def update_response(self, topic, payload, now=None):
        """Record correlated config/ack or config/result messages."""
        now = float(now if now is not None else time.time())
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        data = json.loads(payload) if isinstance(payload, str) else payload
        pending = self.pending.get(str(data.get("message_id") or ""))
        if not pending:
            return False
        channel = self.channels.get(pending["channel_id"], {})
        if topic == channel.get("ack_topic"):
            pending["ack"] = bool(data.get("accepted"))
            if not pending["ack"]:
                self._fail_pending(pending, "command rejected")
        elif topic == channel.get("result_topic"):
            pending["result"] = bool(data.get("applied"))
            if not pending["result"]:
                self._fail_pending(pending, str(data.get("error") or "apply failed"))
        else:
            return False
        self._finish_completed(now)
        return True

    def evaluate(self, packet=None, now=None):
        """Evaluate all enabled rules against current observations and time."""
        now = float(now if now is not None else time.time())
        packet = packet or {}
        sample_time = float(packet.get("dateTime") or now)
        for name, value in packet.items():
            if name != "dateTime" and value is not None:
                self.observations[name] = (value, sample_time)
        self._expire_pending(now)
        for rule in self.rules:
            self._evaluate_rule(rule, now)

    def _evaluate_rule(self, rule, now):
        channel = self.channels.get(rule["channel_id"])
        if not channel:
            rule["last_decision"] = "waiting for switch metadata"
            return
        pending = next(
            (
                item
                for item in self.pending.values()
                if item["channel_id"] == rule["channel_id"]
            ),
            None,
        )
        if pending:
            rule["last_decision"] = "waiting for command result"
            return

        values_fresh = True
        all_active = True
        for condition in rule["conditions"]:
            reading = self.observations.get(condition["observation"])
            if not reading or now - reading[1] > rule["stale_after"]:
                values_fresh = False
                continue
            condition["active"] = _condition_active(
                condition, reading[0], condition["active"]
            )
            all_active = all_active and condition["active"]

        in_window = _time_window_active(
            now, rule["start_minute"], rule["end_minute"], rule["days"]
        )
        desired = None
        if not values_fresh:
            rule["last_decision"] = "sensor data stale"
            if rule["stale_action"] == "off":
                desired = False
        elif not in_window:
            rule["last_decision"] = "outside active window"
            if rule["outside_window"] == "off":
                desired = False
        else:
            desired = bool(all_active)
            rule["last_decision"] = (
                "conditions active" if desired else "conditions idle"
            )

        if desired is None or channel.get("state") == desired:
            return
        if now < rule["retry_not_before"]:
            rule["last_decision"] += "; retry cooldown"
            return
        dwell = rule["minimum_on"] if channel.get("state") else rule["minimum_off"]
        if now - channel.get("last_changed", now) < dwell:
            rule["last_decision"] += "; minimum dwell"
            return
        self._send(rule, channel, desired, now)

    def _send(self, rule, channel, desired, now):
        self.sequence += 1
        token = re.sub(r"[^A-Za-z0-9_.-]+", "-", rule["name"]).strip("-")
        message_id = "weewx-{}-{}-{}".format(token or "rule", int(now), self.sequence)
        payload = _command_payload(channel, desired, message_id)
        published = self.publisher(
            channel["set_topic"],
            json.dumps(payload, separators=(",", ":")),
            False,
        )
        if published is False:
            rule["last_error"] = "MQTT publish failed"
            rule["retry_not_before"] = now + max(1, rule["retry_seconds"])
            log.warning(
                "Nodus automation %s failed to publish %s",
                rule["name"],
                channel["set_topic"],
            )
            return
        self.pending[message_id] = {
            "message_id": message_id,
            "rule": rule,
            "channel_id": channel["channel_id"],
            "desired": bool(desired),
            "sent": now,
            "ack": False,
            "result": False,
            "state_seen": False,
        }
        rule["retry_not_before"] = now + max(1, rule["retry_seconds"])
        rule["last_action"] = "requested {}".format("ON" if desired else "OFF")
        rule["last_error"] = ""
        log.info(
            "Nodus automation %s requested %s on %s message_id=%s",
            rule["name"],
            "ON" if desired else "OFF",
            channel["channel_id"],
            message_id,
        )

    def _finish_completed(self, now):
        for message_id, pending in list(self.pending.items()):
            if pending["ack"] and pending["result"] and pending["state_seen"]:
                rule = pending["rule"]
                rule["last_action"] = "confirmed {} at {}".format(
                    "ON" if pending["desired"] else "OFF",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                )
                rule["last_error"] = ""
                log.info(
                    "Nodus automation %s confirmed %s message_id=%s",
                    rule["name"],
                    "ON" if pending["desired"] else "OFF",
                    message_id,
                )
                del self.pending[message_id]

    def _fail_pending(self, pending, error):
        pending["rule"]["last_error"] = error
        log.warning(
            "Nodus automation %s failed message_id=%s error=%s",
            pending["rule"]["name"],
            pending["message_id"],
            error,
        )
        self.pending.pop(pending["message_id"], None)

    def _expire_pending(self, now):
        for pending in list(self.pending.values()):
            if now - pending["sent"] > self.command_timeout:
                self._fail_pending(pending, "command confirmation timed out")

    def status(self, enabled=True, now=None):
        """Return a JSON-safe status snapshot for Nodus."""
        now = int(now if now is not None else time.time())
        rows = []
        for rule in self.rules:
            channel = self.channels.get(rule["channel_id"], {})
            rows.append(
                {
                    "name": rule["name"],
                    "channel_id": rule["channel_id"],
                    "label": channel.get("label") or rule["channel_id"],
                    "state": "ON" if channel.get("state") else "OFF"
                    if "state" in channel
                    else "unknown",
                    "decision": rule["last_decision"],
                    "last_action": rule["last_action"] or "none",
                    "error": rule["last_error"],
                }
            )
        return {"enabled": bool(enabled), "updated": now, "rules": rows}


def _write_status(path, status):
    """Atomically write the skin-facing automation status file."""
    temporary = "{}.tmp.{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(status, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _new_mqtt_client(mqtt_module, client_id):
    """Create a Paho client compatible with callback API v1 and v2."""
    callback_api = getattr(mqtt_module, "CallbackAPIVersion", None)
    if callback_api is not None:
        return mqtt_module.Client(callback_api.VERSION1, client_id=client_id)
    return mqtt_module.Client(client_id=client_id)


class NodusAutomation(StdService):
    """WeeWX data service for Nodus metric/time switch automations."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)
        section = config_dict.get("NodusAutomation", {})
        self.enabled = _to_bool(section.get("enabled"), False)
        self.status_file = str(section.get("status_file") or _DEFAULT_STATUS_FILE)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_status = ""
        self.client = None
        self.controller = AutomationController([], lambda *_args: False)
        if not self.enabled:
            self._save_status()
            return

        rules = _parse_rules(section)
        if not rules:
            raise ValueError("NodusAutomation is enabled but has no enabled rules")
        mqtt_section = _mqtt_section(config_dict)
        self.meta_topic = _derive_meta_topic(config_dict, section.get("meta_topic"))
        self.controller = AutomationController(
            rules, self._publish, section.get("command_timeout") or 15
        )

        import paho.mqtt.client as mqtt

        self.client = _new_mqtt_client(
            mqtt, "weewx-nodus-automation-{}".format(os.getpid())
        )
        username = str(section.get("username") or mqtt_section.get("username") or "")
        password = str(section.get("password") or mqtt_section.get("password") or "")
        if username:
            self.client.username_pw_set(username, password)
        tls = mqtt_section.get("tls", {})
        if _to_bool(section.get("tls_enable"), _to_bool(tls.get("enable"), False)):
            ca_certs = str(section.get("ca_certs") or tls.get("ca_certs") or "")
            self.client.tls_set(ca_certs=ca_certs or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        host = str(section.get("host") or mqtt_section.get("host") or "localhost")
        port = int(section.get("port") or mqtt_section.get("port") or 1883)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()
        if weewx is not None:
            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)
        self.watchdog = threading.Thread(
            target=self._watchdog, name="nodus-automation", daemon=True
        )
        self.watchdog.start()
        self._save_status()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        try:
            connected = int(reason_code) == 0
        except (TypeError, ValueError):
            connected = reason_code == 0
        if connected:
            client.subscribe(self.meta_topic, qos=0)
        else:
            log.error("Nodus automation MQTT connect failed: %s", reason_code)

    def _on_message(self, client, _userdata, message):
        try:
            with self.lock:
                if message.topic == self.meta_topic:
                    for topic in self.controller.update_meta(message.payload):
                        client.subscribe(topic, qos=0)
                elif not self.controller.update_state(message.topic, message.payload):
                    self.controller.update_response(message.topic, message.payload)
                self._save_status()
        except Exception as exc:
            log.error("Nodus automation MQTT message failed: %s", exc)

    def _publish(self, topic, payload, retain=False):
        result = self.client.publish(topic, payload, qos=0, retain=retain)
        return getattr(result, "rc", 1) == 0

    def new_loop_packet(self, event):
        """Evaluate rules after MQTTSubscribe has populated a loop packet."""
        with self.lock:
            self.controller.evaluate(getattr(event, "packet", {}) or {})
            self._save_status()

    def _watchdog(self):
        while not self.stop_event.wait(5):
            with self.lock:
                self.controller.evaluate()
                self._save_status()

    def _save_status(self):
        status = self.controller.status(enabled=self.enabled)
        comparison = dict(status)
        comparison["updated"] = 0
        serialized = json.dumps(comparison, separators=(",", ":"), sort_keys=True)
        if serialized == self.last_status:
            return
        try:
            _write_status(self.status_file, status)
            self.last_status = serialized
        except Exception as exc:
            log.warning("Unable to write Nodus automation status: %s", exc)

    def shutDown(self):
        """Stop the watchdog and MQTT client during WeeWX shutdown."""
        self.stop_event.set()
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            watchdog.join(timeout=6)
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


class NodusAutomationStatus(SearchList):
    """Expose automation status to Nodus Cheetah templates."""

    def __init__(self, generator):
        super().__init__(generator)
        section = generator.skin_dict.get("NodusAutomationStatus", {})
        path = str(section.get("status_file") or _DEFAULT_STATUS_FILE)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                self.nodus_automation = json.load(handle)
        except Exception:
            self.nodus_automation = {"enabled": False, "updated": 0, "rules": []}
