"""Host-side checks for the interactive WeeWX installer."""

import os
import subprocess
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

    assert "weewx@nodus.service" in output
    assert "MQTTSubscribe 3.1.1" in output
    assert "--inspect-config PATH" in output
    assert "Preserves the primary WeeWX instance" in output
    assert "Restores the prior Nodus configuration" in output


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
  "nodus/aqi-example/data" "user.MQTTSubscribe"
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
    assert "[NodusAutomation]" in config
    assert "condition_1 = inTemp, above, 27.0, 25.0" in config


def test_installer_maps_sensor_families_to_expected_observations():
    command = r'''
source "$1"
for family in avpd co2 aqi soil; do
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
