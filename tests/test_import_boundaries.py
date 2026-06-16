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
            "cpynodus_ii.core.mdns_runtime",
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
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_offset_parse",
            "cpynodus_ii.features.calibration_offset_persistence",
            "cpynodus_ii.features.calibration_offsets",
            "cpynodus_ii.features.calibration_persistence",
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


def test_steady_state_import_does_not_load_command_intake():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.features.steady_state  # noqa: F401

        blocked = (
            "cpynodus_ii.features.command_intake",
            "cpynodus_ii.features.command_models",
            "cpynodus_ii.features.command_handlers",
        )
        loaded = [name for name in blocked if name in sys.modules]
        if loaded:
            raise SystemExit("unexpected imports: {}".format(",".join(loaded)))
        """
    )


def test_idle_steady_state_poll_does_not_load_command_intake():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.core.config import RuntimeConfig
        from cpynodus_ii.core.mqtt import MQTTTransport
        from cpynodus_ii.features.steady_state import run_steady_state_iteration

        transport = MQTTTransport("broker.local", 1883)
        result = run_steady_state_iteration(
            transport,
            RuntimeConfig(),
            None,
            None,
            now_monotonic=1.0,
        )
        if result.command_results != ():
            raise SystemExit("unexpected commands: {}".format(result.command_results))
        if "cpynodus_ii.features.command_intake" in sys.modules:
            raise SystemExit("command_intake loaded during idle poll")
        """
    )


def test_mqtt_network_stack_build_does_not_load_mdns_runtime():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.core.network import build_network_stack

        class Network:
            ssid = "TestWiFi"
            password = "secretpass"
            hostname = "co2-pmoopn"
            ap_ssid = "Nodus_Setup"
            ap_password = "setup-password"
            ap_channel = 6

        class RuntimeConfig:
            ap_mode = False
            mqtt_enabled = True
            web_enabled = False
            ntp_enabled = True
            network = Network()

        class Radio:
            connected = True
            hostname = ""
            ipv4_address = "10.0.0.236"

            def connect(self, ssid, password):
                self.connected = True

        class ConnMgr:
            @staticmethod
            def get_radio_socketpool(radio):
                return {"radio": radio}

            @staticmethod
            def get_radio_ssl_context(radio):
                return {"radio": radio}

        stack = build_network_stack(
            RuntimeConfig(),
            wifi_radio=Radio(),
            connection_manager_module=ConnMgr,
        )
        if stack.phase != "ready":
            raise SystemExit("unexpected phase: {}".format(stack.phase))
        if "cpynodus_ii.core.mdns_runtime" in sys.modules:
            raise SystemExit("mdns runtime loaded during MQTT stack build")
        """
    )


def test_sensor_service_import_does_not_load_soil_runtime():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.features.sensor_service  # noqa: F401

        if "cpynodus_ii.features.soil_sensor_service" in sys.modules:
            raise SystemExit("soil runtime loaded during sensor service import")
        """
    )


def test_i2c_sensor_start_does_not_load_soil_runtime():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.core.config import DetectedSensor, I2CConfig, RuntimeConfig
        from cpynodus_ii.features.sensor_service import start_sensor_service

        class BME680:
            def __init__(self, transport, *, address):
                self.transport = transport
                self.address = address
                self.pressure = 1000.0

        class BME680Module:
            Adafruit_BME680_I2C = BME680

        runtime_config = RuntimeConfig(
            sensor=DetectedSensor(
                family="i2c",
                interface="i2c",
                device="aqi",
                sensor_id="aqi-x",
                i2c=I2CConfig(address=0x77),
            ),
        )
        adapter = type(
            "Adapter",
            (),
            {"phase": "bound", "transport": object(), "errors": ()},
        )()
        sensor_runtime = type(
            "SensorRuntime",
            (),
            {"device": "aqi", "interface": "i2c", "errors": ()},
        )()

        service = start_sensor_service(
            sensor_runtime,
            adapter,
            runtime_config,
            modules={"adafruit_bme680": BME680Module},
        )
        if service.phase != "ready":
            raise SystemExit("unexpected service phase: {}".format(service.phase))
        if "cpynodus_ii.features.soil_sensor_service" in sys.modules:
            raise SystemExit("soil runtime loaded during i2c start")
        """
    )


def test_modbus_sensor_start_loads_soil_runtime():
    _run_import_check(
        """
        import sys

        from cpynodus_ii.core.config import (
            DetectedSensor,
            RuntimeConfig,
            SoilModbusConfig,
        )
        from cpynodus_ii.features.sensor_service import start_sensor_service

        class SoilTransport:
            def read_registers(self, start, count):
                return None

        runtime_config = RuntimeConfig(
            sensor=DetectedSensor(
                family="soil",
                interface="modbus_rs485",
                active_config_file="sensor_soil.toml",
                device="soil",
                sensor_id="soil-x",
                modbus=SoilModbusConfig(address=3),
            ),
        )
        adapter = type(
            "Adapter",
            (),
            {"phase": "bound", "transport": SoilTransport(), "errors": ()},
        )()
        sensor_runtime = type(
            "SensorRuntime",
            (),
            {"device": "soil", "interface": "modbus_rs485", "errors": ()},
        )()

        service = start_sensor_service(sensor_runtime, adapter, runtime_config)
        if service.phase != "ready":
            raise SystemExit("unexpected service phase: {}".format(service.phase))
        if "cpynodus_ii.features.soil_sensor_service" not in sys.modules:
            raise SystemExit("soil runtime was not loaded for modbus start")
        """
    )


def test_pico_startup_imports_do_not_load_board_profiles():
    _run_import_check(
        """
        import sys

        import cpynodus_ii.core.settings  # noqa: F401
        import cpynodus_ii.hardware.sensor_adapter  # noqa: F401

        if "cpynodus_ii.core.board_profile" in sys.modules:
            raise SystemExit("board_profile loaded during Pico startup imports")
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
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_offset_parse",
            "cpynodus_ii.features.calibration_offset_persistence",
            "cpynodus_ii.features.calibration_offsets",
            "cpynodus_ii.features.calibration_persistence",
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
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_offset_parse",
            "cpynodus_ii.features.calibration_offset_persistence",
            "cpynodus_ii.features.calibration_persistence",
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
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_offset_persistence",
            "cpynodus_ii.features.calibration_persistence",
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


def test_time_fast_config_does_not_load_heavy_command_handlers():
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
                device="aht",
                sensor_id="aht-x",
            ),
        )
        runtime_config.time.tz = "UTC"
        transport = MQTTTransport("broker.local", 1883)
        transport.receive(
            "nodus/aht-x/config/set",
            (
                '{"message_id":"cfg-time","payload":{"updates":['
                '{"section":"Time","key":"TZ","value":"America/Denver"}'
                ']}}'
            ),
        )

        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "settings.toml").write_text(
                '[Time]\\nTZ = "UTC"\\n',
                encoding="utf-8",
            )
            results = process_inbound_messages(
                transport,
                runtime_config,
                None,
                settings_root=tmpdir,
            )
            settings_text = Path(tmpdir, "settings.toml").read_text(
                encoding="utf-8"
            )

        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected time result: {}".format(results))
        if results[0].ntp_resync_requested is not True:
            raise SystemExit("time update did not request NTP resync")
        if 'TZ = "America/Denver"' not in settings_text:
            raise SystemExit("time config was not persisted")

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
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_persistence",
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


def test_calibration_fast_apply_without_persistence_does_not_load_file_writer():
    _run_import_check(
        """
        import sys

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
                '{"message_id":"cal-volatile","action":"apply","payload":{"offsets":['
                '{"key":"Calibration.Device.TEMP_OFFSET","value":1.25}'
                ']}}'
            ),
        )

        results = process_inbound_messages(transport, runtime_config, None)

        if len(results) != 1 or results[0].phase != "published":
            raise SystemExit("unexpected calibration result: {}".format(results))
        if results[0].runtime_config.sensor.calibration_device.temp_offset != 1.25:
            raise SystemExit("calibration was not applied")

        blocked = (
            "cpynodus_ii.core.settings",
            "cpynodus_ii.features.calibration_config",
            "cpynodus_ii.features.calibration_persistence",
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
