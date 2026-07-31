"""Run bounded local switch automations for the NodusWeb profile.

``NodusWebAutomationService`` loads the persisted rule set, evaluates eligible
conditions, and applies switch actions while tracking ownership. Automation is
profile-gated and intentionally capped for predictable device resource use.
"""

import json
import os
import time

from cpynodus_ii.core import toml_compat

AUTOMATIONS_FILE = "automations.toml"
AUTOMATION_STATE_FILE = ".nodus_automation_state.json"
EVALUATION_INTERVAL_S = 5.0
MAX_RULES = 12
MAX_CONDITIONS = 12
MAX_ACTIONS = 4
MAX_SCRIPT_BYTES = 1536
_VALID_CONDITION_TYPES = ("sensor", "time", "timer", "or")
_VALID_OPERATORS = (">", ">=", "<", "<=", "==", "!=")


def nodusweb_automations_enabled(runtime_config):
    """Return whether local automation belongs to this exact runtime."""
    return bool(
        getattr(runtime_config, "active_profile", "") == "nodusweb"
        and getattr(getattr(runtime_config, "switch", None), "present", False)
    )


class NodusWebAutomationService:
    """Load, validate, evaluate, and persist local automation rules."""

    def __init__(self, runtime_config, switch_service, *, settings_root=None):
        self.runtime_config = runtime_config
        self.switch_service = switch_service
        self.settings_root = settings_root
        self.rules = []
        self.errors = ()
        self.next_evaluation_at = 0.0
        self.latest_metrics = {}
        self._condition_states = {}
        self._active_actions = {}
        self._load()
        self._load_runtime_state()

    @property
    def active(self):
        """Return whether this service may execute rules."""
        return nodusweb_automations_enabled(self.runtime_config)

    def update_context(
        self, *, runtime_config=None, switch_service=None, metrics=None
    ):
        """Refresh mutable services and the most recent sampled metrics."""
        if runtime_config is not None:
            self.runtime_config = runtime_config
        if switch_service is not None:
            self.switch_service = switch_service
        if metrics is not None:
            self.latest_metrics = dict(metrics or {})

    def payload(self):
        """Return the bounded web representation and local target catalogs."""
        metrics = list(self.latest_metrics.keys())
        for metric in getattr(self.runtime_config.sensor.display, "metrics", ()):
            if metric and metric not in metrics:
                metrics.append(metric)
        metrics.sort(key=lambda item: str(item).lower())
        channels = []
        for channel in self.runtime_config.switch.channels:
            channels.append(
                {
                    "key": channel.key,
                    "channel_id": channel.channel_id,
                    "label": channel.label,
                    "switch_key": "{}::{}".format(
                        self.runtime_config.switch.device_id, channel.channel_id
                    ),
                }
            )
        rules = []
        for rule in self.rules:
            rules.append(
                {
                    "rule_id": rule["rule_id"],
                    "enabled": bool(rule["enabled"]),
                    "script": rule["script"],
                }
            )
        return {
            "success": True,
            "active": self.active,
            "profile": self.runtime_config.active_profile,
            "sensor_id": self.runtime_config.sensor.sensor_id,
            "metrics": metrics,
            "channels": channels,
            "condition_types": list(_VALID_CONDITION_TYPES),
            "limits": {
                "rules": MAX_RULES,
                "conditions": MAX_CONDITIONS,
                "actions": MAX_ACTIONS,
            },
            "rules": rules,
            "errors": list(self.errors),
        }

    def save_rule(self, rule_id, enabled, script):
        """Validate and transactionally save one compatible rule."""
        if not self.active:
            return _error_payload("automation_profile_inactive")
        try:
            normalized_id = _normalize_rule_id(rule_id, script, self.rules)
            normalized = _normalize_script(
                script, self.runtime_config, now_epoch=_epoch_time()
            )
        except ValueError as exc:
            return _error_payload(str(exc))
        enabled = bool(enabled)
        normalized["enabled"] = enabled
        replacement = {
            "rule_id": normalized_id,
            "enabled": enabled,
            "script": normalized,
        }
        candidate = []
        found = False
        for current in self.rules:
            if current["rule_id"] == normalized_id:
                candidate.append(replacement)
                found = True
            else:
                candidate.append(current)
        if not found:
            if len(candidate) >= MAX_RULES:
                return _error_payload("automation_rule_limit")
            candidate.append(replacement)
        error = self._persist_rules(candidate)
        if error:
            return _error_payload(error)
        if found:
            self._release_rule(normalized_id)
        self.rules = candidate
        self._clear_rule_runtime(normalized_id)
        return {"success": True, "rule_id": normalized_id, "errors": []}

    def delete_rule(self, rule_id):
        """Transactionally delete one rule and release owned actions."""
        if not self.active:
            return _error_payload("automation_profile_inactive")
        rule_id = str(rule_id or "").strip()
        if not any(rule["rule_id"] == rule_id for rule in self.rules):
            return _error_payload("automation_rule_not_found")
        candidate = [rule for rule in self.rules if rule["rule_id"] != rule_id]
        error = self._persist_rules(candidate)
        if error:
            return _error_payload(error)
        self._release_rule(rule_id)
        self.rules = candidate
        return {"success": True, "rule_id": rule_id, "errors": []}

    def channel_controlled(self, channel_id=None, channel_key=None):
        """Return enabled rules that own a local channel."""
        channel = self._find_channel(channel_id=channel_id, channel_key=channel_key)
        if channel is None:
            return ()
        canonical = "{}::{}".format(
            self.runtime_config.switch.device_id, channel.channel_id
        )
        owners = []
        for rule in self.rules:
            if not rule["enabled"]:
                continue
            for action in rule["script"].get("actions", ()):
                if action.get("switch_key") == canonical:
                    owners.append(rule["script"].get("name") or rule["rule_id"])
                    break
        return tuple(owners)

    def tick(self, *, now_monotonic, now_epoch=None, metrics=None):
        """Evaluate at most once per cadence without polling sensor hardware."""
        if metrics is not None:
            self.latest_metrics = dict(metrics or {})
        if not self.active or float(now_monotonic) < self.next_evaluation_at:
            return ()
        self.next_evaluation_at = float(now_monotonic) + EVALUATION_INTERVAL_S
        now_epoch = int(now_epoch if now_epoch is not None else _epoch_time())
        errors = []
        for rule in self.rules:
            try:
                matched = bool(rule["enabled"]) and self._rule_matches(
                    rule, now_epoch
                )
                self._apply_rule(rule, matched, float(now_monotonic))
            except Exception as exc:
                errors.append("{}:{}".format(rule["rule_id"], type(exc).__name__))
        self.errors = tuple(errors)
        return self.errors

    def _rule_matches(self, rule, now_epoch):
        groups = [[]]
        for index, condition in enumerate(rule["script"].get("conditions", ())):
            if condition.get("type") == "or":
                if groups[-1]:
                    groups.append([])
                continue
            groups[-1].append(
                self._condition_matches(rule["rule_id"], index, condition, now_epoch)
            )
        return any(group and all(group) for group in groups)

    def _condition_matches(self, rule_id, index, condition, now_epoch):
        kind = condition.get("type")
        if kind == "sensor":
            return self._sensor_matches(rule_id, index, condition)
        if kind == "time":
            return _clock_ready(now_epoch) and _time_matches(condition, now_epoch)
        if kind == "timer":
            if not _clock_ready(now_epoch):
                return False
            period_s = int(condition["period_min"]) * 60
            duration_s = int(condition["duration_min"]) * 60
            anchor = int(condition.get("anchor_epoch") or 0)
            return ((now_epoch - anchor) % period_s) < duration_s
        return False

    def _sensor_matches(self, rule_id, index, condition):
        value = self.latest_metrics.get(condition.get("metric"))
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        threshold = float(condition["value"])
        hyst = max(0.0, float(condition.get("hyst") or 0.0))
        token = "{}:{}".format(rule_id, index)
        previous = bool(self._condition_states.get(token, False))
        operator = condition["op"]
        compare_threshold = threshold
        if previous and hyst:
            if operator in (">", ">="):
                compare_threshold -= hyst
            elif operator in ("<", "<="):
                compare_threshold += hyst
        matched = _compare(value, operator, compare_threshold)
        self._condition_states[token] = matched
        return matched

    def _apply_rule(self, rule, matched, now_monotonic):
        rule_id = rule["rule_id"]
        for index, action in enumerate(rule["script"].get("actions", ())):
            token = "{}:{}".format(rule_id, index)
            active = self._active_actions.get(token)
            if matched:
                if active is None:
                    active = {
                        "due": now_monotonic + int(action.get("delay_s") or 0),
                        "applied": False,
                        "revert_to": None,
                    }
                    self._active_actions[token] = active
                if not active["applied"] and now_monotonic >= active["due"]:
                    current = self._channel_state(action["switch_key"])
                    desired = bool(action["set"])
                    if current is None:
                        continue
                    if current != desired:
                        if not self._apply_action(action["switch_key"], desired):
                            continue
                        active["revert_to"] = current
                    active["applied"] = True
                    self._persist_runtime_state()
            elif active is not None:
                released = True
                if (
                    active.get("applied")
                    and action.get("revert_action") == "previous_state"
                    and active.get("revert_to") is not None
                ):
                    released = self._apply_action(
                        action["switch_key"], active["revert_to"]
                    )
                if released:
                    self._active_actions.pop(token, None)
                    self._persist_runtime_state()

    def _apply_action(self, switch_key, state):
        channel = self._channel_for_switch_key(switch_key)
        if channel is None:
            return False
        from cpynodus_ii.features.web_handlers import handle_switch_state_request

        payload = handle_switch_state_request(
            self.runtime_config,
            self.switch_service,
            channel_id=channel.channel_id,
            state=bool(state),
            settings_root=self.settings_root,
        )
        self.runtime_config = payload.pop("runtime_config")
        return bool(payload.get("success"))

    def _channel_state(self, switch_key):
        channel = self._channel_for_switch_key(switch_key)
        if channel is None:
            return None
        value = getattr(getattr(channel, "control_handle", None), "value", None)
        return bool(value) if value is not None else None

    def _channel_for_switch_key(self, switch_key):
        suffix = str(switch_key or "").split("::", 1)[-1]
        return self._find_channel(channel_id=suffix, channel_key=suffix)

    def _find_channel(self, *, channel_id=None, channel_key=None):
        for channel in getattr(self.switch_service, "channels", ()):
            if channel_id and channel.channel_id == channel_id:
                return channel
            if channel_key and channel.key == channel_key:
                return channel
        return None

    def _release_rule(self, rule_id):
        rule = next(
            (candidate for candidate in self.rules if candidate["rule_id"] == rule_id),
            None,
        )
        actions = (rule or {}).get("script", {}).get("actions", ())
        prefix = "{}:".format(rule_id)
        for token in tuple(self._active_actions.keys()):
            if not token.startswith(prefix):
                continue
            active = self._active_actions.pop(token, None) or {}
            try:
                action = actions[int(token.rsplit(":", 1)[1])]
            except (IndexError, TypeError, ValueError):
                action = {}
            if (
                active.get("applied")
                and active.get("revert_to") is not None
                and action.get("revert_action") == "previous_state"
            ):
                self._apply_action(action.get("switch_key"), active["revert_to"])
        self._clear_rule_runtime(rule_id)
        self._persist_runtime_state()

    def _clear_rule_runtime(self, rule_id):
        prefix = "{}:".format(rule_id)
        for token in tuple(self._condition_states.keys()):
            if token.startswith(prefix):
                self._condition_states.pop(token, None)

    def _load(self):
        if not self.active or self.settings_root is None:
            return
        path = _join_path(self.settings_root, AUTOMATIONS_FILE)
        try:
            document = toml_compat.load_file(path)
        except OSError:
            return
        except Exception as exc:
            self.errors = ("automation_load_{}".format(type(exc).__name__),)
            return
        loaded = []
        for rule_id, value in _advanced_entries(document, path).items():
            if len(loaded) >= MAX_RULES or not isinstance(value, dict):
                continue
            try:
                script = json.loads(str(value.get("script_json", "") or ""))
                script = _normalize_script(script, self.runtime_config)
                enabled = bool(value.get("enabled", script.get("enabled", False)))
                script["enabled"] = enabled
                loaded.append(
                    {"rule_id": str(rule_id), "enabled": enabled, "script": script}
                )
            except Exception:
                continue
        self.rules = loaded

    def _load_runtime_state(self):
        if not self.active or self.settings_root is None:
            return
        path = _join_path(self.settings_root, AUTOMATION_STATE_FILE)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.loads(handle.read())
        except (OSError, TypeError, ValueError):
            return
        active = document.get("active") if isinstance(document, dict) else None
        if not isinstance(active, dict):
            return
        allowed = set()
        for rule in self.rules:
            for index, _action in enumerate(rule["script"].get("actions", ())):
                allowed.add("{}:{}".format(rule["rule_id"], index))
        for token, value in active.items():
            if token not in allowed or not isinstance(value, dict):
                continue
            revert_to = value.get("revert_to")
            if revert_to not in (True, False, None):
                continue
            self._active_actions[token] = {
                "due": 0.0,
                "applied": bool(value.get("applied", False)),
                "revert_to": revert_to,
            }

    def _persist_runtime_state(self):
        if self.settings_root is None:
            return False
        active = {}
        for token, value in self._active_actions.items():
            if value.get("applied"):
                active[token] = {
                    "applied": True,
                    "revert_to": value.get("revert_to"),
                }
        text = json.dumps({"active": active}, separators=(",", ":"))
        return not _replace_file(
            _join_path(self.settings_root, AUTOMATION_STATE_FILE), text
        )

    def _persist_rules(self, rules):
        if self.settings_root is None:
            return "automation_persistence_unavailable"
        return _replace_file(
            _join_path(self.settings_root, AUTOMATIONS_FILE),
            _automation_document_text(rules),
            error_prefix="automation_persist_",
        )


def _normalize_script(script, runtime_config, *, now_epoch=None):
    if not isinstance(script, dict):
        raise ValueError("automation_script_invalid")
    try:
        if len(json.dumps(script, separators=(",", ":"))) > MAX_SCRIPT_BYTES:
            raise ValueError("automation_script_too_large")
    except MemoryError:
        raise ValueError("automation_script_too_large")
    conditions = script.get("conditions") or []
    actions = script.get("actions") or []
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("automation_conditions_required")
    if not isinstance(actions, list) or not actions:
        raise ValueError("automation_actions_required")
    if len(conditions) > MAX_CONDITIONS:
        raise ValueError("automation_condition_limit")
    if len(actions) > MAX_ACTIONS:
        raise ValueError("automation_action_limit")
    normalized_conditions = [
        _normalize_condition(item, runtime_config, now_epoch=now_epoch)
        for item in conditions
    ]
    if normalized_conditions[0].get("type") == "or" or normalized_conditions[-1].get(
        "type"
    ) == "or":
        raise ValueError("automation_or_position")
    previous_or = False
    for condition in normalized_conditions:
        is_or = condition.get("type") == "or"
        if is_or and previous_or:
            raise ValueError("automation_or_position")
        previous_or = is_or
    normalized_actions = [
        _normalize_action(action, runtime_config) for action in actions
    ]
    name = str(script.get("name", "") or "").strip()
    if not name:
        raise ValueError("automation_name_required")
    return {
        "name": name[:64],
        "enabled": bool(script.get("enabled", False)),
        "conditions": normalized_conditions,
        "actions": normalized_actions,
    }


def _normalize_condition(condition, runtime_config, *, now_epoch=None):
    if not isinstance(condition, dict):
        raise ValueError("automation_condition_invalid")
    kind = str(condition.get("type", "") or "").strip().lower()
    if kind == "astral":
        raise ValueError("automation_astral_unsupported")
    if kind not in _VALID_CONDITION_TYPES:
        raise ValueError("automation_condition_type")
    if kind == "or":
        return {"type": "or"}
    if kind == "sensor":
        sensor = str(
            condition.get("sensor", condition.get("sensor_id", "")) or ""
        ).strip()
        if sensor and sensor != runtime_config.sensor.sensor_id:
            raise ValueError("automation_sensor_not_local")
        metric = str(condition.get("metric", "") or "").strip()
        operator = str(condition.get("op", ">") or ">").strip()
        if not metric:
            raise ValueError("automation_metric_required")
        if operator not in _VALID_OPERATORS:
            raise ValueError("automation_operator_invalid")
        try:
            threshold = float(condition.get("value"))
            hyst = max(0.0, float(condition.get("hyst", 0) or 0))
        except (TypeError, ValueError):
            raise ValueError("automation_threshold_invalid")
        return {
            "type": "sensor",
            "sensor": runtime_config.sensor.sensor_id,
            "metric": metric,
            "op": operator,
            "value": threshold,
            "hyst": hyst,
        }
    if kind == "time":
        start = _normalize_clock(condition.get("start"))
        end = _normalize_clock(condition.get("end"))
        days = []
        for value in condition.get("days") or range(7):
            try:
                day = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= day <= 6 and day not in days:
                days.append(day)
        if not days:
            raise ValueError("automation_days_required")
        return {"type": "time", "start": start, "end": end, "days": days}
    try:
        duration = int(condition.get("duration_min") or 0)
        period = int(condition.get("period_min") or 0)
        if period <= 0:
            period = int(condition.get("freq_hours") or 0) * 60
    except (TypeError, ValueError):
        raise ValueError("automation_timer_invalid")
    if duration <= 0 or period <= 0 or duration >= period:
        raise ValueError("automation_timer_invalid")
    try:
        anchor = int(condition.get("anchor_epoch"))
    except (TypeError, ValueError):
        if not _clock_ready(now_epoch):
            raise ValueError("automation_clock_unavailable")
        anchor = int(now_epoch)
    return {
        "type": "timer",
        "duration_min": duration,
        "period_min": period,
        "anchor_epoch": anchor,
    }


def _normalize_action(action, runtime_config):
    if not isinstance(action, dict):
        raise ValueError("automation_action_invalid")
    raw_key = str(
        action.get("switch_key", action.get("switch_label", "")) or ""
    ).strip()
    suffix = raw_key.split("::", 1)[-1]
    matched = None
    for channel in runtime_config.switch.channels:
        if suffix in (channel.channel_id, channel.key, channel.label):
            matched = channel
            break
    if matched is None:
        raise ValueError("automation_switch_not_local")
    desired = action.get("set", action.get("state", False))
    if isinstance(desired, str):
        desired = desired.strip().lower() in ("on", "true", "1")
    revert = str(action.get("revert_action", "previous_state") or "").lower()
    if revert not in ("previous_state", "do_nothing"):
        raise ValueError("automation_revert_invalid")
    try:
        delay = int(action.get("delay_s", action.get("delay", 0)) or 0)
    except (TypeError, ValueError):
        raise ValueError("automation_delay_invalid")
    if delay < 0 or delay > 60:
        raise ValueError("automation_delay_invalid")
    return {
        "switch_key": "{}::{}".format(
            runtime_config.switch.device_id, matched.channel_id
        ),
        "set": bool(desired),
        "revert_action": revert,
        "delay_s": delay,
    }


def _normalize_rule_id(rule_id, script, existing):
    text = str(rule_id or "").strip()
    if not text:
        text = str((script or {}).get("name", "automation") or "automation")
        text = "".join(
            char.lower() if _is_ascii_alnum(char) else "_" for char in text
        ).strip("_") or "automation"
        base = text[:40]
        text = base
        used = {rule["rule_id"] for rule in existing}
        suffix = 2
        while text in used:
            text = "{}_{}".format(base[:36], suffix)
            suffix += 1
    if len(text) > 48 or any(ord(char) < 32 for char in text):
        raise ValueError("automation_rule_id_invalid")
    return text


def _normalize_clock(value):
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("automation_time_invalid")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError("automation_time_invalid")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("automation_time_invalid")
    return "{:02d}:{:02d}".format(hour, minute)


def _time_matches(condition, now_epoch):
    local = time.localtime(now_epoch)
    weekday = int(local[6])
    minute = int(local[3]) * 60 + int(local[4])
    start = _clock_minutes(condition["start"])
    end = _clock_minutes(condition["end"])
    days = condition.get("days", ())
    if start > end and minute < end:
        weekday = (weekday - 1) % 7
    if weekday not in days:
        return False
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _clock_minutes(value):
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def _clock_ready(now_epoch):
    try:
        return int(time.localtime(int(now_epoch or 0))[0]) >= 2023
    except Exception:
        return False


def _compare(value, operator, threshold):
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    return value != threshold


def _epoch_time():
    try:
        return int(time.time())
    except Exception:
        return 0


def _error_payload(error):
    return {"success": False, "error": str(error), "errors": [str(error)]}


def _join_path(root, filename):
    text = str(root or ".").rstrip("/")
    return "{}/{}".format(text or ".", filename)


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _path_size(path):
    try:
        return int(os.stat(path)[6])
    except OSError:
        return 0


def _replace_file(path, text, error_prefix=""):
    tmp_path = "{}.tmp".format(path)
    backup_path = "{}.bak".format(path)
    moved_original = False
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            try:
                handle.flush()
            except AttributeError:
                pass
        if _path_size(tmp_path) <= 0:
            raise OSError("write_empty")
        if _path_exists(backup_path):
            os.remove(backup_path)
        if _path_exists(path):
            os.rename(path, backup_path)
            moved_original = True
        os.rename(tmp_path, path)
        return ""
    except Exception as exc:
        try:
            if moved_original and not _path_exists(path):
                os.rename(backup_path, path)
        except OSError:
            pass
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return "{}{}".format(error_prefix, type(exc).__name__)


def _advanced_entries(document, path):
    advanced = document.get("Advanced") or {}
    nested = {}
    for rule_id, value in advanced.items():
        if isinstance(value, dict):
            nested[str(rule_id)] = value
    try:
        with open(path, "r", encoding="utf-8") as handle:
            inline = _parse_inline_advanced(handle.read())
    except OSError:
        inline = {}
    nested.update(inline)
    return nested


def _parse_inline_advanced(text):
    rules = {}
    section = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Advanced" or "=" not in line:
            continue
        rule_id, value = line.split("=", 1)
        rule_id = _toml_key_text(rule_id.strip())
        body = value.strip()
        if not rule_id or not body.startswith("{") or "script_json" not in body:
            continue
        enabled = "enabled=true" in body.replace(" ", "").lower()
        script_part = body.split("script_json", 1)[1]
        if "=" not in script_part:
            continue
        script_part = script_part.split("=", 1)[1].strip()
        if script_part.endswith("}"):
            script_part = script_part[:-1].rstrip()
        try:
            parsed = toml_compat.loads(
                "[Value]\nscript_json = {}\n".format(script_part)
            )
            script_json = parsed.get("Value", {}).get("script_json", "")
        except Exception:
            continue
        rules[rule_id] = {"enabled": enabled, "script_json": script_json}
    return rules


def _toml_key_text(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def _automation_document_text(rules):
    lines = [
        "[Meta]",
        "version = 1",
        'notes = "NodusWeb automations; Sensorius compatible without Astral."',
        "",
        "[Advanced]",
    ]
    for rule in rules:
        script_json = json.dumps(rule["script"], separators=(",", ":"))
        lines.append(
            "{} = {{ enabled={}, script_json={} }}".format(
                _toml_key(rule["rule_id"]),
                "true" if rule["enabled"] else "false",
                _toml_string(script_json),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _toml_key(value):
    text = str(value or "")
    if text and all(_is_ascii_alnum(char) or char in "_-" for char in text):
        return text
    return _toml_string(text)


def _is_ascii_alnum(char):
    return "0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z"


def _toml_string(value):
    text = str(value or "")
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))
