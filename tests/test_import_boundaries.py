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


def test_switch_location_fast_config_does_not_load_heavy_command_handlers():
    _run_import_check(
        """
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from cpynodus_ii.core.config import (
            RuntimeConfig,
            SwitchChannelConfig,
            SwitchConfig,
        )
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.features.command_intake import process_inbound_messages

        runtime_config = RuntimeConfig(
            switch=SwitchConfig(
                present=True,
                device_id="switch-x",
                channel_count=1,
                channels=(
                    SwitchChannelConfig(
                        key="SWITCH_1",
                        channel_id="S1-x",
                        label="Fan",
                    ),
                ),
            ),
        )
        switch_service = type("Obj", (), {"channels": ()})()
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/switch-x/config/set",
            (
                '{"message_id":"cfg-loc","payload":{"updates":['
                '{"section":"Switch","key":"SWITCH_LOCATION","value":"Bench"}'
                ']}}'
            ),
        )

        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "switch.toml").write_text(
                '[Switch]\\nSWITCH_LOCATION = "Old"\\n',
                encoding="utf-8",
            )
            results = process_inbound_messages(
                transport,
                runtime_config,
                switch_service,
                settings_root=tmpdir,
            )
            switch_text = Path(tmpdir, "switch.toml").read_text(encoding="utf-8")

        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected switch location result: {}".format(results))
        if 'SWITCH_LOCATION = "Bench"' not in switch_text:
            raise SystemExit("switch location was not persisted")

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


def test_sensor_location_fast_config_does_not_load_heavy_command_handlers():
    _run_import_check(
        """
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.features.command_intake import process_inbound_messages

        runtime_config = RuntimeConfig(
            sensor=DetectedSensor(
                family="i2c",
                interface="i2c",
                active_config_file="sensor_i2c.toml",
                device="co2",
                sensor_id="co2-x",
            ),
        )
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/co2-x/config/set",
            (
                '{"message_id":"cfg-loc","payload":{"updates":['
                '{"section":"Sensor","key":"LOCATION","value":"Bench"}'
                ']}}'
            ),
        )

        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "sensor_i2c.toml").write_text(
                '[Sensor]\\nDEVICE = "co2"\\nLOCATION = "Old"\\n',
                encoding="utf-8",
            )
            results = process_inbound_messages(
                transport,
                runtime_config,
                None,
                settings_root=tmpdir,
            )
            sensor_text = Path(tmpdir, "sensor_i2c.toml").read_text(
                encoding="utf-8"
            )

        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected sensor location result: {}".format(results))
        if 'LOCATION = "Bench"' not in sensor_text:
            raise SystemExit("sensor location was not persisted")

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


def test_calibration_fast_apply_does_not_load_heavy_command_handlers():
    _run_import_check(
        """
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.features.command_intake import process_inbound_messages

        runtime_config = RuntimeConfig(
            sensor=DetectedSensor(
                family="i2c",
                interface="i2c",
                active_config_file="sensor_i2c.toml",
                device="co2",
                sensor_id="co2-x",
            ),
        )
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/co2-x/calibration/set",
            (
                '{"message_id":"cal-1","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.TEMP_OFFSET","value":1.25}'
                ']}}'
            ),
        )

        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "sensor_i2c.toml").write_text(
                (
                    '[Sensor]\\nDEVICE = "co2"\\n'
                    '[Calibration.Device]\\nTEMP_OFFSET = 0.0\\n'
                ),
                encoding="utf-8",
            )
            results = process_inbound_messages(
                transport,
                runtime_config,
                None,
                settings_root=tmpdir,
            )
            sensor_text = Path(tmpdir, "sensor_i2c.toml").read_text(
                encoding="utf-8"
            )

        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected calibration result: {}".format(results))
        if "TEMP_OFFSET = 1.25" not in sensor_text:
            raise SystemExit("calibration was not persisted")

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
