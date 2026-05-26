"""Support constrained onboarding and setup web-service behavior.

The web-service helpers manage onboarding persistence, setup payload shaping,
and lightweight service decisions used during AP provisioning and normal web
configuration flows.
"""

import time

from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import onboarding_state as _onboarding_state

ITAOT_META_SCHEMA = "itaot-meta/v1"

_DISPLAY_METRICS_BY_DEVICE = {
    "aqi": ("Air Quality", "Temperature"),
    "co2": ("CO2", "Temperature"),
    "avpd": ("Temperature", "Ambient VPD"),
    "apvpd": ("Temperature", "Ambient VPD"),
    "lux": ("Light Intensity", "Estimated PPFD"),
    "soil": ("Soil Moisture", "Soil Temp_C"),
}


def _clean_str(value):
    return str(value or "").strip()


def _coerce_port(value):
    try:
        port = int(value)
    except Exception:
        return 0
    if 0 < port <= 65535:
        return port
    return 0


def _bool_capabilities(runtime_config):
    return {
        "sensor": bool(getattr(runtime_config.sensor, "present", False)),
        "switch": bool(getattr(runtime_config.switch, "present", False)),
    }


def bootstrap_routes_enabled(runtime_config):
    """Return whether the bootstrap route pair should be exposed."""
    return bool(getattr(runtime_config, "ap_mode", False))


def load_onboarding_state(root="."):
    """Load any persisted onboarding runtime state."""
    return _onboarding_state.load_onboarding_state(root)


def save_onboarding_state(root, state):
    """Persist onboarding runtime state outside TOML config files."""
    return _onboarding_state.save_onboarding_state(root, state)


def clear_onboarding_state(root="."):
    """Delete any persisted onboarding runtime state."""
    return _onboarding_state.clear_onboarding_state(root)


def normalize_itaot_init_payload(payload):
    """Validate and normalize the onboarding bootstrap payload."""
    document = payload if isinstance(payload, dict) else {}
    mqtt_doc = document.get("mqtt", {})
    if not isinstance(mqtt_doc, dict):
        mqtt_doc = {}

    onboard_token = _clean_str(document.get("onboard_token", ""))
    ssid = _clean_str(document.get("ssid", ""))
    password = _clean_str(document.get("password", ""))
    hostname = _clean_str(document.get("hostname", ""))
    broker_host = _clean_str(mqtt_doc.get("broker_host", ""))
    broker_ip = _clean_str(mqtt_doc.get("broker_ip", ""))
    broker_port = _coerce_port(mqtt_doc.get("broker_port", 0))
    mqtt_username = _clean_str(mqtt_doc.get("username", ""))
    mqtt_password = _clean_str(mqtt_doc.get("password", ""))
    mqtt_base_topic = _clean_str(mqtt_doc.get("base_topic", "")) or "nodus"
    active_profile = _clean_str(mqtt_doc.get("active_profile", "")).lower()
    if not active_profile and broker_host:
        active_profile = "sensorius"

    errors = []
    if not onboard_token:
        errors.append("onboard_token_required")
    if not ssid:
        errors.append("ssid_required")
    if not password:
        errors.append("password_required")
    if not hostname:
        errors.append("hostname_required")
    if not broker_host:
        errors.append("mqtt_broker_host_required")
    if not broker_port:
        errors.append("mqtt_broker_port_required")
    if active_profile and active_profile not in {
        "sensorius",
        "weewx",
        "homeassistant",
        "nodusweb",
    }:
        errors.append("mqtt_active_profile_invalid")

    return {
        "onboard_token": onboard_token,
        "ssid": ssid,
        "password": password,
        "hostname": hostname,
        "mqtt": {
            "broker_host": broker_host,
            "broker_ip": broker_ip,
            "broker_port": broker_port,
            "username": mqtt_username,
            "password": mqtt_password,
            "base_topic": mqtt_base_topic,
            "active_profile": active_profile or "sensorius",
        },
        "errors": tuple(errors),
    }


def build_itaot_init_updates(normalized_payload):
    """Translate normalized bootstrap input into TOML updates."""
    mqtt_doc = normalized_payload.get("mqtt", {})
    updates = [
        {
            "section": "Network",
            "key": "SSID",
            "value": normalized_payload.get("ssid", ""),
        },
        {
            "section": "Network",
            "key": "PASSWORD",
            "value": normalized_payload.get("password", ""),
        },
        {
            "section": "Network",
            "key": "HOSTNAME",
            "value": normalized_payload.get("hostname", ""),
        },
        {"section": "MQTT", "key": "BROKER", "value": mqtt_doc.get("broker_host", "")},
        {
            "section": "MQTT",
            "key": "PORT",
            "value": int(mqtt_doc.get("broker_port", 0) or 0),
        },
        {
            "section": "MQTT",
            "key": "BASE_TOPIC",
            "value": mqtt_doc.get("base_topic", "nodus"),
        },
        {
            "section": "Profile",
            "key": "ACTIVE_PROFILE",
            "value": mqtt_doc.get("active_profile", "sensorius"),
        },
    ]
    if mqtt_doc.get("broker_ip"):
        updates.append(
            {"section": "MQTT", "key": "BROKER_IP", "value": mqtt_doc.get("broker_ip")}
        )
    if mqtt_doc.get("username"):
        updates.append(
            {"section": "MQTT", "key": "USERNAME", "value": mqtt_doc.get("username")}
        )
    if mqtt_doc.get("password"):
        updates.append(
            {"section": "MQTT", "key": "PASSWORD", "value": mqtt_doc.get("password")}
        )
    return tuple(updates)


class ItaotInitResult:
    """Describe the result of bootstrap payload handling."""

    def __init__(
        self,
        *,
        accepted,
        rebooting,
        status_code,
        body,
        runtime_config,
        applied_updates=(),
        errors=(),
    ):
        self.accepted = bool(accepted)
        self.rebooting = bool(rebooting)
        self.status_code = int(status_code)
        self.body = body
        self.runtime_config = runtime_config
        self.applied_updates = tuple(applied_updates or ())
        self.errors = tuple(errors or ())


def apply_itaot_init_payload(payload, runtime_config, *, settings_root="."):
    """Apply a validated onboarding bootstrap payload to local config."""
    normalized = normalize_itaot_init_payload(payload)
    errors = normalized.get("errors", ())
    if errors:
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=400,
            body={"success": False, "accepted": False, "errors": list(errors)},
            runtime_config=runtime_config,
            errors=tuple(errors),
        )

    updates = build_itaot_init_updates(normalized)
    reloaded_config, applied_updates, persistence_errors = (
        Settings.apply_updates_to_directory(
            settings_root,
            runtime_config,
            updates,
            reload_runtime=True,
        )
    )
    if persistence_errors:
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=503,
            body={
                "success": False,
                "accepted": False,
                "errors": list(persistence_errors),
            },
            runtime_config=reloaded_config,
            applied_updates=tuple(applied_updates),
            errors=tuple(persistence_errors),
        )

    state = {
        "schema": "nodus-onboard-state/v1",
        "onboard_token": normalized.get("onboard_token", ""),
        "hostname": normalized.get("hostname", ""),
        "base_topic": normalized.get("mqtt", {}).get("base_topic", "nodus"),
        "active_profile": normalized.get("mqtt", {}).get("active_profile", "sensorius"),
        "created_at": int(time.time()),
    }
    try:
        save_onboarding_state(settings_root, state)
    except OSError as exc:
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=503,
            body={
                "success": False,
                "accepted": False,
                "errors": ["onboarding_state_persist_failed", str(exc)],
            },
            runtime_config=reloaded_config,
            applied_updates=tuple(applied_updates),
            errors=("onboarding_state_persist_failed", str(exc)),
        )

    body = {
        "success": True,
        "accepted": True,
        "rebooting": True,
        "restart_mode": "hard",
        "hostname": normalized.get("hostname", ""),
        "mqtt_profile": normalized.get("mqtt", {}).get("active_profile", "sensorius"),
        "base_topic": normalized.get("mqtt", {}).get("base_topic", "nodus"),
    }
    return ItaotInitResult(
        accepted=True,
        rebooting=True,
        status_code=200,
        body=body,
        runtime_config=reloaded_config,
        applied_updates=tuple(applied_updates),
        errors=(),
    )


def _display_metrics_for_sensor(sensor):
    if not getattr(sensor, "present", False):
        return ()
    configured = tuple(
        value
        for value in tuple(
            getattr(getattr(sensor, "display", None), "metrics", ()) or ()
        )
        if _clean_str(value)
    )
    if configured:
        return configured
    return _DISPLAY_METRICS_BY_DEVICE.get(
        _clean_str(getattr(sensor, "device", "")).lower(), ()
    )


def _calibration_status(sensor):
    offsets = []
    for calibration in (
        getattr(sensor, "calibration_system", None),
        getattr(sensor, "calibration_device", None),
    ):
        if calibration is None:
            continue
        for name in (
            "temp_offset",
            "rh_offset",
            "co2_offset",
            "aqi_offset",
            "gas_offset",
            "lux_offset",
            "ppfd_offset",
            "apvpd_temp_cal_val",
            "apvpd_rh_cal_val",
            "soil_temp_cal_val",
            "soil_temp_moist_val",
            "soil_ph_cal_val",
            "soil_ec_cal_val",
        ):
            try:
                offsets.append(float(getattr(calibration, name, 0.0) or 0.0))
            except Exception:
                offsets.append(0.0)
    calibrated = any(value != 0.0 for value in offsets)
    return {
        "calibrated": calibrated,
        "status": "Calibrated" if calibrated else "Not calibrated",
    }


def _switch_channel_state(channel, switch_states):
    snapshot = {}
    if isinstance(switch_states, dict):
        snapshot = (
            switch_states.get(channel.key)
            or switch_states.get(channel.channel_id)
            or {}
        )
    if isinstance(snapshot, dict) and "state" in snapshot:
        return bool(snapshot.get("state"))
    return bool(getattr(channel, "last_state", False))


def build_itaot_meta_payload(
    runtime_config, *, version, ip_address="", switch_states=None
):
    """Build the compact onboarding metadata payload."""
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = sensor.sensor_id or switch.device_id or runtime_config.network.hostname
    members = []
    if sensor.sensor_id:
        members.append(sensor.sensor_id)
    for channel in switch.channels:
        if channel.channel_id:
            members.append(channel.channel_id)

    sensor_block = {
        "present": bool(sensor.present),
        "device": sensor.device,
        "sensor_id": sensor.sensor_id,
        "serial": sensor.serial_number,
        "location": sensor.location,
        "active_sensor_file": sensor.active_config_file,
        "display_metrics": list(_display_metrics_for_sensor(sensor)),
        "calibration": _calibration_status(sensor),
    }
    if not sensor.present:
        sensor_block["display_metrics"] = []

    channels = []
    for index, channel in enumerate(switch.channels, start=1):
        channels.append(
            {
                "index": index,
                "label": channel.label,
                "channel_id": channel.channel_id,
                "state": _switch_channel_state(channel, switch_states),
                "enabled": True,
            }
        )

    switch_block = {
        "present": bool(switch.present),
        "device_id": switch.device_id,
        "serial": switch.serial_number,
        "location": switch.location,
        "channels": channels,
    }

    location_group = {
        "id": sensor.serial_number or switch.serial_number or device_id,
        "members": members,
        "label": " ".join(member for member in members if member),
    }

    return {
        "schema": ITAOT_META_SCHEMA,
        "version": _clean_str(version),
        "origin": "nodus",
        "device_id": device_id,
        "network": {
            "hostname": runtime_config.network.hostname,
            "ssid": runtime_config.network.ssid,
            "ipv4addr": _clean_str(ip_address),
        },
        "device": {
            "type": "nodus",
            "capabilities": _bool_capabilities(runtime_config),
        },
        "endpoints": {},
        "sensor": sensor_block,
        "switch": switch_block,
        "location_group": location_group,
    }
