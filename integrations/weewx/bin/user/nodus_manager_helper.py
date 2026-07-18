"""Execute fixed root-owned Nodus WeeWX install/remove requests."""

import argparse
import glob
import json
import os
import re
import subprocess
import tempfile

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manager document must be an object")
    return value


def _atomic(path, document, mode=0o664):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".nodus-manager-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _systemctl(*args):
    subprocess.run(["systemctl", *args], check=True, timeout=45)


def _install(settings, device_id):
    registry = _read(settings["registry_file"])
    entry = (registry.get("devices") or {}).get(device_id)
    if not isinstance(entry, dict):
        raise ValueError("device is not present in the discovery registry")
    template_family = str(settings.get("template_family") or "").strip().lower()
    device_family = str(entry.get("family") or "").strip().lower()
    if template_family and device_family != template_family:
        raise ValueError(
            "discovered sensor family does not match the managed WeeWX template"
        )
    topic = str(entry.get("data_topic") or "").strip()
    if not topic.endswith("/data") or "+" in topic or "#" in topic:
        raise ValueError("discovered data topic is invalid")
    with open(settings["template_config"], "r", encoding="utf-8") as handle:
        config = handle.read()
    if "__NODUS_DATA_TOPIC__" not in config:
        raise ValueError("managed WeeWX template is missing its topic marker")
    config = config.replace("__NODUS_DATA_TOPIC__", topic)
    config = config.replace("__NODUS_DEVICE_ID__", device_id)
    target = settings["operational_config"]
    descriptor, temporary = tempfile.mkstemp(
        prefix=".nodus-conf-", dir=os.path.dirname(target)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(config)
        os.chmod(temporary, 0o660)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _atomic(settings["installed_file"], {"device_id": device_id, "data_topic": topic})
    _systemctl("enable", settings["operational_service"])
    _systemctl("restart", settings["operational_service"])
    return {"service": "restarted", "config": target}


def _remove(settings, device_id):
    installed_path = settings["installed_file"]
    try:
        installed = _read(installed_path)
    except OSError:
        installed = {}
    if installed.get("device_id") != device_id:
        raise ValueError("selected device is not the installed Nodus")
    service = settings["operational_service"]
    subprocess.run(["systemctl", "stop", service], check=False, timeout=45)
    subprocess.run(["systemctl", "disable", service], check=False, timeout=45)
    removed = []
    fixed = [
        settings.get("operational_config"),
        settings.get("database"),
        settings.get("status_file"),
        settings.get("control_file"),
        settings.get("rules_file"),
        settings.get("runtime_file"),
        settings.get("automation_status_file"),
        installed_path,
    ]
    identity_pattern = "/var/lib/weewx/nodus_identity_{}.json".format(device_id)
    for path in [value for value in fixed if value] + [identity_pattern]:
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
    dashboard = str(settings.get("dashboard_web_root") or "")
    if dashboard and os.path.abspath(dashboard) != "/":
        for pattern in ("index.html", "micro_*.png"):
            for path in glob.glob(os.path.join(dashboard, pattern)):
                if os.path.isfile(path):
                    os.unlink(path)
                    removed.append(path)
    return {"service": "stopped", "removed": removed}


def run(config_path):
    settings = _read(config_path)
    request_path = settings["request_file"]
    result_path = settings["result_file"]
    request = _read(request_path)
    token = str(request.get("token") or "")
    action = str(request.get("action") or "")
    device_id = str(request.get("device_id") or "")
    result = {"ok": False, "token": token, "action": action, "device_id": device_id}
    try:
        if not token or not _SAFE_ID.fullmatch(device_id):
            raise ValueError("manager request is invalid")
        if action == "install":
            result.update(_install(settings, device_id))
        elif action == "remove":
            result.update(_remove(settings, device_id))
        else:
            raise ValueError("manager action is invalid")
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        try:
            os.unlink(request_path)
        except FileNotFoundError:
            pass
        _atomic(result_path, result)
    return 0 if result["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/weewx/nodus-discovery.json")
    args = parser.parse_args(argv)
    raise SystemExit(run(args.config))


if __name__ == "__main__":
    main()
