"""Tests for the fixed root-side Nodus WeeWX manager actions."""

import importlib.util
import json
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_manager_helper.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nodus_manager_helper", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helper_installs_only_registry_device_into_managed_template(
    tmp_path, monkeypatch
):
    module = _load_module()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "devices": {
                    "aht-va41ka": {
                        "data_topic": "nodus/aht-va41ka/data",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    template = tmp_path / "template.conf"
    template.write_text(
        "[[[__NODUS_DATA_TOPIC__]]]\ndevice = __NODUS_DEVICE_ID__\n",
        encoding="utf-8",
    )
    target = tmp_path / "nodus.conf"
    installed = tmp_path / "installed.json"
    calls = []
    monkeypatch.setattr(module, "_systemctl", lambda *args: calls.append(args))

    result = module._install(
        {
            "registry_file": str(registry),
            "template_config": str(template),
            "operational_config": str(target),
            "installed_file": str(installed),
            "operational_service": "weewx@nodus.service",
        },
        "aht-va41ka",
    )

    assert "[[[nodus/aht-va41ka/data]]]" in target.read_text(encoding="utf-8")
    assert json.loads(installed.read_text())["device_id"] == "aht-va41ka"
    assert calls == [
        ("enable", "weewx@nodus.service"),
        ("restart", "weewx@nodus.service"),
    ]
    assert result["service"] == "restarted"


def test_helper_rejects_install_for_device_missing_from_registry(tmp_path):
    module = _load_module()
    registry = tmp_path / "registry.json"
    registry.write_text('{"devices":{}}', encoding="utf-8")

    try:
        module._install(
            {
                "registry_file": str(registry),
                "template_config": str(tmp_path / "template.conf"),
                "operational_config": str(tmp_path / "nodus.conf"),
                "installed_file": str(tmp_path / "installed.json"),
                "operational_service": "weewx@nodus.service",
            },
            "aht-va41ka",
        )
    except ValueError as exc:
        assert "discovery registry" in str(exc)
    else:
        raise AssertionError("unknown discovery device was installed")


def test_helper_removes_only_managed_instance_files(tmp_path, monkeypatch):
    module = _load_module()
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    managed = {
        "operational_config": tmp_path / "nodus.conf",
        "database": tmp_path / "nodus.sdb",
        "status_file": tmp_path / "status.json",
        "installed_file": tmp_path / "installed.json",
    }
    for path in managed.values():
        path.write_text("managed", encoding="utf-8")
    managed["installed_file"].write_text('{"device_id":"aht-va41ka"}', encoding="utf-8")
    (dashboard / "index.html").write_text("old report", encoding="utf-8")
    (dashboard / "micro_vpd.png").write_bytes(b"old graph")
    (dashboard / "style.css").write_text("keep", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    result = module._remove(
        {
            **{key: str(value) for key, value in managed.items()},
            "operational_service": "weewx@nodus.service",
            "dashboard_web_root": str(dashboard),
        },
        "aht-va41ka",
    )

    assert all(not path.exists() for path in managed.values())
    assert not (dashboard / "index.html").exists()
    assert not (dashboard / "micro_vpd.png").exists()
    assert (dashboard / "style.css").read_text(encoding="utf-8") == "keep"
    assert [call[0][:2] for call in calls] == [
        ["systemctl", "stop"],
        ["systemctl", "disable"],
    ]
    assert result["service"] == "stopped"
