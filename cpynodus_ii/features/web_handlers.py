"""Implement host-testable handlers behind the web route table.

The handler functions assemble payloads and apply actions without depending on
the concrete web server implementation, which keeps the route layer thin and
easy to test.
"""

from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.web_config import (
    apply_web_config_updates,
    apply_web_switch_override,
)
from cpynodus_ii.features.web_routes import build_web_route_table


def build_status_payload(
    runtime_config,
    *,
    version,
    sensor_service=None,
    sensor_snapshot=None,
    display_timestamp="",
    switch_service=None,
    automation_service=None,
    ip_address="",
    wifi_recovery_count=0,
):
    """Build the operational status payload for `/` and `/current-data`."""
    if sensor_snapshot is None and sensor_service is not None:
        from cpynodus_ii.features.sensor_service import read_sensor_snapshot

        sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
    switch_snapshot = {}
    if switch_service is not None:
        from cpynodus_ii.features.switch_service import snapshot_switch_states

        switch_snapshot = snapshot_switch_states(switch_service)

    display_metrics = []
    for index, metric in enumerate(runtime_config.sensor.display.metrics, start=1):
        if not metric:
            continue
        display_metrics.append(
            {
                "index": index,
                "metric": metric,
                "style": runtime_config.sensor.display.styles[index - 1],
                "value": (sensor_snapshot.metrics or {}).get(metric)
                if sensor_snapshot is not None
                else None,
            }
        )

    switch_channels = []
    for channel in runtime_config.switch.channels:
        snapshot = switch_snapshot.get(channel.key, {})
        owners = ()
        if automation_service is not None and automation_service.active:
            owners = automation_service.channel_controlled(
                channel_id=channel.channel_id
            )
        switch_channels.append(
            {
                "key": channel.key,
                "channel_id": channel.channel_id,
                "label": channel.label,
                "state": snapshot.get("state", channel.last_state),
                "phase": snapshot.get("phase", ""),
                "automation": {
                    "controlled": bool(owners),
                    "controller": "nodusweb" if owners else "",
                    "automations": list(owners),
                },
            }
        )

    return {
        "schema": "nodus-web-status/v1",
        "version": str(version or ""),
        "profile": runtime_config.active_profile,
        "ap_mode": bool(runtime_config.ap_mode),
        "network": {
            "hostname": runtime_config.network.hostname,
            "ssid": runtime_config.network.ssid,
            "ipv4addr": str(ip_address or ""),
            "wifi_recovery_count": int(wifi_recovery_count or 0),
        },
        "sensor": {
            "present": bool(runtime_config.sensor.present),
            "sensor_id": runtime_config.sensor.sensor_id,
            "device": runtime_config.sensor.device,
            "location": runtime_config.sensor.location,
            "display_timestamp": str(display_timestamp or ""),
            "display_metrics": display_metrics,
            "snapshot": {
                "phase": getattr(sensor_snapshot, "phase", "unavailable"),
                "metrics": dict(getattr(sensor_snapshot, "metrics", {}) or {}),
                "errors": list(getattr(sensor_snapshot, "errors", ()) or ()),
            },
        },
        "switch": {
            "present": bool(runtime_config.switch.present),
            "device_id": runtime_config.switch.device_id,
            "location": runtime_config.switch.location,
            "channels": switch_channels,
        },
    }


def build_setup_payload(runtime_config, *, version):
    """Build the view-model payload for `/setup`."""
    route_table = build_web_route_table(runtime_config)
    sensor = runtime_config.sensor
    return {
        "schema": "nodus-setup/v1",
        "version": str(version or ""),
        "profile": runtime_config.active_profile,
        "ap_mode": bool(runtime_config.ap_mode),
        "routes": [
            {
                "path": route.path,
                "methods": list(route.methods),
                "kind": route.kind,
                "description": route.description,
            }
            for route in route_table
        ],
        "network": {
            "ssid": runtime_config.network.ssid,
            "password": runtime_config.network.password,
            "hostname": runtime_config.network.hostname,
            "http_port": runtime_config.network.http_port,
            "ap_channel": runtime_config.network.ap_channel,
        },
        "sensor": {
            "present": bool(sensor.present),
            "device": sensor.device,
            "hardware": sensor.hardware,
            "family": sensor.family,
            "interface": sensor.interface,
            "active_config_file": sensor.active_config_file,
            "sensor_id": sensor.sensor_id,
            "serial_number": sensor.serial_number,
            "location": sensor.location,
            "display_metrics": list(sensor.display.metrics),
            "display_styles": list(sensor.display.styles),
            "calibration_system": _calibration_payload(sensor.calibration_system),
            "calibration_device": _calibration_payload(sensor.calibration_device),
        },
        "switch": {
            "present": bool(runtime_config.switch.present),
            "device_id": runtime_config.switch.device_id,
            "serial_number": runtime_config.switch.serial_number,
            "location": runtime_config.switch.location,
            "channel_count": runtime_config.switch.channel_count,
            "channels": [
                {
                    "key": channel.key,
                    "channel_id": channel.channel_id,
                    "label": channel.label,
                    "last_state": bool(channel.last_state),
                }
                for channel in runtime_config.switch.channels
            ],
        },
        "time": {
            "tz": runtime_config.time.tz,
            "tz_offset": runtime_config.time.tz_offset,
            "tz_name": runtime_config.time.tz_name,
            "ntp_server": runtime_config.time.ntp_server,
            "ntp_server_ip": runtime_config.time.ntp_server_ip,
        },
        "mqtt": {
            "broker": runtime_config.mqtt.broker,
            "broker_ip": runtime_config.mqtt.broker_ip,
            "port": runtime_config.mqtt.port,
            "use_tls": bool(runtime_config.mqtt.use_tls),
            "base_topic": runtime_config.mqtt.base_topic,
            "username": runtime_config.mqtt.username,
            "password": runtime_config.mqtt.password,
        },
    }


def build_config_page_payload(runtime_config, *, version, page):
    """Build only the values needed to render one configuration page."""
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    payload = {
        "version": str(version or ""),
        "profile": runtime_config.active_profile,
        "network": {"hostname": runtime_config.network.hostname},
        "switch": {"present": bool(switch.present)},
    }
    if page == "setup":
        payload["network"].update(
            {
                "ssid": runtime_config.network.ssid,
                "password": runtime_config.network.password,
            }
        )
        payload.update(
            {
                "sensor": {
                    "location": sensor.location,
                    "display_metrics": sensor.display.metrics,
                    "display_styles": sensor.display.styles,
                },
                "time": {
                    "tz": runtime_config.time.tz,
                    "tz_offset": runtime_config.time.tz_offset,
                    "tz_name": runtime_config.time.tz_name,
                    "ntp_server": runtime_config.time.ntp_server,
                    "ntp_server_ip": runtime_config.time.ntp_server_ip,
                },
                "mqtt": {
                    "broker": runtime_config.mqtt.broker,
                    "port": runtime_config.mqtt.port,
                    "use_tls": bool(runtime_config.mqtt.use_tls),
                    "username": runtime_config.mqtt.username,
                    "password": runtime_config.mqtt.password,
                },
            }
        )
    elif page == "calibration":
        payload["sensor"] = {
            "device": sensor.device,
            "calibration_device": _calibration_payload(sensor.calibration_device),
        }
    elif page == "switch":
        payload["switch"].update(
            {
                "device_id": switch.device_id,
                "serial_number": switch.serial_number,
                "location": switch.location,
                "channel_count": switch.channel_count,
                "channels": tuple(
                    {
                        "key": channel.key,
                        "channel_id": channel.channel_id,
                        "label": channel.label,
                    }
                    for channel in switch.channels
                ),
            }
        )
    elif page == "info":
        payload["network"]["ssid"] = runtime_config.network.ssid
        payload.update(
            {
                "sensor": {
                    "device": sensor.device,
                    "hardware": sensor.hardware,
                    "interface": sensor.interface,
                    "sensor_id": sensor.sensor_id,
                },
            }
        )
        payload["switch"].update(
            {
                "device_id": switch.device_id,
                "channel_count": switch.channel_count,
            }
        )
    return payload


def _calibration_payload(calibration):
    return {
        "TEMP_OFFSET": calibration.temp_offset,
        "RH_OFFSET": calibration.rh_offset,
        "CO2_OFFSET": calibration.co2_offset,
        "AQI_OFFSET": calibration.aqi_offset,
        "GAS_OFFSET": calibration.gas_offset,
        "LUX_OFFSET": calibration.lux_offset,
        "PPFD_OFFSET": calibration.ppfd_offset,
        "APVPD_TEMP_CAL_VAL": calibration.apvpd_temp_cal_val,
        "APVPD_RH_CAL_VAL": calibration.apvpd_rh_cal_val,
        "ALTITUDE_METERS": calibration.altitude_meters,
        "SOIL_TEMP_CAL_VAL": calibration.soil_temp_cal_val,
        "SOIL_MOIST_CAL_VAL": calibration.soil_moist_cal_val,
        "SOIL_PH_CAL_VAL": calibration.soil_ph_cal_val,
        "SOIL_EC_CAL_VAL": calibration.soil_ec_cal_val,
    }


def handle_web_config_request(runtime_config, updates, *, settings_root=None):
    """Apply web-driven config updates and return a route payload."""
    result = apply_web_config_updates(
        runtime_config,
        updates,
        settings_root=settings_root,
    )
    return {
        "success": bool(result.applied_updates),
        "updated": len(result.applied_updates),
        "live_updated": len(result.live_updates),
        "restart_required": bool(result.restart_required_updates),
        "live_updates": list(result.live_updates),
        "restart_required_updates": list(result.restart_required_updates),
        "ignored_updates": list(result.ignored_updates),
        "errors": list(result.errors),
        "persistence_mode": result.persistence_mode,
        "runtime_config": result.runtime_config,
    }


def handle_switch_state_request(
    runtime_config,
    switch_service,
    *,
    channel_id=None,
    channel_key=None,
    state,
    settings_root=None,
):
    """Apply a web switch override and persist last-state when possible."""
    updated_runtime_config, apply_result = apply_web_switch_override(
        runtime_config,
        switch_service,
        channel_id=channel_id,
        channel_key=channel_key,
        state=state,
    )
    persistence_errors = ()
    persistence_mode = ""
    if settings_root is not None and apply_result.phase == "ready" and apply_result.key:
        _, _, persistence_errors = Settings.apply_updates_to_directory(
            settings_root,
            updated_runtime_config,
            (
                {
                    "section": "Switch",
                    "key": "{}_LAST_STATE".format(apply_result.key),
                    "value": bool(apply_result.applied_state),
                },
            ),
            reload_runtime=False,
        )
        persistence_mode = "volatile" if persistence_errors else "persisted"
    return {
        "success": apply_result.phase == "ready",
        "phase": apply_result.phase,
        "channel_id": apply_result.channel_id,
        "channel_key": apply_result.key,
        "state": apply_result.applied_state,
        "errors": list(apply_result.errors) + list(persistence_errors),
        "persistence_mode": persistence_mode,
        "runtime_config": updated_runtime_config,
    }
