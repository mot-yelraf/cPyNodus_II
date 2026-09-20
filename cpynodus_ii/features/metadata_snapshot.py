"""Read saved configuration fields for retained MQTT metadata.

Stream only advertised TOML scalars when a settings root is available. This
keeps persisted state distinct from volatile runtime edits without allocating
another settings document or retaining a second runtime configuration.
"""

_TIME_KEYS = ("TZ", "TZ_OFFSET", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP")
_HA_KEYS = (
    "DISCOVERY_PREFIX",
    "BASE_TOPIC",
    "PUBLISH_DISCOVERY_RETAIN",
    "PUBLISH_STATE_RETAIN",
    "PUBLISH_LEGACY_SENSOR_TOPIC",
)
_DEVICE_KEYS = (
    "TEMP_OFFSET",
    "RH_OFFSET",
    "CO2_OFFSET",
    "AQI_OFFSET",
    "GAS_OFFSET",
    "LUX_OFFSET",
    "PPFD_OFFSET",
    "APVPD_TEMP_CAL_VAL",
    "APVPD_RH_CAL_VAL",
    "ALTITUDE_METERS",
)
_SOIL_KEYS = (
    "SOIL_TEMP_CAL_VAL",
    "SOIL_MOIST_CAL_VAL",
    "SOIL_PH_CAL_VAL",
    "SOIL_EC_CAL_VAL",
)
_SYSTEM_KEYS = (
    "TEMP_OFFSET",
    "RH_OFFSET",
    "CO2_OFFSET",
    "REF_SENSOR_ID",
    "REF_RANGE_HOURS",
    "REF_START_TS",
    "REF_END_TS",
    "REF_NOTE",
)


def _saved_scalars(root, filename):
    from cpynodus_ii.core.toml_compat import _parse_value, _strip_comment

    path = "{}/{}".format(str(root).rstrip("/"), filename)
    # Match settings loading's interrupted-write backup fallback.
    import os

    try:
        size = os.stat(path)[6]
    except OSError as exc:
        if getattr(exc, "errno", exc.args[0]) != 2:
            raise
        size = 0
    if size <= 0:
        try:
            if os.stat(path + ".bak")[6] > 0:
                path += ".bak"
            else:
                return
        except OSError as exc:
            if getattr(exc, "errno", exc.args[0]) != 2:
                raise
            return
    section = ""
    with open(path, "r") as handle:
        for raw_line in handle:
            line = _strip_comment(raw_line).strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif "=" in line:
                key, value = line.split("=", 1)
                yield section, key.strip(), _parse_value(value.strip())


def add_configuration_snapshot(payload, config, root=None, *, compact=False):
    """Add advertised settings, preferring saved scalars over live values."""
    sensor = config.sensor
    if not compact:
        payload["time"] = {key: getattr(config.time, key.lower()) for key in _TIME_KEYS}
        payload["homeassistant"] = {
            key: getattr(config.homeassistant, key.lower()) for key in _HA_KEYS
        }
    calibration = {}
    device_keys = _SOIL_KEYS if sensor.family == "soil" else _DEVICE_KEYS
    if compact:
        device_keys = () if sensor.family == "soil" else ("ALTITUDE_METERS",)
    if root is None:
        if sensor.present:
            calibration["Device"] = {
                key: getattr(sensor.calibration_device, key.lower())
                for key in device_keys
            }
            if not compact and sensor.family != "soil":
                calibration["System"] = {
                    key: getattr(sensor.calibration_system, key.lower())
                    for key in _SYSTEM_KEYS[:3]
                }
    else:
        # Absence is not a configured default; a hub must preserve unknown fields.
        if not compact:
            payload["time"] = {}
            payload["homeassistant"] = {}
        for section, key, value in _saved_scalars(root, "settings.toml"):
            if not compact and section == "Time" and key in _TIME_KEYS:
                payload["time"][key] = value
            elif not compact and section == "HomeAssistant" and key in _HA_KEYS:
                payload["homeassistant"][key] = value
            elif (
                "network" in payload
                and section == "Network"
                and key in ("SSID", "HOSTNAME", "PASSWORD")
            ):
                if key == "PASSWORD":
                    from cpynodus_ii.features.payloads import _obfuscated_password

                    value = _obfuscated_password(value, config)
                payload["network"][key.lower()] = value
            elif (
                "mqtt" in payload
                and section == "MQTT"
                and key
                in (
                    "BROKER",
                    "BROKER_IP",
                    "PORT",
                    "USE_TLS",
                    "BASE_TOPIC",
                    "USERNAME",
                    "PASSWORD",
                )
            ):
                if key == "PASSWORD":
                    from cpynodus_ii.features.payloads import _obfuscated_password

                    value = _obfuscated_password(value, config)
                if key not in ("USERNAME", "PASSWORD") or value:
                    payload["mqtt"][key.lower()] = value
                else:
                    payload["mqtt"].pop(key.lower(), None)
            elif (
                "profile" in payload
                and section == "Profile"
                and key == "ACTIVE_PROFILE"
            ):
                payload["profile"]["active_profile"] = value
        if sensor.present and sensor.active_config_file:
            metrics = {}
            styles = {}
            for section, key, value in _saved_scalars(root, sensor.active_config_file):
                if (
                    not compact
                    and section == "Calibration"
                    and key in ("CALIBRATED", "CALIB_STATUS")
                ):
                    calibration[key] = value
                elif (
                    not compact
                    and section == "Calibration.System"
                    and key in _SYSTEM_KEYS
                ):
                    calibration.setdefault("System", {})[key] = value
                elif section == "Calibration.Device":
                    if (
                        not compact
                        and sensor.family == "soil"
                        and key == "SOIL_TEMP_MOIST_VAL"
                    ):
                        calibration.setdefault("Device", {}).setdefault(
                            "SOIL_MOIST_CAL_VAL", value
                        )
                    elif key in device_keys:
                        calibration.setdefault("Device", {})[key] = value
                elif section == "Sensor" and key == "LOCATION":
                    payload["sensor"]["location"] = value
                    if "location_group" in payload:
                        payload["location_group"]["location"] = value
                elif not compact and section in ("Display", "Display.Style"):
                    if key in (
                        "METRIC_1",
                        "METRIC_2",
                        "METRIC_3",
                        "METRIC_4",
                        "METRIC_5",
                        "METRIC_6",
                    ):
                        target = metrics if section == "Display" else styles
                        target[key] = value
            if not compact:
                payload["sensor"]["display_metrics"] = [
                    metrics[key] for key in sorted(metrics) if metrics[key]
                ]
                payload["sensor"]["display_styles"] = [
                    styles[key] for key in sorted(styles) if styles[key]
                ]
    if sensor.present:
        payload["sensor"]["calibration"] = calibration


def apply_saved_switch(payload, switch, root):
    """Overlay saved physical switch settings without changing live state."""
    for section, key, value in _saved_scalars(root, "switch.toml"):
        if section != "Switch":
            continue
        if key == "SWITCH_LOCATION":
            payload["location"] = value
        for channel, entry in zip(switch.channels, payload["channels"]):
            prefix = channel.key + "_"
            if not key.startswith(prefix):
                continue
            field = {
                "PIN": "pin",
                "ENABLE_PIN": "enable_pin",
                "LABEL": "label",
                "OVERRIDE_SCRIPT": "override_script",
            }.get(key[len(prefix) :])
            if field:
                entry[field] = value
