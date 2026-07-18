"""Collect Nodus switch identity, state, and events for WeeWX skins."""

import json
import logging
import os
import re
import threading
import time

try:
    from weewx.cheetahgenerator import SearchList
    from weewx.engine import StdService
except ImportError:  # Allow host-side tests without a WeeWX installation.
    class StdService:
        """Minimal test fallback for the WeeWX service base class."""

        def __init__(self, engine, config_dict):
            self.engine = engine

    class SearchList:
        """Minimal test fallback for the WeeWX SearchList base class."""

        def __init__(self, generator):
            self.generator = generator


log = logging.getLogger(__name__)

_DEFAULT_STATUS_FILE = "/var/lib/weewx/nodus_switch.json"
_DEFAULT_CONTROL_FILE = "/var/lib/weewx/nodus_switch_control.json"
_DEFAULT_MAX_EVENTS = 20
_MANUAL_GUARD_SECONDS = 5


def _to_bool(value, default=False):
    """Return a permissive boolean for ConfigObj scalar values."""
    if value is None:
        return bool(default)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _section_names(section):
    """Return ConfigObj subsection names or plain mapping keys."""
    names = getattr(section, "sections", None)
    if names is not None:
        return list(names)
    return [name for name, value in section.items() if hasattr(value, "items")]


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


def _automation_channels(config_dict):
    """Return channels targeted by enabled host-side automation rules."""
    return set(_automation_owners(config_dict))


def _automation_owners(config_dict):
    """Return enabled automation rule names keyed by owned switch channel."""
    section = config_dict.get("NodusAutomation", {})
    if not _to_bool(section.get("enabled"), False):
        return {}
    rules_file = str(section.get("rules_file") or "").strip()
    if rules_file:
        try:
            with open(rules_file, "r", encoding="utf-8") as handle:
                rules = json.load(handle).get("rules", {})
        except (OSError, ValueError):
            rules = {}
    else:
        rules = section.get("rules", {})
    channels = {}
    for name in _section_names(rules):
        rule = rules[name]
        if not _to_bool(rule.get("enabled"), True):
            continue
        actions = rule.get("actions") or ()
        if isinstance(actions, dict):
            actions = actions.values()
        for action in actions:
            channel_id = str(action.get("channel_id") or "").strip()
            if channel_id:
                channels[channel_id] = str(name)
        # Continue to recognize pre-v2 rules until the setup UI rewrites them.
        channel_id = str(rule.get("channel_id") or "").strip()
        if channel_id:
            channels[channel_id] = str(name)
    return channels


def _data_observations(config_dict):
    """Return observation names configured for the exact Nodus data topic."""
    topics = _mqtt_section(config_dict).get("topics", {})
    names = []
    for topic in _section_names(topics):
        if not str(topic).endswith("/data"):
            continue
        fields = topics[topic]
        for field in _section_names(fields):
            name = str(fields[field].get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _normalize_state(payload):
    """Read raw ON/OFF or a nodus-switch-state/v1 JSON payload."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    text = str(payload or "").strip()
    if text.upper() in ("ON", "OFF"):
        return text.upper()
    data = json.loads(text)
    state = str(data.get("state") or "").strip().upper()
    if data.get("schema") != "nodus-switch-state/v1" or state not in (
        "ON",
        "OFF",
    ):
        raise ValueError("unexpected Nodus switch state payload")
    return state


def _event_from_payload(payload, received_at=None):
    """Validate and normalize a canonical Nodus switch event."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    data = json.loads(payload) if isinstance(payload, str) else payload
    state = str(data.get("state") or "").strip().upper()
    if data.get("schema") != "nodus-switch-event/v1" or state not in (
        "ON",
        "OFF",
    ):
        raise ValueError("unexpected Nodus switch event payload")
    timestamp = int(data.get("timestamp") or time.time())
    received_at = int(received_at if received_at is not None else time.time())
    return {
        "state": state,
        "timestamp": timestamp,
        "received_at": received_at,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(received_at)),
        "message_id": str(data.get("message_id") or ""),
    }


class SwitchStatusController:
    """Maintain a skin-safe snapshot of discovered switch channels."""

    def __init__(
        self,
        max_events=_DEFAULT_MAX_EVENTS,
        automation_channels=None,
        automation_owners=None,
        manual_countdowns=None,
        manual_deadlines=None,
    ):
        self.max_events = max(1, int(max_events))
        self.automation_channels = set(automation_channels or ())
        self.automation_owners = dict(automation_owners or {})
        self.manual_countdowns = dict(manual_countdowns or {})
        self.manual_deadlines = dict(manual_deadlines or {})
        self.device_id = ""
        self.switch_device_id = ""
        self.location = ""
        self.channels = {}
        self.updated = 0

    def restore(self, status):
        """Restore cached channels and event history before MQTT reconnect."""
        if not isinstance(status, dict):
            return
        self.device_id = str(status.get("device_id") or "")
        self.switch_device_id = str(status.get("switch_device_id") or "")
        self.location = str(status.get("location") or "")
        for item in status.get("channels") or ():
            channel_id = str(item.get("channel_id") or "").strip()
            if channel_id:
                channel = dict(item)
                channel["events"] = list(channel.get("events") or ())[
                    : self.max_events
                ]
                self._apply_control_state(channel)
                for event in channel.get("events") or ():
                    if not event.get("received_at") and event.get("timestamp"):
                        event["time"] = time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.gmtime(int(event["timestamp"])),
                        )
                    self._format_event(channel_id, event)
                self.channels[channel_id] = channel
        self.updated = int(status.get("updated") or 0)

    def update_meta(self, payload, now=None):
        """Load authoritative switch metadata and return channel topics."""
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        data = json.loads(payload) if isinstance(payload, str) else payload
        if data.get("schema") != "nodus-meta-switch/v1":
            raise ValueError("unexpected Nodus switch metadata schema")
        self.device_id = str(data.get("device_id") or "")
        self.switch_device_id = str(data.get("switch_device_id") or "")
        self.location = str(data.get("location") or "")
        subscriptions = []
        channels = {}
        for item in data.get("channels") or ():
            channel_id = str(item.get("channel_id") or "").strip()
            if not channel_id:
                continue
            previous = self.channels.get(channel_id, {})
            state = "ON" if bool(item.get("state")) else "OFF"
            channel = {
                "index": int(item.get("index") or 0),
                "channel_id": channel_id,
                "label": str(item.get("label") or channel_id),
                "state": state,
                "state_topic": str(item.get("state_topic") or ""),
                "event_topic": str(item.get("event_topic") or ""),
                "set_topic": str(item.get("set_topic") or ""),
                "ack_topic": str(item.get("ack_topic") or ""),
                "result_topic": str(item.get("result_topic") or ""),
                "events": list(previous.get("events") or ())[: self.max_events],
            }
            self._apply_control_state(channel)
            for event in channel["events"]:
                self._format_event(channel_id, event)
            channels[channel_id] = channel
            subscriptions.extend(
                topic
                for topic in (
                    channel["state_topic"],
                    channel["event_topic"],
                    channel["ack_topic"],
                    channel["result_topic"],
                )
                if topic
            )
        self.channels = channels
        self.updated = int(now if now is not None else time.time())
        return subscriptions

    def _automation_label(self, channel_id):
        """Return the skin label for a channel's WeeWX automation state."""
        if channel_id in self.automation_channels:
            return "Automation enabled"
        seconds = max(0, int(self.manual_countdowns.get(channel_id) or 0))
        if seconds:
            return "Manual mode · {}s countdown".format(seconds)
        return "Manual mode"

    def _apply_control_state(self, channel):
        """Attach automation ownership and manual countdown state."""
        channel_id = channel["channel_id"]
        automated = channel_id in self.automation_channels
        channel["automation_enabled"] = automated
        channel["mode_class"] = "automated" if automated else "manual"
        channel["automation"] = self._automation_label(channel_id)
        channel["countdown_seconds"] = max(
            0, int(self.manual_countdowns.get(channel_id) or 0)
        )
        channel["countdown_until"] = max(
            0, int(self.manual_deadlines.get(channel_id) or 0)
        )

    def _format_event(self, channel_id, event):
        rule = (
            event.get("rule")
            or self.automation_owners.get(channel_id)
            or "Manual"
        )
        event["rule"] = rule
        event["display"] = "{} {} : {}".format(
            event.get("time") or "",
            rule,
            event.get("state") or "",
        ).strip()

    def update_control_state(
        self,
        automation_owners=None,
        manual_countdowns=None,
        manual_deadlines=None,
    ):
        """Refresh host-side ownership and countdown state."""
        if automation_owners is not None:
            self.automation_owners = dict(automation_owners)
            self.automation_channels = set(self.automation_owners)
        if manual_countdowns is not None:
            self.manual_countdowns = dict(manual_countdowns)
        if manual_deadlines is not None:
            self.manual_deadlines = dict(manual_deadlines)
        for channel in self.channels.values():
            self._apply_control_state(channel)
            for event in channel.get("events") or ():
                self._format_event(channel["channel_id"], event)

    def update_message(self, topic, payload, now=None):
        """Apply a channel state or event message."""
        for channel in self.channels.values():
            if channel.get("state_topic") == topic:
                channel["state"] = _normalize_state(payload)
                self.updated = int(now if now is not None else time.time())
                return True
            if channel.get("event_topic") == topic:
                event = _event_from_payload(payload, received_at=now)
                self._format_event(channel["channel_id"], event)
                channel["state"] = event["state"]
                channel.setdefault("events", []).insert(0, event)
                del channel["events"][self.max_events :]
                self.updated = int(now if now is not None else time.time())
                return True
        return False

    def status(self):
        """Return a deterministic JSON-safe skin snapshot."""
        channels = sorted(
            self.channels.values(),
            key=lambda item: (item.get("index", 0), item.get("channel_id", "")),
        )
        return {
            "device_id": self.device_id,
            "switch_device_id": self.switch_device_id,
            "location": self.location,
            "updated": self.updated,
            "channels": channels,
        }


def _read_status(path):
    """Read a previously written switch status snapshot."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _skin_status(status):
    """Normalize older cached switch snapshots for the current skin."""
    result = dict(status)
    channels = []
    for source in status.get("channels") or ():
        channel = dict(source)
        label = str(channel.get("automation") or "")
        automated = bool(channel.get("automation_enabled")) or label in (
            "Timer enabled",
            "Automation enabled",
        )
        channel["automation_enabled"] = automated
        channel["mode_class"] = "automated" if automated else "manual"
        countdown = max(0, int(channel.get("countdown_seconds") or 0))
        if automated:
            channel["automation"] = "Automation enabled"
        elif countdown:
            channel["automation"] = "Manual mode · {}s countdown".format(countdown)
        else:
            channel["automation"] = "Manual mode"
        events = []
        for source_event in (channel.get("events") or ())[:_DEFAULT_MAX_EVENTS]:
            event = dict(source_event)
            if not event.get("received_at") and event.get("timestamp"):
                event["time"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.gmtime(int(event["timestamp"])),
                )
            rule = event.get("rule") or "Manual"
            event["display"] = "{} {} : {}".format(
                event.get("time") or "",
                rule,
                event.get("state") or "",
            ).strip()
            events.append(event)
        channel["events"] = events
        channels.append(channel)
    result["channels"] = channels
    return result


def _write_status(path, status):
    """Atomically write the skin-facing switch status file."""
    temporary = "{}.tmp.{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(status, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _read_control(path):
    """Read persisted manual countdown settings and active deadlines."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    countdowns = data.get("countdown_seconds") or {}
    deadlines = data.get("off_deadlines") or {}
    return (
        {str(key): max(0, int(value or 0)) for key, value in countdowns.items()},
        {str(key): max(0, int(value or 0)) for key, value in deadlines.items()},
    )


def _write_control(path, countdowns, deadlines):
    """Atomically persist manual countdown settings and active deadlines."""
    temporary = "{}.tmp.{}".format(path, os.getpid())
    document = {
        "schema": "nodus-weewx-switch-control/v1",
        "countdown_seconds": countdowns,
        "off_deadlines": deadlines,
    }
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _switch_command_payload(channel, desired, message_id):
    """Build the canonical channel-scoped switch config command."""
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


def _new_mqtt_client(mqtt_module, client_id):
    """Create a Paho client compatible with callback API v1 and v2."""
    callback_api = getattr(mqtt_module, "CallbackAPIVersion", None)
    if callback_api is not None:
        return mqtt_module.Client(callback_api.VERSION1, client_id=client_id)
    return mqtt_module.Client(client_id=client_id)


class NodusSwitchStatus(StdService):
    """Continuously collect Nodus switch metadata, state, and events."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)
        section = config_dict.get("NodusSwitchStatus", {})
        self.enabled = _to_bool(section.get("enabled"), True)
        self.status_file = str(section.get("status_file") or _DEFAULT_STATUS_FILE)
        self.lock = threading.RLock()
        self.client = None
        self.admin = None
        self.pending = {}
        self.manual_guard_until = {}
        self.manual_stop = threading.Event()
        self.manual_thread = None
        self.command_timeout = max(1, int(section.get("command_timeout") or 15))
        self.config_dict = config_dict
        self.device_meta = {}
        self.service_started = int(time.time())
        self.mqtt_connected = False
        self.mqtt_connected_at = 0
        self.last_disconnect_at = 0
        self.disconnect_count = 0
        self.messages_received = 0
        self.last_message_at = 0
        self.mqtt_host = ""
        self.rules_file = str(
            section.get("rules_file")
            or config_dict.get("NodusAutomation", {}).get("rules_file")
            or "/var/lib/weewx/nodus_automation_rules.json"
        )
        self.control_file = str(
            section.get("control_file") or _DEFAULT_CONTROL_FILE
        )
        self.manual_countdowns, self.manual_deadlines = _read_control(
            self.control_file
        )
        automation_owners = _automation_owners(config_dict)
        self.controller = SwitchStatusController(
            section.get("max_events") or _DEFAULT_MAX_EVENTS,
            automation_owners,
            automation_owners,
            self.manual_countdowns,
            self.manual_deadlines,
        )
        try:
            self.controller.restore(_read_status(self.status_file))
        except Exception:
            pass
        if not self.enabled:
            return

        mqtt_section = _mqtt_section(config_dict)
        self.meta_topic = _derive_meta_topic(config_dict, section.get("meta_topic"))
        self.device_topic = self.meta_topic[: -len("/meta/switch")]

        import paho.mqtt.client as mqtt

        self.client = _new_mqtt_client(
            mqtt, "weewx-nodus-switch-{}".format(os.getpid())
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
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        host = str(section.get("host") or mqtt_section.get("host") or "localhost")
        self.mqtt_host = host
        port = int(section.get("port") or mqtt_section.get("port") or 1883)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()
        self.manual_thread = threading.Thread(
            target=self._manual_countdown_loop,
            name="nodus-manual-countdown",
            daemon=True,
        )
        self.manual_thread.start()
        if _to_bool(section.get("admin_enabled"), False):
            try:
                from user.nodus_admin import NodusAdminHTTP

                self.admin = NodusAdminHTTP(self, section)
            except Exception as exc:
                log.error("Unable to start Nodus admin UI: %s", exc)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        try:
            connected = int(reason_code) == 0
        except (TypeError, ValueError):
            connected = reason_code == 0
        if connected:
            with self.lock:
                self.mqtt_connected = True
                self.mqtt_connected_at = int(time.time())
            client.subscribe(self.meta_topic, qos=0)
            for suffix in (
                "meta",
                "config/ack",
                "config/result",
                "calibration/ack",
                "calibration/result",
            ):
                client.subscribe("{}/{}".format(self.device_topic, suffix), qos=0)
        else:
            with self.lock:
                self.mqtt_connected = False
            log.error("Nodus switch status MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, _client, _userdata, reason_code, _properties=None):
        """Record broker disconnects for the setup information panes."""
        with self.lock:
            was_connected = self.mqtt_connected
            self.mqtt_connected = False
            self.last_disconnect_at = int(time.time())
            if was_connected or reason_code:
                self.disconnect_count += 1

    def _on_message(self, client, _userdata, message):
        try:
            with self.lock:
                self.messages_received += 1
                self.last_message_at = int(time.time())
                if message.topic == self.meta_topic:
                    for topic in self.controller.update_meta(message.payload):
                        client.subscribe(topic, qos=0)
                elif message.topic == "{}/meta".format(self.device_topic):
                    self.device_meta = json.loads(message.payload.decode("utf-8"))
                elif self._update_pending(message.topic, message.payload):
                    pass
                else:
                    self.controller.update_message(message.topic, message.payload)
                _write_status(self.status_file, self.controller.status())
        except Exception as exc:
            log.error("Nodus switch status MQTT message failed: %s", exc)

    def shutDown(self):
        """Stop the MQTT client during WeeWX shutdown."""
        self.manual_stop.set()
        if self.manual_thread is not None:
            self.manual_thread.join(timeout=5)
        if self.admin is not None:
            self.admin.shutdown()
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def _update_pending(self, topic, payload):
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return False
        message_id = str(data.get("message_id") or "")
        pending = self.pending.get(message_id)
        if not pending:
            return False
        if topic == pending["ack_topic"]:
            pending["ack"] = bool(data.get("accepted"))
        elif topic == pending["result_topic"]:
            pending["result"] = data
        else:
            return False
        if pending.get("ack") is False or (
            pending.get("ack") is True and pending.get("result") is not None
        ):
            pending["event"].set()
        return True

    def _request(self, family, payload):
        message_id = str(payload.get("message_id") or "")
        set_topic = "{}/{}/set".format(self.device_topic, family)
        ack_topic = "{}/{}/ack".format(self.device_topic, family)
        result_topic = "{}/{}/result".format(self.device_topic, family)
        pending = {
            "event": threading.Event(),
            "ack": None,
            "result": None,
            "ack_topic": ack_topic,
            "result_topic": result_topic,
        }
        with self.lock:
            self.pending[message_id] = pending
            result = self.client.publish(
                set_topic, json.dumps(payload, separators=(",", ":")), qos=0
            )
        if getattr(result, "rc", 1) != 0:
            with self.lock:
                self.pending.pop(message_id, None)
            raise ValueError("MQTT publish failed")
        pending["event"].wait(self.command_timeout)
        with self.lock:
            self.pending.pop(message_id, None)
        if pending["ack"] is not True:
            raise ValueError("Nodus command was not acknowledged")
        response = pending["result"]
        if not response or not response.get("applied"):
            error = (response or {}).get("error") or "Nodus command failed"
            raise ValueError(str(error))
        return response

    def _request_channel(self, channel, payload):
        """Publish a channel-scoped command and require its ack and result."""
        message_id = str(payload.get("message_id") or "")
        pending = {
            "event": threading.Event(),
            "ack": None,
            "result": None,
            "ack_topic": channel.get("ack_topic"),
            "result_topic": channel.get("result_topic"),
        }
        if not all(
            (channel.get("set_topic"), pending["ack_topic"], pending["result_topic"])
        ):
            raise ValueError("switch control topics are not available")
        with self.lock:
            self.pending[message_id] = pending
            result = self.client.publish(
                channel["set_topic"],
                json.dumps(payload, separators=(",", ":")),
                qos=0,
            )
        if getattr(result, "rc", 1) != 0:
            with self.lock:
                self.pending.pop(message_id, None)
            raise ValueError("MQTT publish failed")
        pending["event"].wait(self.command_timeout)
        with self.lock:
            self.pending.pop(message_id, None)
        if pending["ack"] is not True:
            raise ValueError("Nodus switch command was not acknowledged")
        response = pending["result"]
        if not response or not response.get("applied"):
            error = (response or {}).get("error") or "switch command failed"
            raise ValueError(str(error))
        return response

    def _save_manual_control(self):
        _write_control(
            self.control_file, self.manual_countdowns, self.manual_deadlines
        )
        self.controller.update_control_state(
            manual_countdowns=self.manual_countdowns,
            manual_deadlines=self.manual_deadlines,
        )
        _write_status(self.status_file, self.controller.status())

    def _set_manual_state(self, channel_id, desired, source="manual"):
        with self.lock:
            channel = self.controller.channels.get(channel_id)
            if not channel:
                raise ValueError("switch channel is not available")
            if channel_id in self.controller.automation_channels:
                raise ValueError("disable the switch automation before manual control")
            channel = dict(channel)
        payload = _switch_command_payload(
            channel, desired, self._message_id("{}-{}".format(source, channel_id))
        )
        self._request_channel(channel, payload)
        with self.lock:
            current = self.controller.channels.get(channel_id)
            if current:
                current["state"] = "ON" if desired else "OFF"
            if desired and self.manual_countdowns.get(channel_id, 0) > 0:
                self.manual_deadlines[channel_id] = int(time.time()) + int(
                    self.manual_countdowns[channel_id]
                )
            else:
                self.manual_deadlines.pop(channel_id, None)
            self._save_manual_control()
            return dict(self.controller.channels[channel_id])

    def _manual_countdown_loop(self):
        while not self.manual_stop.wait(1):
            now = int(time.time())
            with self.lock:
                due = [
                    channel_id
                    for channel_id, deadline in self.manual_deadlines.items()
                    if deadline <= now
                    and channel_id not in self.controller.automation_channels
                ]
            for channel_id in due:
                try:
                    self._set_manual_state(channel_id, False, "countdown")
                except Exception as exc:
                    log.warning(
                        "Manual countdown OFF failed channel=%s error=%s",
                        channel_id,
                        exc,
                    )

    def _message_id(self, prefix):
        token = re.sub(r"[^A-Za-z0-9-]+", "-", prefix).strip("-")
        return "weewx-admin-{}-{}".format(token or "update", int(time.time() * 1000))

    def admin_snapshot(self):
        """Return the limited setup state exposed by the admin page."""
        from user.nodus_admin import (
            calibration_fields,
            metric_fields,
            read_rules,
            rules_for_ui,
        )

        with self.lock:
            switch = self.controller.status()
            meta = dict(self.device_meta)
            mqtt_connected = bool(getattr(self, "mqtt_connected", False))
            info = {
                "service_started": int(getattr(self, "service_started", 0) or 0),
                "mqtt_connected_at": int(
                    getattr(self, "mqtt_connected_at", 0) or 0
                ),
                "last_disconnect_at": int(
                    getattr(self, "last_disconnect_at", 0) or 0
                ),
                "disconnect_count": int(
                    getattr(self, "disconnect_count", 0) or 0
                ),
                "last_packet_at": int(getattr(self, "last_message_at", 0) or 0),
                "packets_received": int(
                    getattr(self, "messages_received", 0) or 0
                ),
            }
        device_id = str(meta.get("device_id") or switch.get("device_id") or "")
        sensor = dict(meta.get("sensor") or {})
        network = dict(meta.get("network") or {})
        mqtt = dict(meta.get("mqtt") or {})
        info.update(
            {
                "board_type": str(meta.get("mcu") or ""),
                "firmware_version": str(meta.get("version") or ""),
                "sensor_device": str(sensor.get("device") or ""),
                "sensor_hardware": str(sensor.get("hardware") or ""),
                "ip_address": str(network.get("ipv4addr") or ""),
                "broker": str(
                    mqtt.get("active_broker")
                    or mqtt.get("broker")
                    or getattr(self, "mqtt_host", "")
                    or ""
                ),
                "broker_status": "Connected" if mqtt_connected else "Disconnected",
            }
        )
        try:
            rules = rules_for_ui(read_rules(self.rules_file))
        except Exception:
            rules = []
        metrics = _data_observations(self.config_dict)
        return {
            "device_id": device_id,
            "sensor": {
                "location": str(sensor.get("location") or ""),
                "calibrations": calibration_fields(device_id),
            },
            "switch": switch,
            "info": info,
            "metrics": metrics,
            "metric_options": metric_fields(metrics),
            "automations": rules,
        }

    def admin_update_sensor(self, body):
        """Apply a sensor location and selected calibration changes."""
        from user.nodus_admin import calibration_fields

        snapshot = self.admin_snapshot()
        location = str(body.get("location") or "").strip()
        if len(location) > 64:
            raise ValueError("sensor location is too long")
        if location != snapshot["sensor"]["location"]:
            message_id = self._message_id("sensor-location")
            payload = {
                "message_id": message_id,
                "payload": {
                    "updates": [
                        {"section": "Sensor", "key": "LOCATION", "value": location}
                    ]
                },
                "restart": False,
            }
            self._request("config", payload)
            with self.lock:
                self.device_meta.setdefault("sensor", {})["location"] = location

        allowed = {item["key"] for item in calibration_fields(snapshot["device_id"])}
        offsets = []
        for item in body.get("calibrations") or ():
            key = str(item.get("key") or "")
            if key not in allowed:
                raise ValueError("unsupported calibration field")
            offsets.append({"key": key, "value": float(item.get("value"))})
        if offsets:
            self._request(
                "calibration",
                {
                    "message_id": self._message_id("calibration"),
                    "action": "apply",
                    "payload": {"offsets": offsets},
                },
            )
        return {"ok": True, "message": "Sensor settings confirmed"}

    def admin_update_switch(self, body):
        """Apply switch location, labels, and host-side countdown settings."""
        location = str(body.get("location") or "").strip()
        if len(location) > 64:
            raise ValueError("switch location is too long")
        current_location = self.admin_snapshot()["switch"]["location"]
        with self.lock:
            channels = {
                item["channel_id"]: item for item in self.controller.channels.values()
            }
        label_changes = []
        for item in body.get("labels") or ():
            channel_id = str(item.get("channel_id") or "").strip()
            if channel_id not in channels:
                raise ValueError("switch label channel is invalid")
            label = str(item.get("label") or "").strip()
            if not label:
                raise ValueError("switch label is required")
            if len(label) > 64:
                raise ValueError("switch label is too long")
            channel = channels[channel_id]
            if label != str(channel.get("label") or ""):
                label_changes.append((channel_id, channel, label))
        if location != current_location:
            payload = {
                "message_id": self._message_id("switch-location"),
                "payload": {
                    "updates": [
                        {
                            "section": "Switch",
                            "key": "SWITCH_LOCATION",
                            "value": location,
                        }
                    ]
                },
                "restart": False,
            }
            self._request("config", payload)
        for _channel_id, channel, label in label_changes:
            self._request(
                "config",
                {
                    "message_id": self._message_id("switch-label"),
                    "payload": {
                        "updates": [
                            {
                                "section": "Switch",
                                "key": "SWITCH_{}_LABEL".format(channel["index"]),
                                "value": label,
                            }
                        ]
                    },
                    "restart": False,
                },
            )
        with self.lock:
            self.controller.location = location
            for channel_id, _channel, label in label_changes:
                self.controller.channels[channel_id]["label"] = label
            channel_ids = set(self.controller.channels)
            for item in body.get("manual_controls") or ():
                channel_id = str(item.get("channel_id") or "").strip()
                if channel_id not in channel_ids:
                    raise ValueError("manual control channel is invalid")
                seconds = int(item.get("countdown_seconds") or 0)
                if not 0 <= seconds <= 86400:
                    raise ValueError("manual countdown must be 0-86400 seconds")
                self.manual_countdowns[channel_id] = seconds
                if not seconds:
                    self.manual_deadlines.pop(channel_id, None)
            self._save_manual_control()
        return {"ok": True, "message": "Switch settings saved"}

    def admin_toggle_switch(self, body):
        """Toggle a manually owned switch with a five-second command guard."""
        channel_id = str(body.get("channel_id") or "").strip()
        now = time.monotonic()
        with self.lock:
            channel = self.controller.channels.get(channel_id)
            if not channel:
                raise ValueError("switch channel is not available")
            if channel_id in self.controller.automation_channels:
                raise ValueError("disable the switch automation before manual control")
            if now < self.manual_guard_until.get(channel_id, 0):
                remaining = max(
                    1, int(self.manual_guard_until[channel_id] - now + 0.999)
                )
                raise ValueError(
                    "wait {} seconds before toggling again".format(remaining)
                )
            self.manual_guard_until[channel_id] = now + _MANUAL_GUARD_SECONDS
            desired = channel.get("state") != "ON"
        try:
            channel = self._set_manual_state(channel_id, desired)
        except Exception:
            with self.lock:
                self.manual_guard_until.pop(channel_id, None)
            raise
        return {
            "ok": True,
            "channel": channel,
            "guard_seconds": _MANUAL_GUARD_SECONDS,
        }

    def admin_update_automations(self, body):
        """Validate and atomically replace host-side automations."""
        from user.nodus_admin import validate_rules, write_rules

        snapshot = self.admin_snapshot()
        channels = [item["channel_id"] for item in snapshot["switch"]["channels"]]
        document = validate_rules(
            body.get("automations"), channels, snapshot["metrics"]
        )
        write_rules(self.rules_file, document)
        automation_owners = {
            action["channel_id"]: name
            for name, rule in document["rules"].items()
            if rule.get("enabled", True)
            for action in rule.get("actions", ())
        }
        with self.lock:
            for channel_id in automation_owners:
                self.manual_deadlines.pop(channel_id, None)
            self.controller.update_control_state(
                automation_owners=automation_owners,
                manual_countdowns=self.manual_countdowns,
                manual_deadlines=self.manual_deadlines,
            )
            self._save_manual_control()
        return {"ok": True, "message": "Automations saved"}


class NodusSwitchStatusSearchList(SearchList):
    """Expose cached switch status to Nodus Cheetah templates."""

    def __init__(self, generator):
        super().__init__(generator)
        section = generator.skin_dict.get("NodusSwitchStatus", {})
        path = str(section.get("status_file") or _DEFAULT_STATUS_FILE)
        try:
            self.nodus_switch = _skin_status(_read_status(path))
        except Exception:
            self.nodus_switch = {
                "device_id": "",
                "switch_device_id": "",
                "location": "",
                "updated": 0,
                "channels": [],
            }
