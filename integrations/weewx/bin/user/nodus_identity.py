"""Expose retained Nodus MQTT identity metadata to WeeWX skins.

The extension caches device metadata and supplies report-friendly identity and
network fields without coupling skin generation to the MQTT client.
"""

import json
import logging
import os
import re
import time

try:
    from weewx.cheetahgenerator import SearchList
except ImportError:  # Allow host-side tests without a WeeWX installation.
    class SearchList:
        """Minimal test fallback for the WeeWX SearchList base class."""

        def __init__(self, generator):
            self.generator = generator


log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 2.0
_DEFAULT_CACHE_TTL = 900.0
_UNKNOWN = "unknown"
_DEFAULT_DESCRIPTION = "Sensor conditions"
_DESCRIPTIONS = {
    "avpd": "Indoor environment + VPD + disease-risk indicators.",
    "co2": "Outdoor environment + CO2 + VPD + disease-risk indicators.",
    "aqi": "Indoor environment + AQI + VPD + disease-risk indicators.",
    "soil": "Soil Moisture + Temperature",
}


def _to_bool(value):
    """Return a permissive boolean for ConfigObj scalar values."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _section_names(section):
    """Return nested ConfigObj section names or plain mapping keys."""
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


def _derive_meta_topic(mqtt_section, configured_topic=None):
    """Return an exact retained meta topic from the configured data topic."""
    if configured_topic:
        return str(configured_topic).strip()

    topics = mqtt_section.get("topics", {})
    data_topics = []
    for topic in _section_names(topics):
        topic_text = str(topic).strip()
        if (
            topic_text.endswith("/data")
            and "+" not in topic_text
            and "#" not in topic_text
        ):
            data_topics.append(topic_text)
    if len(data_topics) != 1:
        raise ValueError(
            "expected one exact MQTTSubscribe /data topic, found {}".format(
                len(data_topics)
            )
        )
    return data_topics[0][:-5] + "/meta"


def _mqtt_settings(config_dict, identity_config):
    """Resolve broker connection settings and the retained meta topic."""
    section = _mqtt_section(config_dict)
    topic = _derive_meta_topic(section, identity_config.get("meta_topic"))
    tls = section.get("tls", {})
    return {
        "host": str(section.get("host") or "localhost"),
        "port": int(section.get("port") or 1883),
        "username": str(section.get("username") or ""),
        "password": str(section.get("password") or ""),
        "tls": _to_bool(tls.get("enable", False)),
        "ca_certs": str(tls.get("ca_certs") or ""),
        "topic": topic,
    }


def _sensor_family(device_id, data=None):
    """Infer the display family from Nodus identity and sensor hardware."""
    data = data or {}
    sensor = data.get("sensor") or {}
    candidates = (
        data.get("device"),
        sensor.get("device"),
        str(device_id).partition("-")[0],
    )
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in {"aht", "avpd", "apvpd", "apvpd_aht"}:
            return "avpd"
        if normalized in {"co2", "aqi", "soil"}:
            return normalized

    hardware = str(sensor.get("hardware") or "").strip().lower()
    if "soil" in hardware:
        return "soil"
    if "bme680" in hardware:
        return "aqi"
    if hardware.startswith("scd"):
        return "co2"
    if "aht" in hardware or "bme280" in hardware:
        return "avpd"
    return ""


def _sensor_description(device_id, data=None):
    """Return the sensor-family description shown by Nodus."""
    return _DESCRIPTIONS.get(
        _sensor_family(device_id, data), _DEFAULT_DESCRIPTION
    )


def _identity_from_payload(payload, topic):
    """Validate a retained Nodus meta payload and return display identity."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    data = json.loads(payload)
    if data.get("schema") != "nodus-meta/v1":
        raise ValueError("unexpected Nodus meta schema")
    device_id = str(data.get("device_id") or "").strip()
    version = str(data.get("version") or "").strip()
    if not device_id or not version:
        raise ValueError("Nodus meta is missing device_id or version")
    return {
        "device_id": device_id,
        "version": version,
        "description": _sensor_description(device_id, data),
        "meta_topic": topic,
        "updated": int(time.time()),
    }


def _new_mqtt_client(mqtt_module, client_id):
    """Create a Paho client compatible with callback API v1 and v2."""
    callback_api = getattr(mqtt_module, "CallbackAPIVersion", None)
    if callback_api is not None:
        return mqtt_module.Client(callback_api.VERSION1, client_id=client_id)
    return mqtt_module.Client(client_id=client_id)


def _fetch_retained_meta(settings, timeout):
    """Subscribe once and return the retained Nodus identity payload."""
    import paho.mqtt.client as mqtt

    received = {}
    client_id = "weewx-nodus-identity-{}".format(os.getpid())
    client = _new_mqtt_client(mqtt, client_id)

    if settings["username"]:
        client.username_pw_set(settings["username"], settings["password"])
    if settings["tls"]:
        ca_certs = settings["ca_certs"] or None
        client.tls_set(ca_certs=ca_certs)

    def on_connect(active_client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0:
            active_client.subscribe(settings["topic"], qos=0)

    def on_message(_client, _userdata, message):
        if message.topic == settings["topic"]:
            received["identity"] = _identity_from_payload(
                message.payload, settings["topic"]
            )

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings["host"], settings["port"], keepalive=10)
    deadline = time.monotonic() + max(0.1, float(timeout))
    try:
        while "identity" not in received and time.monotonic() < deadline:
            client.loop(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    if "identity" not in received:
        raise TimeoutError("retained Nodus meta was not received")
    return received["identity"]


def _safe_device_token(meta_topic):
    """Return a filesystem-safe device token derived from a meta topic."""
    parts = str(meta_topic).split("/")
    value = parts[-2] if len(parts) >= 2 else "nodus"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value) or "nodus"


def _cache_path(identity_config, meta_topic):
    """Return an explicit or device-specific identity cache path."""
    configured = str(identity_config.get("cache_file") or "").strip()
    if configured:
        return configured
    return "/var/lib/weewx/nodus_identity_{}.json".format(
        _safe_device_token(meta_topic)
    )


def _read_cache(path, meta_topic):
    """Read cached identity when it belongs to the expected meta topic."""
    with open(path, "r", encoding="utf-8") as handle:
        identity = json.load(handle)
    if identity.get("meta_topic") != meta_topic:
        raise ValueError("cached Nodus identity topic does not match")
    if not identity.get("device_id") or not identity.get("version"):
        raise ValueError("cached Nodus identity is incomplete")
    if not identity.get("description"):
        identity["description"] = _sensor_description(identity["device_id"])
    return identity


def _read_fresh_cache(path, meta_topic, ttl):
    """Return cached identity while its file remains inside the configured TTL."""
    if ttl <= 0 or time.time() - os.path.getmtime(path) > ttl:
        return None
    return _read_cache(path, meta_topic)


def _write_cache(path, identity):
    """Atomically persist a successful retained-meta read."""
    temporary = "{}.tmp.{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(identity, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


class NodusIdentity(SearchList):
    """Provide ``$nodus.device_id`` and ``$nodus.version`` skin tags."""

    def __init__(self, generator):
        super().__init__(generator)
        extras = generator.skin_dict.get("Extras", {})
        fallback = {
            "device_id": str(extras.get("device_id") or _UNKNOWN),
            "version": str(extras.get("firmware_version") or _UNKNOWN),
            "description": str(
                extras.get("sensor_description") or _DEFAULT_DESCRIPTION
            ),
        }
        identity_config = generator.skin_dict.get("NodusIdentity", {})

        try:
            settings = _mqtt_settings(generator.config_dict, identity_config)
            cache_path = _cache_path(identity_config, settings["topic"])
        except Exception as exc:
            log.error("Unable to resolve Nodus identity MQTT settings: %s", exc)
            self.nodus = fallback
            return

        identity = None
        try:
            timeout = float(identity_config.get("timeout") or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        try:
            cache_ttl_value = identity_config.get("cache_ttl")
            cache_ttl = (
                _DEFAULT_CACHE_TTL
                if cache_ttl_value in (None, "")
                else float(cache_ttl_value)
            )
        except (TypeError, ValueError):
            cache_ttl = _DEFAULT_CACHE_TTL
        try:
            identity = _read_fresh_cache(
                cache_path,
                settings["topic"],
                max(0.0, cache_ttl),
            )
        except Exception:
            identity = None
        if identity is None:
            try:
                identity = _fetch_retained_meta(settings, timeout)
            except Exception as exc:
                log.warning("Unable to refresh retained Nodus identity: %s", exc)
                try:
                    identity = _read_cache(cache_path, settings["topic"])
                except Exception:
                    identity = None
            else:
                try:
                    _write_cache(cache_path, identity)
                except Exception as exc:
                    log.warning("Unable to cache retained Nodus identity: %s", exc)

        self.nodus = identity or fallback
