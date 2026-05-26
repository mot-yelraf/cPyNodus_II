import subprocess
import sys
import textwrap


def _run_import_check(source):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_app_import_does_not_load_mqtt_or_feature_runtimes():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.app  # noqa: F401

        blocked = (
            "cpynodus_ii.core.mqtt",
            "cpynodus_ii.core.mqtt_client",
            "cpynodus_ii.features.command_intake",
            "cpynodus_ii.features.steady_state",
            "cpynodus_ii.features.switch_service",
            "cpynodus_ii.features.web_services",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_command_intake_import_does_not_load_web_or_hardware_services():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.features.command_intake  # noqa: F401

        blocked = (
            "cpynodus_ii.ota.state",
            "cpynodus_ii.features.publish_cycle",
            "cpynodus_ii.features.sensor_service",
            "cpynodus_ii.features.switch_service",
            "cpynodus_ii.features.web_services",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_runtime_update_import_does_not_load_mqtt_command_or_ota():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.features.runtime_config_update  # noqa: F401

        blocked = (
            "cpynodus_ii.ota.state",
            "cpynodus_ii.features.command_intake",
            "cpynodus_ii.features.publish_cycle",
            "cpynodus_ii.features.web_services",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_web_handlers_import_does_not_load_command_or_hardware_services():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.features.web_handlers  # noqa: F401

        blocked = (
            "cpynodus_ii.features.command_intake",
            "cpynodus_ii.features.sensor_service",
            "cpynodus_ii.features.switch_service",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )
