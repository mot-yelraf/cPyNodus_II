"""Tests for passive Nodus WeeWX device discovery."""

import importlib.util
import json
import time
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_discovery.py"
)
UNIT_PATH = MODULE_PATH.parents[2] / "nodus-weewx-discovery.service"


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_discovery", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _meta(device_id="aht-yuk0nv", device="aht", hardware="AHTx0"):
    return json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": device_id,
            "version": "v0.26.199.1",
            "profile": {"active_profile": "weewx"},
            "sensor": {
                "device": device,
                "sensor_id": device_id,
                "hardware": hardware,
                "location": "Greenhouse",
                "data_topic": "nodus/{}/data".format(device_id),
            },
            "switch": {
                "channel_count": 1,
                "meta_topic": "nodus/{}/meta/switch".format(device_id),
            },
        }
    )


def test_discovery_accepts_only_matching_weewx_retained_metadata():
    module = _load_module()

    descriptor = module.parse_meta(_meta(), "nodus/aht-yuk0nv/meta", "nodus")

    assert descriptor["device_id"] == "aht-yuk0nv"
    assert descriptor["family"] == "aht"
    assert descriptor["data_topic"] == "nodus/aht-yuk0nv/data"
    assert descriptor["switch_meta_topic"] == "nodus/aht-yuk0nv/meta/switch"

    document = json.loads(_meta())
    document["profile"]["active_profile"] = "sensorius"
    try:
        module.parse_meta(json.dumps(document), "nodus/aht-yuk0nv/meta", "nodus")
    except ValueError as exc:
        assert "weewx profile" in str(exc)
    else:
        raise AssertionError("non-WeeWX metadata was accepted")


def test_discovery_uses_hardware_fallback_for_older_aht_metadata():
    module = _load_module()
    document = json.loads(_meta())
    document["sensor"].pop("device")

    descriptor = module.parse_meta(
        json.dumps(document), "nodus/aht-yuk0nv/meta", "nodus"
    )

    assert descriptor["family"] == "aht"


def test_manager_records_devices_without_provisioning_services(tmp_path):
    module = _load_module()
    registry = tmp_path / "registry.json"
    manager = module.DiscoveryManager(
        {
            "registry_file": str(registry),
            "max_devices": 32,
        }
    )

    first = module.parse_meta(_meta(), "nodus/aht-yuk0nv/meta", "nodus")
    second = module.parse_meta(
        _meta("aqi-other", "aqi", "BME680"),
        "nodus/aqi-other/meta",
        "nodus",
    )
    first_entry = manager.register(first)
    manager.register(second)

    saved = json.loads(registry.read_text())
    assert first_entry["device_id"] == "aht-yuk0nv"
    assert set(saved["devices"]) == {"aht-yuk0nv", "aqi-other"}
    assert saved["devices"]["aht-yuk0nv"]["data_topic"] == ("nodus/aht-yuk0nv/data")
    assert "service" not in saved["devices"]["aht-yuk0nv"]
    assert "config" not in saved["devices"]["aht-yuk0nv"]
    assert "admin_port" not in saved["devices"]["aht-yuk0nv"]


def test_manager_migrates_old_provisioning_entry_to_registry_only(tmp_path):
    module = _load_module()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "nodus-weewx-discovery/v1",
                "devices": {
                    "aht-yuk0nv": {
                        "device_id": "aht-yuk0nv",
                        "service": "weewx@nodus-aht-yuk0nv.service",
                        "config": "/etc/weewx/nodus-aht-yuk0nv.conf",
                        "admin_port": 8767,
                        "first_seen": 100,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = module.DiscoveryManager({"registry_file": str(registry)})
    descriptor = module.parse_meta(_meta(), "nodus/aht-yuk0nv/meta", "nodus")
    entry = manager.register(descriptor)

    assert entry["first_seen"] == 100
    assert "service" not in entry
    assert "config" not in entry
    assert "admin_port" not in entry


def test_discovery_module_has_no_service_management_capability():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "systemctl" not in source
    assert "render_device_config" not in source
    assert "weewx@nodus-" not in source


def test_discovery_systemd_unit_runs_unprivileged():
    unit = UNIT_PATH.read_text(encoding="utf-8")

    assert "User=weewx" in unit
    assert "Group=weewx" in unit
    assert "NoNewPrivileges=true" in unit
    assert "provision isolated WeeWX instances" not in unit


def test_system_settings_round_trip_in_host_toml(tmp_path):
    module = _load_module()
    path = tmp_path / "nodus_system.toml"

    module.write_system_settings(
        path,
        {
            "title": "Nodus Automation Instrumentorum",
            "online_timeout_seconds": 210,
            "auto_provision": False,
        },
    )

    assert module.read_system_settings(path) == {
        "title": "Nodus Automation Instrumentorum",
        "online_timeout_seconds": 210,
        "auto_provision": False,
    }


def test_manager_resolves_broker_ipv4_and_prefers_configured_timezone(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args: [
            (
                module.socket.AF_INET,
                module.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.248", 0),
            )
        ],
    )

    assert module._broker_ipv4("mqtt.local") == "10.0.0.248"
    assert module._host_timezone("America/Denver") == "America/Denver"


def test_manager_removes_exact_device_switch_and_channel_retained_topics(tmp_path):
    module = _load_module()
    registry_path = tmp_path / "registry.json"
    discovery = module.DiscoveryManager({"registry_file": str(registry_path)})
    discovery.registry["devices"]["aht-va41ka"] = {
        "device_id": "aht-va41ka",
        "switch_id": "switch-va41ka",
        "data_topic": "nodus/aht-va41ka/data",
        "switch_meta_topic": "nodus/aht-va41ka/meta/switch",
        "heartbeat_topic": "nodus/aht-va41ka/status/heartbeat",
    }
    discovery._save_registry()

    published = []

    class Result:
        rc = 0

        def wait_for_publish(self, timeout=None):
            return timeout

        def is_published(self):
            return True

    class Client:
        def publish(self, topic, payload, qos, retain):
            published.append((topic, payload, qos, retain))
            return Result()

    manager = module.NodusManager.__new__(module.NodusManager)
    manager.settings = {"base_topic": "nodus"}
    manager.discovery = discovery
    manager.discovery_service = type("Service", (), {"client": Client()})()
    manager.installed_file = str(tmp_path / "installed.json")
    manager.live_seen = {"aht-va41ka": time.time()}
    manager.topic_ids = {"aht-va41ka": {"nodus/S1-va41ka/state"}}

    result = manager.remove_devices(["aht-va41ka"])

    topics = {item[0] for item in published}
    assert "nodus/aht-va41ka/meta" in topics
    assert "nodus/switch-va41ka/meta" in topics
    assert "nodus/S1-va41ka/state" in topics
    assert all(item[1:] == ("", 0, True) for item in published)
    assert result["results"]["aht-va41ka"]["installed"] is False
    assert discovery.registry["devices"] == {}
