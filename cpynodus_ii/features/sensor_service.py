"""Own sensor-driver startup and snapshot collection over hardware adapters.

The sensor service layer instantiates device drivers, reads normalized
snapshots, applies calibration and derived metrics, and reports service state
back to the rest of the runtime.
"""

from dataclasses import dataclass
from time import sleep

from cpynodus_ii.features.derived_metrics import enrich_metrics, estimate_dli_from_ppfd


@dataclass(frozen=True)
class SensorSnapshot:
    """Describe one normalized sensor reading snapshot."""

    phase: str
    sensor_id: str
    device: str
    metrics: dict
    errors: tuple = ()


@dataclass(frozen=True)
class SensorService:
    """Describe the active sensor service state."""

    phase: str
    device: str
    interface: str
    driver_kind: str
    driver: object | None = None
    transport: object | None = None
    secondary_transport: object | None = None
    errors: tuple = ()


@dataclass(frozen=True)
class DualI2CSensorDriver:
    """Capture ambient and plant I2C drivers for dual-sensor VPD devices."""

    ambient: object
    plant: object


class SoilModbusClient:
    """Provide minimal Modbus register reads over a UART transport."""

    def __init__(self, uart_transport, *, address, channel_name=""):
        self.uart_transport = uart_transport
        self.address = int(address or 1)
        self.channel_name = str(channel_name or "").strip().upper()

    def read_registers(self, start, count):
        """Read one or more holding registers from the configured sensor."""
        request = bytes(
            [
                self.address & 0xFF,
                0x03,
                (int(start) >> 8) & 0xFF,
                int(start) & 0xFF,
                (int(count) >> 8) & 0xFF,
                int(count) & 0xFF,
            ]
        )
        crc = _modbus_crc16(request)
        self.uart_transport.write(request + bytes([crc & 0xFF, (crc >> 8) & 0xFF]))
        sleep(0.05)
        response = self.uart_transport.read(5 + (int(count) * 2))
        registers = _parse_modbus_register_response(
            response, address=self.address, count=count
        )
        if registers is None:
            return None
        if int(count) == 1:
            return registers[0]
        return registers

    def deinit(self):
        """Keep shutdown compatible with the generic service stop path."""
        return None


def start_sensor_service(
    sensor_runtime, sensor_adapter, runtime_config, *, modules=None
):
    """Create a sensor service from a bound hardware adapter."""
    if sensor_adapter.phase != "bound":
        return SensorService(
            phase=sensor_adapter.phase,
            device=sensor_runtime.device,
            interface=sensor_runtime.interface,
            driver_kind="",
            transport=sensor_adapter.transport,
            errors=sensor_adapter.errors or sensor_runtime.errors,
        )

    modules = modules or {}
    sensor = runtime_config.sensor
    transport = sensor_adapter.transport

    if sensor_runtime.interface == "i2c":
        try:
            return _start_i2c_sensor_service(sensor, sensor_adapter, modules)
        except Exception as exc:
            return _sensor_service_error(
                sensor.device,
                sensor.interface,
                transport,
                "sensor_not_found",
                exc,
            )

    if sensor_runtime.interface == "modbus_rs485":
        driver = transport
        if isinstance(transport, tuple):
            driver = tuple(
                (
                    channel,
                    item
                    if hasattr(item, "read_registers")
                    else SoilModbusClient(
                        item,
                        address=getattr(channel, "address", 1),
                        channel_name=getattr(channel, "name", ""),
                    ),
                )
                for channel, item in transport
            )
        elif transport is not None and not hasattr(transport, "read_registers"):
            channels = getattr(getattr(sensor, "modbus", None), "channels", ())
            channel = tuple(channels or (None,))[0]
            fallback_address = getattr(getattr(sensor, "modbus", None), "address", 1)
            driver = SoilModbusClient(
                transport,
                address=getattr(channel, "address", fallback_address),
                channel_name=getattr(channel, "name", ""),
            )
        return SensorService(
            phase="ready",
            device=sensor.device,
            interface=sensor.interface,
            driver_kind="soil_modbus_uart",
            driver=driver,
            transport=transport,
            errors=(),
        )

    return SensorService(
        phase="error",
        device=sensor.device,
        interface=sensor.interface,
        driver_kind="",
        transport=transport,
        errors=("unsupported_sensor_service_interface",),
    )


def stop_sensor_service(sensor_service):
    """Deinitialize the sensor service transport if supported."""
    _safe_deinit(getattr(sensor_service, "driver", None))
    transport = getattr(sensor_service, "transport", None)
    if transport is not getattr(sensor_service, "driver", None):
        _safe_deinit(transport)
    _safe_deinit(getattr(sensor_service, "secondary_transport", None))


def read_sensor_snapshot(sensor_service, runtime_config):
    """Read a normalized sensor snapshot from an active sensor service."""
    sensor = runtime_config.sensor
    if sensor_service.phase != "ready":
        return SensorSnapshot(
            phase=sensor_service.phase,
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics={},
            errors=sensor_service.errors,
        )

    try:
        return _read_ready_sensor_snapshot(sensor_service, runtime_config, sensor)
    except Exception as exc:
        error = "sensor_read_failed"
        if _is_sensor_not_found_exception(exc):
            error = "sensor_not_found"
        return _sensor_snapshot_error(sensor, error, exc)


def _read_ready_sensor_snapshot(sensor_service, runtime_config, sensor):
    if sensor.device == "aqi":
        temp_c = _apply_linear_calibration(
            getattr(sensor_service.driver, "temperature", None),
            sensor.calibration_system.temp_offset,
            sensor.calibration_device.temp_offset,
        )
        rh_pct = _apply_linear_calibration(
            getattr(sensor_service.driver, "humidity", None),
            sensor.calibration_system.rh_offset,
            sensor.calibration_device.rh_offset,
        )
        gas_ohms = _apply_linear_calibration(
            getattr(sensor_service.driver, "gas", None),
            0.0,
            sensor.calibration_device.gas_offset,
        )
        metrics = _compact_metrics(
            {
                "Temperature": _maybe_round(temp_c, 2),
                "Rel-Humidity": _maybe_round(rh_pct, 0),
                "Baro-Pressure": _maybe_round(
                    _scale_pressure_hpa(
                        getattr(sensor_service.driver, "pressure", None)
                    ),
                    0,
                ),
                "Gas": _maybe_round(gas_ohms, 0),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        _apply_post_enrichment_calibration(metrics, sensor)
        return _ready_sensor_snapshot(sensor, metrics)

    if sensor.device == "co2":
        driver_kind = getattr(sensor_service, "driver_kind", "")
        if driver_kind in {"adafruit_scd30", "adafruit_scd4x"}:
            if not _sensor_data_ready(sensor_service.driver, driver_kind=driver_kind):
                return SensorSnapshot(
                    phase="waiting",
                    sensor_id=sensor.sensor_id,
                    device=sensor.device,
                    metrics={},
                    errors=("sensor_data_not_ready",),
                )
        co2_ppm = _apply_linear_calibration(
            getattr(sensor_service.driver, "CO2", None),
            sensor.calibration_system.co2_offset,
            sensor.calibration_device.co2_offset,
        )
        temp_c = _apply_linear_calibration(
            getattr(sensor_service.driver, "temperature", None),
            sensor.calibration_system.temp_offset,
            sensor.calibration_device.temp_offset,
        )
        rh_pct = _apply_linear_calibration(
            getattr(sensor_service.driver, "relative_humidity", None),
            sensor.calibration_system.rh_offset,
            sensor.calibration_device.rh_offset,
        )
        metrics = _compact_metrics(
            {
                "CO2": _maybe_round(co2_ppm, 0),
                "Temperature": _maybe_round(temp_c, 2),
                "Rel-Humidity": _maybe_round(rh_pct, 0),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        _apply_post_enrichment_calibration(metrics, sensor)
        return _ready_sensor_snapshot(sensor, metrics)

    if sensor.device == "lux":
        lux = _apply_linear_calibration(
            getattr(sensor_service.driver, "lux", None),
            0.0,
            sensor.calibration_device.lux_offset,
        )
        auto_light = _apply_linear_calibration(
            getattr(sensor_service.driver, "autolux", None),
            0.0,
            sensor.calibration_device.lux_offset,
        )
        metrics = _compact_metrics(
            {
                "Light Intensity": _maybe_round(lux, 0),
                "Auto Light": _maybe_round(auto_light, 0),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        _apply_post_enrichment_calibration(metrics, sensor)
        return _ready_sensor_snapshot(sensor, metrics)

    if sensor.device == "aht":
        temp_c = _apply_linear_calibration(
            getattr(sensor_service.driver, "temperature", None),
            sensor.calibration_system.temp_offset,
            sensor.calibration_device.temp_offset,
        )
        rh_pct = _apply_linear_calibration(
            getattr(sensor_service.driver, "relative_humidity", None),
            sensor.calibration_system.rh_offset,
            sensor.calibration_device.rh_offset,
        )
        metrics = _compact_metrics(
            {
                "Temperature": _maybe_round(temp_c, 2),
                "Rel-Humidity": _maybe_round(rh_pct, 0),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        _apply_post_enrichment_calibration(metrics, sensor)
        return _ready_sensor_snapshot(sensor, metrics)

    if sensor.device in {"avpd", "apvpd", "apvpd_aht"}:
        driver = sensor_service.driver
        ambient_driver = driver if sensor.device == "avpd" else driver.ambient
        temp_c = _apply_linear_calibration(
            getattr(ambient_driver, "temperature", None),
            sensor.calibration_system.temp_offset,
            sensor.calibration_device.temp_offset,
        )
        rh_pct = _apply_linear_calibration(
            getattr(ambient_driver, "relative_humidity", None),
            sensor.calibration_system.rh_offset,
            sensor.calibration_device.rh_offset,
        )
        metrics = _compact_metrics(
            {
                "Temperature": _maybe_round(temp_c, 2),
                "Rel-Humidity": _maybe_round(rh_pct, 0),
                "Baro-Pressure": _maybe_round(
                    _scale_pressure_hpa(getattr(ambient_driver, "pressure", None)),
                    None,
                ),
            }
        )
        if sensor.device in {"apvpd", "apvpd_aht"}:
            metrics.update(
                _compact_metrics(
                    {
                        "Plant Temperature": _maybe_round(
                            _apply_linear_calibration(
                                getattr(driver.plant, "temperature", None),
                                0.0,
                                sensor.calibration_device.apvpd_temp_cal_val,
                            ),
                            2,
                        ),
                        "Plant Rel-Humidity": _maybe_round(
                            _apply_linear_calibration(
                                getattr(driver.plant, "relative_humidity", None),
                                0.0,
                                sensor.calibration_device.apvpd_rh_cal_val,
                            ),
                            0,
                        ),
                        "Plant Baro-Pressure": _maybe_round(
                            _scale_pressure_hpa(
                                getattr(driver.plant, "pressure", None)
                            ),
                            None,
                        ),
                    }
                )
            )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        _apply_post_enrichment_calibration(metrics, sensor)
        return _ready_sensor_snapshot(sensor, metrics)

    if sensor.device == "soil":
        transport = sensor_service.driver or sensor_service.transport
        metrics = _read_soil_snapshot_metrics(transport, sensor)
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return _ready_sensor_snapshot(sensor, metrics)

    return SensorSnapshot(
        phase="error",
        sensor_id=sensor.sensor_id,
        device=sensor.device,
        metrics={},
        errors=("unsupported_sensor_snapshot_device",),
    )


def _start_i2c_sensor_service(sensor, sensor_adapter, modules):
    device = sensor.device
    transport = sensor_adapter.transport
    if device == "aqi":
        module = _load_module("adafruit_bme680", modules, "missing_adafruit_bme680")
        if module is None:
            return _sensor_service_error(
                device, sensor.interface, transport, "missing_adafruit_bme680"
            )
        driver = module.Adafruit_BME680_I2C(transport, address=sensor.i2c.address)
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_bme680",
            driver=driver,
            transport=transport,
            errors=(),
        )

    if device == "co2":
        if sensor.i2c.address == 0x62:
            module = _load_module("adafruit_scd4x", modules, "missing_adafruit_scd4x")
            if module is None:
                return _sensor_service_error(
                    device, sensor.interface, transport, "missing_adafruit_scd4x"
                )
            driver = _start_scd4x_driver(module, transport, sensor.i2c.address)
            return SensorService(
                phase="ready",
                device=device,
                interface=sensor.interface,
                driver_kind="adafruit_scd4x",
                driver=driver,
                transport=transport,
                errors=(),
            )
        module = _load_module("adafruit_scd30", modules, "missing_adafruit_scd30")
        if module is None:
            return _sensor_service_error(
                device, sensor.interface, transport, "missing_adafruit_scd30"
            )
        driver = module.SCD30(transport)
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_scd30",
            driver=driver,
            transport=transport,
            errors=(),
        )

    if device == "lux":
        module = _load_module("adafruit_veml7700", modules, "missing_adafruit_veml7700")
        if module is None:
            return _sensor_service_error(
                device, sensor.interface, transport, "missing_adafruit_veml7700"
            )
        driver = module.VEML7700(transport, address=sensor.i2c.address)
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_veml7700",
            driver=driver,
            transport=transport,
            errors=(),
        )

    if device == "aht":
        module = _load_module("adafruit_ahtx0", modules, "missing_adafruit_ahtx0")
        if module is None:
            return _sensor_service_error(
                device,
                sensor.interface,
                transport,
                "missing_adafruit_ahtx0",
            )
        driver = module.AHTx0(transport, address=sensor.i2c.address)
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_ahtx0",
            driver=driver,
            transport=transport,
            errors=(),
        )

    if device in {"avpd", "apvpd"}:
        module = _load_module(
            "adafruit_bme280.basic", modules, "missing_adafruit_bme280"
        )
        if module is None:
            return _sensor_service_error(
                device, sensor.interface, transport, "missing_adafruit_bme280"
            )
        if device == "avpd":
            driver = module.Adafruit_BME280_I2C(transport, address=sensor.i2c.address)
            return SensorService(
                phase="ready",
                device=device,
                interface=sensor.interface,
                driver_kind="adafruit_bme280",
                driver=driver,
                transport=transport,
                errors=(),
            )
        secondary_transport = getattr(sensor_adapter, "secondary_transport", None)
        if secondary_transport is None or sensor.secondary_i2c is None:
            return _sensor_service_error(
                device, sensor.interface, transport, "missing_apvpd_secondary_i2c"
            )
        driver = DualI2CSensorDriver(
            ambient=module.Adafruit_BME280_I2C(transport, address=sensor.i2c.address),
            plant=module.Adafruit_BME280_I2C(
                secondary_transport,
                address=sensor.secondary_i2c.address,
            ),
        )
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_bme280",
            driver=driver,
            transport=transport,
            secondary_transport=secondary_transport,
            errors=(),
        )

    if device == "apvpd_aht":
        module = _load_module("adafruit_ahtx0", modules, "missing_adafruit_ahtx0")
        if module is None:
            return _sensor_service_error(
                device,
                sensor.interface,
                transport,
                "missing_adafruit_ahtx0",
            )
        secondary_transport = getattr(sensor_adapter, "secondary_transport", None)
        if secondary_transport is None or sensor.secondary_i2c is None:
            return _sensor_service_error(
                device,
                sensor.interface,
                transport,
                "missing_apvpd_secondary_i2c",
            )
        driver = DualI2CSensorDriver(
            ambient=module.AHTx0(transport, address=sensor.i2c.address),
            plant=module.AHTx0(
                secondary_transport,
                address=sensor.secondary_i2c.address,
            ),
        )
        return SensorService(
            phase="ready",
            device=device,
            interface=sensor.interface,
            driver_kind="adafruit_ahtx0",
            driver=driver,
            transport=transport,
            secondary_transport=secondary_transport,
            errors=(),
        )

    return _sensor_service_error(
        device, sensor.interface, transport, "unsupported_i2c_sensor_device"
    )


def _load_module(name, modules, error_code):
    if name in modules:
        return modules[name]
    try:
        module = __import__(name)
        if "." not in str(name or ""):
            return module
        current = module
        for part in str(name).split(".")[1:]:
            current = getattr(current, part)
        return current
    except ImportError:
        return None


def _start_scd4x_driver(module, transport, address):
    last_exc = None
    for attempt in range(2):
        try:
            driver = module.SCD4X(transport, address=address)
            driver.start_periodic_measurement()
            return driver
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                sleep(0.25)
    raise last_exc


def _sensor_data_ready(driver, *, driver_kind=""):
    """Return True when a driver either has ready data or no ready flag."""
    attr_names = ("data_ready", "data_available")
    if str(driver_kind or "") == "adafruit_scd30":
        attr_names = ("data_available", "data_ready")
    for attr_name in attr_names:
        ready = _read_ready_attr(driver, attr_name)
        if ready is not None:
            return bool(ready)
    return True


def _read_ready_attr(driver, attr_name):
    """Read one sensor ready flag, supporting property and zero-arg callable forms."""
    try:
        value = getattr(driver, attr_name)
    except Exception:
        return None
    if value is None:
        return None
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    if value is None:
        return None
    return value


def _ready_sensor_snapshot(sensor, metrics):
    """Return a ready snapshot or an explicit empty-metrics sensor error."""
    if not metrics:
        return SensorSnapshot(
            phase="error",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics={},
            errors=("sensor_metrics_empty",),
        )
    return SensorSnapshot(
        phase="ready",
        sensor_id=sensor.sensor_id,
        device=sensor.device,
        metrics=metrics,
        errors=(),
    )


def _sensor_service_error(device, interface, transport, error, exc=None):
    errors = (error,)
    if exc is not None:
        errors += (_exception_error_token(error, exc),)
    return SensorService(
        phase="error",
        device=device,
        interface=interface,
        driver_kind="",
        transport=transport,
        errors=errors,
    )


def _sensor_snapshot_error(sensor, error, exc=None):
    errors = (error,)
    if exc is not None:
        errors += (_exception_error_token(error, exc),)
    return SensorSnapshot(
        phase="error",
        sensor_id=sensor.sensor_id,
        device=sensor.device,
        metrics={},
        errors=errors,
    )


def _is_sensor_not_found_exception(exc):
    text = str(exc or "").strip().lower().replace(" ", "_")
    markers = (
        "no_such_device",
        "no_i2c_device",
        "no_i2c_device_at_address",
    )
    for marker in markers:
        if marker in text:
            return True
    try:
        if int(getattr(exc, "errno", -1)) == 19:
            return True
    except Exception:
        pass
    for arg in tuple(getattr(exc, "args", ()) or ()):
        try:
            if int(arg) == 19:
                return True
        except Exception:
            pass
    return False


def _exception_error_token(error, exc):
    text = str(exc or "").strip()
    if text:
        text = text.replace(",", ";").replace(" ", "_")
        return "{}:{}:{}".format(error, type(exc).__name__, text[:48])
    return "{}:{}".format(error, type(exc).__name__)


def _safe_deinit(handle):
    if handle is None:
        return
    if isinstance(handle, tuple):
        for item in handle:
            if isinstance(item, tuple) and len(item) >= 2:
                _safe_deinit(item[1])
            else:
                _safe_deinit(item)
        return
    deinit = getattr(handle, "deinit", None)
    if callable(deinit):
        try:
            deinit()
        except Exception:
            pass


def _maybe_round(value, digits=3):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if digits is None:
        return numeric
    return round(numeric, digits)


def _scale_pressure_hpa(value):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 2000:
        return numeric / 100.0
    return numeric


def _apply_linear_calibration(value, *offsets):
    if value is None:
        return None
    try:
        total = float(value)
        for offset in offsets:
            total += float(offset or 0.0)
        return total
    except (TypeError, ValueError):
        return None


def _apply_post_enrichment_calibration(metrics, sensor):
    calibration = getattr(sensor, "calibration_device", None)
    if calibration is None:
        return
    if "Air Quality" in metrics:
        metrics["Air Quality"] = _maybe_round(
            _apply_linear_calibration(
                metrics.get("Air Quality"), calibration.aqi_offset
            ),
            0,
        )
    if "Estimated PPFD" in metrics:
        metrics["Estimated PPFD"] = _maybe_round(
            _apply_linear_calibration(
                metrics.get("Estimated PPFD"), calibration.ppfd_offset
            ),
            0,
        )
        metrics["Visible Light Intensity"] = estimate_dli_from_ppfd(
            metrics.get("Estimated PPFD")
        )


def _compact_metrics(metrics):
    return {key: value for key, value in metrics.items() if value is not None}


def _read_soil_snapshot_metrics(transport, sensor):
    if isinstance(transport, tuple):
        result = {}
        active = []
        for item in transport:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            channel, channel_transport = item[0], item[1]
            metrics = _compact_metrics(_read_soil_metrics(channel_transport, sensor))
            if metrics:
                active.append(
                    (
                        getattr(channel, "name", "") or "CH{}".format(len(active) + 1),
                        metrics,
                    )
                )
        if len(active) == 1:
            return active[0][1]
        for name, metrics in active:
            prefix = "{} ".format(str(name or "").strip().upper())
            for key, value in metrics.items():
                result["{}{}".format(prefix, key)] = value
        return result
    return _compact_metrics(_read_soil_metrics(transport, sensor))


def _read_soil_metrics(transport, sensor):
    if transport is None or not hasattr(transport, "read_registers"):
        return {}
    registers = sensor.soil_registers
    scales = sensor.soil_scales
    if registers is None or scales is None:
        return {}
    calibration = getattr(sensor, "calibration_device", None)
    return {
        "Soil Temp_C": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    transport.read_registers(registers.temperature, 1),
                    scales.temperature,
                    None,
                ),
                getattr(calibration, "soil_temp_cal_val", 0.0),
            ),
            2,
        ),
        "Soil Moisture": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    transport.read_registers(registers.moisture, 1),
                    scales.moisture,
                    None,
                ),
                getattr(calibration, "soil_temp_moist_val", 0.0),
            ),
            0,
        ),
        "Soil EC": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    transport.read_registers(registers.ec, 1), scales.ec, None
                ),
                getattr(calibration, "soil_ec_cal_val", 0.0),
            ),
            2,
        ),
        "Soil pH": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    transport.read_registers(registers.ph, 1), scales.ph, None
                ),
                getattr(calibration, "soil_ph_cal_val", 0.0),
            ),
            1,
        ),
        "Soil Nitrogen": _scale_register(
            transport.read_registers(registers.n, 1), scales.n, 0
        ),
        "Soil Phosphorus": _scale_register(
            transport.read_registers(registers.p, 1), scales.p, 0
        ),
        "Soil Potassium": _scale_register(
            transport.read_registers(registers.k, 1), scales.k, 0
        ),
    }


def _scale_register(raw_value, scale, digits):
    if raw_value is None:
        return None
    try:
        numeric = float(raw_value) / float(scale or 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if digits is None:
        return numeric
    return round(numeric, digits)


def _modbus_crc16(data):
    crc = 0xFFFF
    for value in bytes(data):
        crc ^= value
        for _ in range(8):
            if (crc & 1) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc


def _parse_modbus_register_response(response, *, address, count):
    if not response or len(response) < 5:
        return None
    body = response[:-2]
    crc_low = response[-2]
    crc_high = response[-1]
    expected_crc = _modbus_crc16(body)
    if crc_low != (expected_crc & 0xFF) or crc_high != ((expected_crc >> 8) & 0xFF):
        return None
    if response[0] != (int(address) & 0xFF) or response[1] != 0x03:
        return None
    byte_count = response[2]
    expected_byte_count = int(count) * 2
    if byte_count != expected_byte_count:
        return None
    data = response[3 : 3 + byte_count]
    if len(data) != byte_count:
        return None
    return tuple(
        (data[index] << 8) | data[index + 1] for index in range(0, len(data), 2)
    )
