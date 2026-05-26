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
            "cpynodus_ii.core.settings",
            "cpynodus_ii.features.command_handlers",
            "cpynodus_ii.features.log_transfer",
            "cpynodus_ii.features.onboarding_state",
            "cpynodus_ii.features.payloads",
            "cpynodus_ii.ota.state",
            "cpynodus_ii.features.publish_cycle",
            "cpynodus_ii.features.runtime_config_update",
            "cpynodus_ii.features.sensor_service",
            "cpynodus_ii.features.switch_service",
            "cpynodus_ii.features.web_services",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_empty_command_poll_does_not_load_command_handlers():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.features.command_intake import process_inbound_messages

        transport = type("Transport", (), {"received_messages": []})()
        result = process_inbound_messages(transport, None, None)
        if result != ():
            raise SystemExit("unexpected result: {}".format(result))

        blocked = (
            "cpynodus_ii.core.settings",
            "cpynodus_ii.features.command_handlers",
            "cpynodus_ii.features.log_transfer",
            "cpynodus_ii.features.payloads",
            "cpynodus_ii.features.runtime_config_update",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_idle_calibration_poll_does_not_load_command_handlers():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.features.command_intake import process_soil_calibration_session

        transport = type("Transport", (), {})()
        result = process_soil_calibration_session(
            transport,
            None,
            None,
            now_monotonic=0.0,
        )
        if result.phase != "ignored":
            raise SystemExit("unexpected phase: {}".format(result.phase))

        blocked = (
            "cpynodus_ii.core.settings",
            "cpynodus_ii.features.command_handlers",
            "cpynodus_ii.features.payloads",
            "cpynodus_ii.features.runtime_config_update",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_switch_command_does_not_load_heavy_command_handlers():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.features.command_intake import process_inbound_messages

        class Handle:
            def __init__(self, value=False):
                self.value = value

        def ns(**values):
            return type("Obj", (), values)()

        runtime_config = ns(
            mqtt=ns(base_topic="nodus"),
            sensor=ns(sensor_id=""),
            switch=ns(
                device_id="switch-x",
                channels=(
                    ns(key="SWITCH_1", channel_id="S1-x", label="Fan"),
                ),
            ),
            network=ns(hostname="host-x"),
        )
        switch_service = ns(
            phase="ready",
            device_id="switch-x",
            channel_count=1,
            errors=(),
            channels=(
                ns(
                    key="SWITCH_1",
                    channel_id="S1-x",
                    phase="ready",
                    control_handle=Handle(True),
                    enable_handle=Handle(True),
                    errors=(),
                ),
            ),
        )
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/S1-x/config/set",
            (
                '{"message_id":"cfg-1","payload":{"updates":['
                '{"section":"Switch","key":"SWITCH_1_LAST_STATE","value":false}'
                ']}}'
            ),
        )

        results = process_inbound_messages(
            transport,
            runtime_config,
            switch_service,
        )
        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected switch result: {}".format(results))
        if switch_service.channels[0].control_handle.value is not False:
            raise SystemExit("switch state was not applied")

        blocked = (
            "cpynodus_ii.core.settings",
            "cpynodus_ii.features.command_handlers",
            "cpynodus_ii.features.payloads",
            "cpynodus_ii.features.runtime_config_update",
            "cpynodus_ii.ota.state",
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
