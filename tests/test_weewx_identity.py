"""Tests for the host-side WeeWX retained Nodus identity extension."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_identity.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_identity", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    return {
        "MQTTSubscribeDriver": {
            "host": "sensorius-hub-1.local",
            "port": "1883",
            "username": "weewx-reader",
            "password": "secret",
            "tls": {"enable": "false"},
            "topics": {
                "message": {"type": "json"},
                "nodus/aht-va41ka/data": {"subscribe": "true"},
            },
        }
    }


def _generator(tmp_path):
    return SimpleNamespace(
        config_dict=_config(),
        skin_dict={
            "Extras": {
                "device_id": "unknown",
                "firmware_version": "unknown",
            },
            "NodusIdentity": {
                "cache_file": str(tmp_path / "identity.json"),
                "timeout": "1",
            },
        },
    )


def test_identity_derives_retained_meta_topic_and_broker_settings():
    module = _load_module()

    settings = module._mqtt_settings(_config(), {})

    assert settings == {
        "host": "sensorius-hub-1.local",
        "port": 1883,
        "username": "weewx-reader",
        "password": "secret",
        "tls": False,
        "ca_certs": "",
        "topic": "nodus/aht-va41ka/meta",
    }


def test_identity_supports_mqttsubscribe_service_mode():
    module = _load_module()
    config = _config()
    config["MQTTSubscribeService"] = config.pop("MQTTSubscribeDriver")

    settings = module._mqtt_settings(config, {})

    assert settings["topic"] == "nodus/aht-va41ka/meta"
    assert settings["host"] == "sensorius-hub-1.local"


def test_identity_explicit_topic_disambiguates_multiple_nodus_devices():
    module = _load_module()
    config = _config()
    config["MQTTSubscribeDriver"]["topics"]["nodus/bme-abc123/data"] = {
        "subscribe": "true"
    }

    settings = module._mqtt_settings(
        config, {"meta_topic": "nodus/aht-va41ka/meta"}
    )

    assert settings["topic"] == "nodus/aht-va41ka/meta"


def test_identity_validates_retained_nodus_meta_payload():
    module = _load_module()
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": "aht-va41ka",
            "version": "v0.26.198.2",
        }
    ).encode("utf-8")

    identity = module._identity_from_payload(payload, "nodus/aht-va41ka/meta")

    assert identity["device_id"] == "aht-va41ka"
    assert identity["version"] == "v0.26.198.2"
    assert identity["description"] == (
        "Indoor environment + VPD + disease-risk indicators."
    )
    assert identity["meta_topic"] == "nodus/aht-va41ka/meta"


def test_identity_uses_sensor_specific_descriptions():
    module = _load_module()

    assert module._sensor_description("avpd-123") == (
        "Indoor environment + VPD + disease-risk indicators."
    )
    assert module._sensor_description("co2-123") == (
        "Outdoor environment + CO2 + VPD + disease-risk indicators."
    )
    assert module._sensor_description("aqi-123") == (
        "Indoor environment + AQI + VPD + disease-risk indicators."
    )
    assert module._sensor_description("soil-123") == (
        "Soil Moisture + Temperature"
    )
    assert module._sensor_description(
        "custom-123", {"sensor": {"hardware": "AHTx0"}}
    ) == "Indoor environment + VPD + disease-risk indicators."


def test_identity_search_list_refreshes_and_caches_retained_meta(
    tmp_path, monkeypatch
):
    module = _load_module()
    expected = {
        "device_id": "aht-va41ka",
        "version": "v0.26.198.2",
        "description": "Indoor environment + VPD + disease-risk indicators.",
        "meta_topic": "nodus/aht-va41ka/meta",
        "updated": 123,
    }
    monkeypatch.setattr(
        module, "_fetch_retained_meta", lambda settings, timeout: expected
    )

    extension = module.NodusIdentity(_generator(tmp_path))

    assert extension.nodus == expected
    assert json.loads((tmp_path / "identity.json").read_text()) == expected


def test_identity_search_list_uses_cache_when_broker_is_unavailable(
    tmp_path, monkeypatch
):
    module = _load_module()
    expected = {
        "device_id": "aht-va41ka",
        "version": "v0.26.197.5",
        "description": "Indoor environment + VPD + disease-risk indicators.",
        "meta_topic": "nodus/aht-va41ka/meta",
        "updated": 123,
    }
    (tmp_path / "identity.json").write_text(json.dumps(expected), encoding="utf-8")

    def unavailable(_settings, _timeout):
        raise OSError("broker unavailable")

    monkeypatch.setattr(module, "_fetch_retained_meta", unavailable)

    extension = module.NodusIdentity(_generator(tmp_path))

    assert extension.nodus == expected


def test_identity_search_list_keeps_live_identity_when_cache_write_fails(
    tmp_path, monkeypatch
):
    module = _load_module()
    expected = {
        "device_id": "aht-va41ka",
        "version": "v0.26.198.2",
        "description": "Indoor environment + VPD + disease-risk indicators.",
        "meta_topic": "nodus/aht-va41ka/meta",
        "updated": 123,
    }
    monkeypatch.setattr(
        module, "_fetch_retained_meta", lambda settings, timeout: expected
    )
    monkeypatch.setattr(
        module,
        "_write_cache",
        lambda path, identity: (_ for _ in ()).throw(PermissionError(path)),
    )

    extension = module.NodusIdentity(_generator(tmp_path))

    assert extension.nodus == expected


def test_identity_search_list_falls_back_to_skin_defaults_without_cache(
    tmp_path, monkeypatch
):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_fetch_retained_meta",
        lambda _settings, _timeout: (_ for _ in ()).throw(OSError("offline")),
    )

    extension = module.NodusIdentity(_generator(tmp_path))

    assert extension.nodus == {
        "device_id": "unknown",
        "version": "unknown",
        "description": "Sensor conditions",
    }
