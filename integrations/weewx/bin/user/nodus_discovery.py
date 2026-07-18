"""Discover WeeWX-profile Nodus devices and maintain a passive registry."""

import argparse
import json
import logging
import os
import re
import tempfile
import time

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_TOPIC = re.compile(r"^[A-Za-z0-9_./+-]{1,240}$")
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
        switch_meta_topic = _clean_topic(
            switch_meta_topic, "switch.meta_topic"
        )
    elif int(switch.get("channel_count") or 0) > 0:
        switch_meta_topic = "{}/{}/meta/switch".format(base_topic, device_id)

    return {
        "device_id": device_id,
        "family": family,
        "hardware": str(sensor.get("hardware") or "").strip(),
        "sensor_id": sensor_id,
        "data_topic": data_topic,
        "switch_meta_topic": switch_meta_topic,
        "switch_count": max(0, int(switch.get("channel_count") or 0)),
        "location": str(sensor.get("location") or "").strip(),
        "version": str(data.get("version") or "").strip(),
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
        if (
            device_id not in self.registry["devices"]
            and len(self.registry["devices"])
            >= int(self.settings.get("max_devices") or 32)
        ):
            raise ValueError("maximum discovered device count reached")
        previous = self.registry["devices"].get(device_id) or {}
        entry = dict(descriptor)
        now = int(time.time())
        entry["first_seen"] = int(previous.get("first_seen") or now)
        entry["last_seen"] = now
        self.registry["devices"][device_id] = entry
        self._save_registry()
        return entry


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

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if int(reason_code) == 0:
            base = _clean_topic(self.settings.get("base_topic"), "base_topic")
            client.subscribe("{}/+/meta".format(base), qos=0)
        else:
            log.error("Nodus discovery MQTT connect failed: %s", reason_code)

    def _on_message(self, _client, _userdata, message):
        try:
            descriptor = parse_meta(
                message.payload, message.topic, self.settings.get("base_topic")
            )
            self.manager.register(descriptor)
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


def main(argv=None):
    """Run the standalone Nodus WeeWX discovery service."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/weewx/nodus-discovery.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with open(args.config, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
    DiscoveryService(settings).run()


if __name__ == "__main__":
    main()
