"""Run threshold-and-time Nodus switch automations from WeeWX.

The service evaluates configured rules against archive data, publishes switch
commands, and maintains bounded status for the web skin and device dashboards.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

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
            """Accept event bindings when WeeWX is unavailable during tests."""
            return None

    class SearchList:
        """Minimal test fallback for the WeeWX SearchList base class."""

        def __init__(self, generator):
            self.generator = generator


log = logging.getLogger(__name__)

_DEFAULT_STATUS_FILE = "/var/lib/weewx/nodus_automation.json"
_DEFAULT_RULES_FILE = "/var/lib/weewx/nodus_automation_rules.json"
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


def _parse_advanced_condition(source, name):
    """Build one runtime condition from the version-two JSON schema."""
    kind = str(source.get("type") or "metric").strip().lower()
    if kind == "or":
        return {"type": "or", "name": name}
    if kind == "metric":
        parsed = _parse_condition(
            "{}, {}, {}, {}".format(
                source.get("metric") or "",
                source.get("direction") or "above",
                source.get("on_threshold"),
                source.get("off_threshold"),
            ),
            name,
        )
        parsed["type"] = "metric"
        return parsed
    if kind == "time":
        days = [item[:3].lower() for item in _as_list(source.get("days"))]
        return {
            "type": "time",
            "name": name,
            "start_minute": _parse_clock(source.get("start") or "00:00"),
            "end_minute": _parse_clock(source.get("end") or "00:00"),
            "days": days or list(_DAY_NAMES),
        }
    if kind == "timer":
        duration = int(source.get("duration_minutes") or 0)
        period = int(source.get("period_minutes") or 0)
        if duration < 1 or period < 2 or duration >= period:
            raise ValueError("{} has an invalid timer window".format(name))
        return {
            "type": "timer",
            "name": name,
            "duration_seconds": duration * 60,
            "period_seconds": period * 60,
            "anchor_epoch": max(0, int(source.get("anchor_epoch") or 0)),
        }
    if kind == "astral":
        return {
            "type": "astral",
            "name": name,
            "event": str(source.get("event") or "sunrise").strip().lower(),
            "offset_minutes": int(source.get("offset_minutes") or 0),
            "days": [
                item[:3].lower() for item in _as_list(source.get("days"))
            ]
            or list(_DAY_NAMES),
        }
    if kind == "switch":
        return {
            "type": "switch",
            "name": name,
            "channel_id": str(source.get("channel_id") or "").strip(),
            "state": str(source.get("state") or "on").strip().lower() == "on",
        }
    raise ValueError("{} has unsupported type {}".format(name, kind))


def _parse_advanced_action(source):
    """Build one runtime switch action from the version-two JSON schema."""
    return {
        "channel_id": str(source.get("channel_id") or "").strip(),
        "state": str(source.get("state") or "on").strip().lower() == "on",
        "false_action": str(source.get("false_action") or "opposite")
        .strip()
        .lower(),
        "delay_seconds": max(0, int(source.get("delay_seconds") or 0)),
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
        if source.get("conditions") is not None and source.get("actions") is not None:
            conditions = [
                _parse_advanced_condition(item, "condition_{}".format(index))
                for index, item in enumerate(source.get("conditions") or (), 1)
            ]
            actions = [
                _parse_advanced_action(item) for item in source.get("actions") or ()
            ]
            if not conditions or not actions:
                raise ValueError(
                    "rule {} has no conditions or actions".format(rule_name)
                )
            action_channels = [item["channel_id"] for item in actions]
            if any(not channel_id for channel_id in action_channels):
                raise ValueError("rule {} has an invalid action".format(rule_name))
            if any(channel_id in used_channels for channel_id in action_channels):
                raise ValueError(
                    "more than one enabled rule targets {}".format(
                        ", ".join(action_channels)
                    )
                )
            used_channels.update(action_channels)
            rules.append(
                {
                    "name": str(rule_name),
                    "conditions": conditions,
                    "actions": actions,
                    "stale_action": str(source.get("stale_action") or "off")
                    .strip()
                    .lower(),
                    "stale_after": int(
                        source.get("stale_after") or section.get("stale_after") or 180
                    ),
                    "minimum_on": int(source.get("minimum_on_seconds") or 0),
                    "minimum_off": int(source.get("minimum_off_seconds") or 0),
                    "retry_seconds": int(source.get("retry_seconds") or 60),
                    "last_decision": "waiting",
                    "last_action": "",
                    "last_error": "",
                }
            )
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
                "actions": [
                    {
                        "channel_id": channel_id,
                        "state": True,
                        "false_action": "off"
                        if outside == "off"
                        else "hold",
                        "delay_seconds": 0,
                    }
                ],
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


def _disabled_rules(section):
    """Return display-only definitions for saved disabled rules."""
    rows = []
    rules_section = section.get("rules", {})
    for rule_name in _section_names(rules_section):
        source = rules_section[rule_name]
        if _to_bool(source.get("enabled"), True):
            continue
        actions = source.get("actions") or ()
        if isinstance(actions, dict):
            actions = actions.values()
        channel_ids = [
            str(action.get("channel_id") or "").strip()
            for action in actions
            if str(action.get("channel_id") or "").strip()
        ]
        legacy_channel = str(source.get("channel_id") or "").strip()
        if legacy_channel and legacy_channel not in channel_ids:
            channel_ids.append(legacy_channel)
        rows.append({"name": str(rule_name), "channel_ids": channel_ids})
    return rows


def _rules_source(section):
    """Load optional UI-managed rules without exposing the WeeWX config."""
    path = str(section.get("rules_file") or "").strip()
    if not path:
        return section
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"rules": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict):
        raise ValueError("Nodus automation rules file is invalid")
    source = dict(section)
    source["rules"] = data["rules"]
    return source


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


def _automation_contract_topics(meta_topic, controller="weewx"):
    """Derive retained automation status topics from switch metadata."""
    suffix = "/meta/switch"
    text = str(meta_topic or "").rstrip("/")
    if not text.endswith(suffix):
        raise ValueError("automation meta topic must end with /meta/switch")
    base = "{}/automation/{}".format(text[: -len(suffix)], controller)
    return base + "/status", base + "/availability"


def _automation_contract_status(rules, enabled=True, now=None):
    """Build the retained channel ownership document for Nodus."""
    channels = {}
    if enabled:
        for rule in rules or ():
            name = str(rule.get("name") or "").strip()
            for action in rule.get("actions") or ():
                channel_id = str(action.get("channel_id") or "").strip()
                if channel_id and name:
                    channels.setdefault(channel_id, []).append(name)
    return {
        "schema": "nodus-automation-status/v1",
        "controller": "weewx",
        "controller_id": "weewx-nodus-automation",
        "updated_at": int(now if now is not None else time.time()),
        "channels": [
            {
                "channel_id": channel_id,
                "automations": names,
                "enabled": True,
            }
            for channel_id, names in sorted(channels.items())
        ],
    }


def _automation_contract_availability(status, now=None):
    """Build the retained WeeWX controller availability document."""
    return {
        "schema": "nodus-automation-availability/v1",
        "controller": "weewx",
        "controller_id": "weewx-nodus-automation",
        "status": str(status or "offline").lower(),
        "updated_at": int(now if now is not None else time.time()),
    }


class AutomationController:
    """Evaluate advanced rules and confirm correlated Nodus commands."""

    def __init__(
        self,
        rules,
        publisher,
        command_timeout=15,
        latitude=None,
        longitude=None,
        runtime_file="",
    ):
        self.rules = rules
        self.publisher = publisher
        self.command_timeout = max(1, int(command_timeout))
        self.latitude = float(latitude) if latitude is not None else None
        self.longitude = float(longitude) if longitude is not None else None
        self.runtime_file = str(runtime_file or "").strip()
        self.channels = {}
        self.observations = {}
        self.pending = {}
        self.delays = {}
        self.retry_not_before = {}
        self.active_actions = self._load_runtime()
        self.sequence = 0

    def _load_runtime(self):
        if not self.runtime_file:
            return {}
        try:
            with open(self.runtime_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            active = data.get("active_actions") or {}
            return active if isinstance(active, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_runtime(self):
        if not self.runtime_file:
            return
        temporary = "{}.tmp.{}".format(self.runtime_file, os.getpid())
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(
                    {"active_actions": self.active_actions},
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                handle.write("\n")
            os.replace(temporary, self.runtime_file)
        except OSError as exc:
            log.warning("Unable to save Nodus automation runtime state: %s", exc)

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
        """Evaluate all enabled rules against observations, time, and state."""
        now = float(now if now is not None else time.time())
        packet = packet or {}
        sample_time = float(packet.get("dateTime") or now)
        for name, value in packet.items():
            if name != "dateTime" and value is not None:
                self.observations[name] = (value, sample_time)
        self._expire_pending(now)
        self._cleanup_inactive_actions(now)
        for rule in self.rules:
            self._evaluate_rule(rule, now)

    def _cleanup_inactive_actions(self, now):
        """Restore prior state when a previous-state action is removed."""
        current_keys = set()
        current_channels = set()
        for rule in self.rules:
            for action in rule["actions"]:
                current_channels.add(action["channel_id"])
                if action.get("false_action") == "previous_state":
                    current_keys.add(self._action_key(rule, action))
        for key, active in list(self.active_actions.items()):
            if key in current_keys:
                continue
            channel_id = str(active.get("channel_id") or "")
            if channel_id in current_channels:
                self.active_actions.pop(key, None)
                self._save_runtime()
                continue
            channel = self.channels.get(channel_id)
            if not channel or "state" not in channel:
                continue
            desired = bool(active.get("previous"))
            if channel["state"] == desired:
                self.active_actions.pop(key, None)
                self._save_runtime()
                continue
            if any(
                pending["channel_id"] == channel_id
                for pending in self.pending.values()
            ):
                continue
            if now < self.retry_not_before.get(channel_id, 0):
                continue
            cleanup_rule = {
                "name": key.split("|", 1)[0] or "removed-rule",
                "retry_seconds": 60,
                "last_action": "",
                "last_error": "",
            }
            self._send(
                cleanup_rule,
                channel,
                desired,
                now,
                release_key=key,
            )

    @staticmethod
    def _groups(conditions):
        groups = []
        current = []
        for condition in conditions:
            if condition.get("type") == "or":
                if current:
                    groups.append(current)
                    current = []
                continue
            current.append(condition)
        if current:
            groups.append(current)
        return groups

    def _astral_result(self, condition, now):
        if self.latitude is None or self.longitude is None:
            return None
        local = datetime.fromtimestamp(now).astimezone()
        if _DAY_NAMES[local.weekday()] not in condition.get("days", _DAY_NAMES):
            return False
        try:
            from astral import Observer
            from astral.sun import sun

            events = sun(
                Observer(latitude=self.latitude, longitude=self.longitude),
                date=local.date(),
                tzinfo=local.tzinfo,
            )
            offset = timedelta(minutes=condition.get("offset_minutes", 0))
            sunrise = events["sunrise"] + offset
            sunset = events["sunset"] + offset
        except Exception as exc:
            log.warning("Nodus automation Astral calculation failed: %s", exc)
            return None
        event = condition.get("event")
        if event == "sunrise_to_sunset":
            return sunrise <= local < sunset
        if event == "sunset_to_sunrise":
            return local >= sunset or local < sunrise
        if event == "sunrise":
            return local >= sunrise
        if event == "sunset":
            return local >= sunset
        return None

    def _condition_result(self, condition, rule, now):
        kind = condition.get("type", "metric")
        if kind == "metric":
            reading = self.observations.get(condition["observation"])
            if not reading or now - reading[1] > rule.get("stale_after", 180):
                return None
            condition["active"] = _condition_active(
                condition, reading[0], condition.get("active", False)
            )
            return condition["active"]
        if kind == "time":
            return _time_window_active(
                now,
                condition["start_minute"],
                condition["end_minute"],
                condition.get("days"),
            )
        if kind == "timer":
            period = condition["period_seconds"]
            anchor = condition.get("anchor_epoch", 0)
            if anchor:
                phase = max(0, int(now) - anchor) % period
            else:
                local = time.localtime(now)
                elapsed = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
                phase = elapsed % period
            return phase < condition["duration_seconds"]
        if kind == "astral":
            return self._astral_result(condition, now)
        if kind == "switch":
            channel = self.channels.get(condition["channel_id"])
            if not channel or "state" not in channel:
                return None
            return bool(channel["state"]) == condition["state"]
        return None

    def _rule_result(self, rule, now):
        conditions = list(rule["conditions"])
        if "start_minute" in rule:
            conditions.append(
                {
                    "type": "time",
                    "start_minute": rule["start_minute"],
                    "end_minute": rule["end_minute"],
                    "days": rule["days"],
                }
            )
        group_results = []
        for group in self._groups(conditions):
            results = [self._condition_result(item, rule, now) for item in group]
            if any(result is False for result in results):
                group_results.append(False)
            elif any(result is None for result in results):
                group_results.append(None)
            else:
                group_results.append(True)
        if any(result is True for result in group_results):
            return True
        if group_results and all(result is False for result in group_results):
            return False
        return None

    @staticmethod
    def _action_key(rule, action):
        return "{}|{}".format(rule["name"], action["channel_id"])

    def _false_desired(self, rule, action):
        behavior = action.get("false_action", "opposite")
        key = self._action_key(rule, action)
        if behavior == "opposite":
            return not action["state"]
        if behavior == "on":
            return True
        if behavior == "off":
            return False
        if behavior == "previous_state":
            active = self.active_actions.get(key)
            return None if active is None else bool(active.get("previous"))
        return None

    def _evaluate_rule(self, rule, now):
        result = self._rule_result(rule, now)
        if result is True:
            rule["last_decision"] = "condition groups active"
        elif result is False:
            rule["last_decision"] = "condition groups idle"
        else:
            stale_metric = any(
                condition.get("type", "metric") == "metric"
                and (
                    condition.get("observation") not in self.observations
                    or now
                    - self.observations[condition["observation"]][1]
                    > rule.get("stale_after", 180)
                )
                for condition in rule["conditions"]
            )
            rule["last_decision"] = (
                "sensor data stale" if stale_metric else "condition state unknown"
            )
        for action in rule["actions"]:
            self._evaluate_action(rule, action, result, now)

    def _evaluate_action(self, rule, action, result, now):
        channel = self.channels.get(action["channel_id"])
        if not channel or "state" not in channel:
            rule["last_decision"] = "waiting for switch metadata"
            return
        if any(
            pending["channel_id"] == action["channel_id"]
            for pending in self.pending.values()
        ):
            rule["last_decision"] += "; waiting for command result"
            return
        key = self._action_key(rule, action)
        if result is True:
            desired = action["state"]
            delay = action.get("delay_seconds", 0)
            if delay:
                due = self.delays.setdefault(key, now + delay)
                if now < due:
                    rule["last_decision"] += "; action delay"
                    return
            if (
                action.get("false_action") == "previous_state"
                and key not in self.active_actions
                and channel["state"] != desired
            ):
                self.active_actions[key] = {
                    "channel_id": action["channel_id"],
                    "previous": bool(channel["state"]),
                }
                self._save_runtime()
        elif result is False:
            self.delays.pop(key, None)
            desired = self._false_desired(rule, action)
        else:
            self.delays.pop(key, None)
            desired = False if rule.get("stale_action") == "off" else None
        if desired is None:
            return
        if channel["state"] == desired:
            if result is not True and key in self.active_actions:
                self.active_actions.pop(key, None)
                self._save_runtime()
            return
        if now < self.retry_not_before.get(action["channel_id"], 0):
            rule["last_decision"] += "; retry cooldown"
            return
        dwell = rule["minimum_on"] if channel["state"] else rule["minimum_off"]
        if now - channel.get("last_changed", now) < dwell:
            rule["last_decision"] += "; minimum dwell"
            return
        release_key = key if result is not True and key in self.active_actions else ""
        self._send(rule, channel, desired, now, release_key=release_key)

    def _send(self, rule, channel, desired, now, release_key=""):
        self.sequence += 1
        token = re.sub(r"[^A-Za-z0-9_.-]+", "-", rule["name"]).strip("-")
        message_id = "weewx-{}-{}-{}".format(token or "rule", int(now), self.sequence)
        payload = _command_payload(channel, desired, message_id)
        published = self.publisher(
            channel["set_topic"],
            json.dumps(payload, separators=(",", ":")),
            False,
        )
        retry_at = now + max(1, rule["retry_seconds"])
        self.retry_not_before[channel["channel_id"]] = retry_at
        if published is False:
            rule["last_error"] = "MQTT publish failed"
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
            "release_key": release_key,
        }
        rule["last_action"] = "requested {} on {}".format(
            "ON" if desired else "OFF", channel["channel_id"]
        )
        rule["last_error"] = ""

    def _finish_completed(self, now):
        for message_id, pending in list(self.pending.items()):
            if pending["ack"] and pending["result"] and pending["state_seen"]:
                rule = pending["rule"]
                rule["last_action"] = "confirmed {} at {} on {}".format(
                    "ON" if pending["desired"] else "OFF",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    pending["channel_id"],
                )
                rule["last_error"] = ""
                release_key = pending.get("release_key")
                if release_key:
                    self.active_actions.pop(release_key, None)
                    self._save_runtime()
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
            actions = []
            for action in rule["actions"]:
                channel = self.channels.get(action["channel_id"], {})
                actions.append(
                    {
                        "channel_id": action["channel_id"],
                        "label": channel.get("label") or action["channel_id"],
                        "state": "ON" if channel.get("state") else "OFF"
                        if "state" in channel
                        else "unknown",
                    }
                )
            first = actions[0] if actions else {}
            rows.append(
                {
                    "name": rule["name"],
                    "enabled": True,
                    "enable_state": "Enabled",
                    "channel_id": first.get("channel_id", ""),
                    "label": first.get("label", ""),
                    "state": first.get("state", "unknown"),
                    "actions": actions,
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


def _skin_status(status):
    """Normalize older automation snapshots for the current skin."""
    result = dict(status)
    rules = []
    for source in status.get("rules") or ():
        rule = dict(source)
        enabled = _to_bool(rule.get("enabled"), True)
        rule["enabled"] = enabled
        rule["enable_state"] = "Enabled" if enabled else "Disabled"
        rules.append(rule)
    result["rules"] = rules
    return result


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
        self.runtime_file = str(section.get("runtime_file") or "").strip()
        self.rules_file = str(section.get("rules_file") or "").strip()
        self.rules_mtime = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_status = ""
        self.client = None
        self.disabled_rules = []
        self.rule_order = []
        self.controller = AutomationController([], lambda *_args: False)
        if not self.enabled and not self.rules_file:
            self._save_status()
            return

        rules_source = _rules_source(section)
        rules = _parse_rules(rules_source)
        self.disabled_rules = _disabled_rules(rules_source)
        self.rule_order = _section_names(rules_source.get("rules", {}))
        if self.enabled and not self.rules_file and not rules:
            raise ValueError("NodusAutomation is enabled but has no enabled rules")
        mqtt_section = _mqtt_section(config_dict)
        self.meta_topic = _derive_meta_topic(config_dict, section.get("meta_topic"))
        (
            self.automation_status_topic,
            self.automation_availability_topic,
        ) = _automation_contract_topics(self.meta_topic)
        self.last_contract_status = ""
        self.last_availability_publish = 0
        self.controller = AutomationController(
            rules,
            self._publish,
            section.get("command_timeout") or 15,
            config_dict.get("Station", {}).get("latitude"),
            config_dict.get("Station", {}).get("longitude"),
            self.runtime_file,
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
        self.client.will_set(
            self.automation_availability_topic,
            json.dumps(
                _automation_contract_availability("offline"),
                separators=(",", ":"),
            ),
            qos=0,
            retain=True,
        )
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
        self._remember_rules_mtime()
        self._save_status()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        try:
            connected = int(reason_code) == 0
        except (TypeError, ValueError):
            connected = reason_code == 0
        if connected:
            client.subscribe(self.meta_topic, qos=0)
            self._publish_contract_availability("online")
            self._publish_contract_status(force=True)
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
                self._reload_rules()
                self.controller.evaluate()
                if time.time() - self.last_availability_publish >= 60:
                    self._publish_contract_availability("online")
                self._save_status()

    def _remember_rules_mtime(self):
        if not self.rules_file:
            return
        try:
            self.rules_mtime = os.path.getmtime(self.rules_file)
        except OSError:
            self.rules_mtime = None

    def _reload_rules(self):
        """Reload UI-managed rules after an atomic file replacement."""
        if not self.rules_file:
            return
        try:
            modified = os.path.getmtime(self.rules_file)
        except OSError:
            modified = None
        if modified == self.rules_mtime:
            return
        try:
            rules_source = _rules_source({"rules_file": self.rules_file})
            rules = _parse_rules(rules_source)
            disabled_rules = _disabled_rules(rules_source)
        except Exception as exc:
            log.error("Unable to reload Nodus automation rules: %s", exc)
            self.rules_mtime = modified
            return
        self.controller.rules = rules
        self.disabled_rules = disabled_rules
        self.rule_order = _section_names(rules_source.get("rules", {}))
        self.rules_mtime = modified
        log.info("Reloaded %d Nodus automation rule(s)", len(rules))
        self._publish_contract_status()

    def _publish_contract_status(self, force=False):
        """Publish retained enabled rule ownership when it changes."""
        document = _automation_contract_status(
            self.controller.rules, enabled=self.enabled
        )
        comparison = dict(document)
        comparison["updated_at"] = 0
        marker = json.dumps(comparison, separators=(",", ":"), sort_keys=True)
        if not force and marker == self.last_contract_status:
            return True
        result = self.client.publish(
            self.automation_status_topic,
            json.dumps(document, separators=(",", ":")),
            qos=0,
            retain=True,
        )
        if getattr(result, "rc", 1) == 0:
            self.last_contract_status = marker
            return True
        return False

    def _publish_contract_availability(self, status):
        """Publish retained controller liveness for Nodus presentation."""
        result = self.client.publish(
            self.automation_availability_topic,
            json.dumps(
                _automation_contract_availability(status), separators=(",", ":")
            ),
            qos=0,
            retain=True,
        )
        if getattr(result, "rc", 1) == 0:
            self.last_availability_publish = time.time()
            return True
        return False

    def _save_status(self):
        status = self.controller.status(enabled=self.enabled)
        for rule in self.disabled_rules:
            actions = []
            for channel_id in rule["channel_ids"]:
                channel = self.controller.channels.get(channel_id, {})
                actions.append(
                    {
                        "channel_id": channel_id,
                        "label": channel.get("label") or channel_id,
                        "state": "ON"
                        if channel.get("state")
                        else "OFF"
                        if "state" in channel
                        else "unknown",
                    }
                )
            first = actions[0] if actions else {}
            status["rules"].append(
                {
                    "name": rule["name"],
                    "enabled": False,
                    "enable_state": "Disabled",
                    "channel_id": first.get("channel_id", ""),
                    "label": first.get("label", ""),
                    "state": first.get("state", "unknown"),
                    "actions": actions,
                    "decision": "disabled",
                    "last_action": "none",
                    "error": "",
                }
            )
        order = {name: index for index, name in enumerate(self.rule_order)}
        status["rules"].sort(
            key=lambda rule: order.get(rule["name"], len(order))
        )
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
            self._publish_contract_availability("offline")
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
                self.nodus_automation = _skin_status(json.load(handle))
        except Exception:
            self.nodus_automation = {"enabled": False, "updated": 0, "rules": []}
