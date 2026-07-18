"""Host-side checks for the interactive WeeWX installer."""

import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "integrations" / "weewx" / "install_nodus_weewx.sh"


def _run(*args):
    return subprocess.run(
        [str(INSTALLER), *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_installer_is_valid_bash_and_has_help():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)

    output = _run("--help").stdout

    assert "/etc/weewx/nodus.conf" in output
    assert "weewx@nodus.service" in output
    assert "MQTTSubscribe 3.1.1" in output
    assert "--inspect-config PATH" in output
    assert "Preserves the primary WeeWX instance" in output
    assert "registry-only watcher" in output
    assert "Never creates or manages a WeeWX service" in output
    assert "--device-id ID" in output
    assert "--update-profile" in output


def test_installer_only_prompts_for_optional_mqtt_secret():
    script = INSTALLER.read_text(encoding="utf-8")

    assert 'prompt_secret "MQTT subscriber password"' in script
    assert "WeeWX Nodus setup password" not in script
    assert 'prompt_value "Nodus device ID' in script
    assert 'prompt_value "Sensor family' in script


def test_installer_writes_and_reuses_device_profile(tmp_path):
    profile = tmp_path / "aht-yuk0nv.toml"
    command = r'''
source "$1"
write_device_profile "$2" "aht-yuk0nv" "aht" "DevDesk" \
  "32.7622222" "-108.2372222" "1781, meter" "localhost" "1883" \
  "nodus" "mqtt-user" "mqtt-password" "false" ""
ROOT_DIR="$3"
DEVICE_ID_ARG=""
resolve_profile_device_id
'''
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "profile-test",
            str(INSTALLER),
            str(profile),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with profile.open("rb") as handle:
        saved = tomllib.load(handle)
    assert result.stdout.strip() == "aht-yuk0nv"
    assert saved["device_id"] == "aht-yuk0nv"
    assert saved["sensor_family"] == "aht"
    assert saved["station"]["description"] == "DevDesk"
    assert saved["mqtt"]["broker"] == "localhost"
    assert saved["mqtt"]["password"] == "mqtt-password"
    assert profile.stat().st_mode & 0o777 == 0o600


def test_installer_keeps_discovery_registry_only():
    script = INSTALLER.read_text(encoding="utf-8")

    assert 'local target_service="weewx@nodus.service"' in script
    assert 'local target_config="/etc/weewx/nodus.conf"' in script
    assert 'run_root systemctl enable --now "$DISCOVERY_SERVICE"' in script
    assert (
        "Discovery records WeeWX-profile devices; it does not create services."
        in script
    )
    assert "nodus-template.conf" not in script
    assert 'instances:  /etc/weewx/nodus-<device_id>.conf' not in script
    assert 'disable "$target_service"' not in script


def test_installer_disables_only_registry_managed_obsolete_instances(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(
        """{
  "devices": {
    "aht-yuk0nv": {"service": "weewx@nodus-aht-yuk0nv.service"},
    "unsafe": {"service": "weewx.service"}
  }
}
""",
        encoding="utf-8",
    )
    command = r'''
source "$1"
SUDO=()
run_root() { printf '%s\n' "$*"; }
disable_discovery_managed_instances "$2"
'''
    output = subprocess.run(
        ["bash", "-c", command, "migration-test", str(INSTALLER), str(registry)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "disable --now weewx@nodus-aht-yuk0nv.service" in output
    assert "weewx.service" not in output


def test_installer_detects_physical_station_without_overwriting_it(tmp_path):
    config = tmp_path / "weewx.conf"
    config.write_text(
        """
[Station]
    station_type = AcuRite

[AcuRite]
    driver = weewx.drivers.acurite
""",
        encoding="utf-8",
    )

    output = _run("--inspect-config", config).stdout

    assert "mode=separate" in output
    assert "station_type=AcuRite" in output
    assert "driver=weewx.drivers.acurite" in output


def test_installer_preserves_main_instance_for_simulator_or_mqtt(tmp_path):
    for station_type, driver in (
        ("Simulator", "weewx.drivers.simulator"),
        ("MQTTSubscribeDriver", "user.MQTTSubscribe"),
    ):
        config = tmp_path / f"{station_type}.conf"
        config.write_text(
            """
[Station]
    station_type = {station_type}

[{station_type}]
    driver = {driver}
""".format(station_type=station_type, driver=driver),
            encoding="utf-8",
        )

        assert "mode=separate" in _run("--inspect-config", config).stdout


def test_installer_renders_complete_nodus_driver_configuration(tmp_path):
    output = tmp_path / "nodus.conf"
    command = r'''
source "$1"
build_field_stanzas aqi
render_config "$2" "5.2.0" "Greenhouse" "32.79" "-108.27" \
  "1782, meter" "nodus.sdb" "/var/www/html/weewx/nodus" \
  "broker.local" "1883" "reader" 'p"ass' "false" "" \
  "nodus/aqi-example/data" "user.MQTTSubscribe" \
  "operator" "admin-secret" "8767"
'''
    subprocess.run(
        ["bash", "-c", command, "installer-test", str(INSTALLER), str(output)],
        check=True,
    )
    config = output.read_text(encoding="utf-8")

    assert "station_type = MQTTSubscribeDriver" in config
    assert "[[Nodus]]" in config
    assert "skin = Nodus" in config
    assert "driver = user.MQTTSubscribe" in config
    assert "database_name = nodus.sdb" in config
    assert "schema = user.nodus_schema.schema" in config
    assert (
        "prep_services = weewx.engine.StdTimeSynch, user.nodus_units.NodusUnits"
        in config
    )
    assert "[[[nodus/aqi-example/data]]]" in config
    assert "[[[[values_Air Quality]]]]" in config
    assert "name = airQuality" in config
    assert 'password = "p\\"ass"' in config
    assert "user.nodus_automation.NodusAutomation" in config
    assert "user.nodus_switch.NodusSwitchStatus" in config
    assert "[NodusAutomation]" in config
    assert "[NodusSwitchStatus]" in config
    assert "status_file = /var/lib/weewx/nodus_switch.json" in config
    assert "control_file = /var/lib/weewx/nodus_switch_control.json" in config
    assert "max_events = 20" in config
    assert "rules_file = /var/lib/weewx/nodus_automation_rules.json" in config
    assert "runtime_file = /var/lib/weewx/nodus_automation_runtime.json" in config
    assert "admin_enabled = true" in config
    assert "admin_require_auth = false" in config
    assert 'admin_username = "operator"' in config
    assert 'admin_password = "admin-secret"' in config
    assert "admin_web_root = /etc/weewx/skins/Nodus/admin" in config
    assert "dashboard_web_root = /var/www/html/weewx/nodus" in config


def test_installer_maps_sensor_families_to_expected_observations():
    command = r'''
source "$1"
for family in aht avpd co2 aqi soil; do
  build_field_stanzas "$family"
  printf '%s\n%s\n--END--\n' "$family" "$FIELD_STANZAS"
done
'''
    output = subprocess.run(
        ["bash", "-c", command, "installer-test", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "name = vpd" in output
    assert "name = co2" in output
    assert "name = airQuality" in output
    assert "name = soilMoisturePct" in output


def test_installer_maps_aht_without_pressure():
    command = r'''
source "$1"
build_field_stanzas aht
printf '%s\n' "$FIELD_STANZAS"
'''
    output = subprocess.run(
        ["bash", "-c", command, "installer-test", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "name = inTemp" in output
    assert "values_Baro-Pressure" not in output


def test_installer_does_not_misclassify_lux_as_environmental():
    command = r'''
source "$1"
infer_family lux-example
'''
    output = subprocess.run(
        ["bash", "-c", command, "installer-test", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert output.strip() == "lux"


def test_installer_exit_trap_restores_prior_nodus_config(tmp_path):
    target = tmp_path / "nodus.conf"
    backup = tmp_path / "nodus.conf.backup"
    fake_sudo = tmp_path / "sudo"
    target.write_text("replacement\n", encoding="utf-8")
    backup.write_text("prior\n", encoding="utf-8")
    fake_sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    fake_sudo.chmod(0o755)
    command = r'''
source "$1"
INSTALL_TARGET_CONFIG="$2"
INSTALL_BACKUP_CONFIG="$3"
INSTALL_ROLLBACK_NEEDED=1
exit 7
'''

    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "installer-test",
            str(INSTALLER),
            str(target),
            str(backup),
        ],
        capture_output=True,
        env={**os.environ, "PATH": "{}:{}".format(tmp_path, os.environ["PATH"])},
        text=True,
    )

    assert result.returncode == 7
    assert target.read_text(encoding="utf-8") == "prior\n"
    assert "restoring the prior Nodus configuration" in result.stderr


def test_installer_validation_uses_debian_weewx_python_path(tmp_path):
    python_root = tmp_path / "share" / "weewx"
    (python_root / "weeutil").mkdir(parents=True)
    command = r'''
source "$1"
WEEWX_PYTHON_ROOT="$2"
WEEWX_USER_ROOT="/etc/weewx/bin/user"
run_weewx() {
  printf '%s\n' "$@"
}
validate_mqttsubscribe_driver /etc/weewx/nodus.conf
'''

    output = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "installer-test",
            str(INSTALLER),
            str(python_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "PYTHONPATH={}".format(python_root) in output
    assert "/etc/weewx/bin/user/MQTTSubscribe.py" in output
    assert "configure\ndriver\n--validate\n--conf\n/etc/weewx/nodus.conf" in output
