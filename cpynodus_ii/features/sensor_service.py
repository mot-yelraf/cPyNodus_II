"""Sensor service lifecycle on top of hardware adapters."""

from dataclasses import dataclass

from cpynodus_ii.features.derived_metrics import enrich_metrics


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
    errors: tuple = ()


def start_sensor_service(sensor_runtime, sensor_adapter, runtime_config, *, modules=None):
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
        return _start_i2c_sensor_service(sensor, transport, modules)

    if sensor_runtime.interface == "modbus_rs485":
        return SensorService(
            phase="ready",
            device=sensor.device,
            interface=sensor.interface,
            driver_kind="soil_modbus_uart",
            driver=None,
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

    if sensor.device == "aqi":
        metrics = _compact_metrics(
            {
                "temperature_c": _maybe_round(getattr(sensor_service.driver, "temperature", None)),
                "humidity_rh": _maybe_round(getattr(sensor_service.driver, "humidity", None)),
                "pressure_hpa": _maybe_round(_scale_pressure_hpa(getattr(sensor_service.driver, "pressure", None))),
                "gas_ohms": _maybe_round(getattr(sensor_service.driver, "gas", None)),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return SensorSnapshot(
            phase="ready",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics=metrics,
            errors=(),
        )

    if sensor.device == "co2":
        metrics = _compact_metrics(
            {
                "co2_ppm": _maybe_round(getattr(sensor_service.driver, "CO2", None)),
                "temperature_c": _maybe_round(getattr(sensor_service.driver, "temperature", None)),
                "humidity_rh": _maybe_round(getattr(sensor_service.driver, "relative_humidity", None)),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return SensorSnapshot(
            phase="ready",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics=metrics,
            errors=(),
        )

    if sensor.device == "lux":
        metrics = _compact_metrics(
            {
                "lux": _maybe_round(getattr(sensor_service.driver, "lux", None)),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return SensorSnapshot(
            phase="ready",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics=metrics,
            errors=(),
        )

    if sensor.device in {"avpd", "apvpd"}:
        metrics = _compact_metrics(
            {
                "temperature_c": _maybe_round(getattr(sensor_service.driver, "temperature", None)),
                "humidity_rh": _maybe_round(getattr(sensor_service.driver, "relative_humidity", None)),
                "pressure_hpa": _maybe_round(_scale_pressure_hpa(getattr(sensor_service.driver, "pressure", None))),
            }
        )
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return SensorSnapshot(
            phase="ready",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics=metrics,
            errors=(),
        )

    if sensor.device == "soil":
        metrics = _compact_metrics(_read_soil_metrics(sensor_service.transport, sensor))
        metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
        return SensorSnapshot(
            phase="ready",
            sensor_id=sensor.sensor_id,
            device=sensor.device,
            metrics=metrics,
            errors=(),
        )

    return SensorSnapshot(
        phase="error",
        sensor_id=sensor.sensor_id,
        device=sensor.device,
        metrics={},
        errors=("unsupported_sensor_snapshot_device",),
    )


def _start_i2c_sensor_service(sensor, transport, modules):
    device = sensor.device
    if device == "aqi":
        module = _load_module("adafruit_bme680", modules, "missing_adafruit_bme680")
        if module is None:
            return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_bme680")
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
                return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_scd4x")
            driver = module.SCD4X(transport)
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
            return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_scd30")
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
            return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_veml7700")
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

    if device in {"avpd", "apvpd"}:
        module = _load_module("adafruit_bme280", modules, "missing_adafruit_bme280")
        if module is None:
            return _sensor_service_error(device, sensor.interface, transport, "missing_adafruit_bme280")
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

    return _sensor_service_error(device, sensor.interface, transport, "unsupported_i2c_sensor_device")


def _load_module(name, modules, error_code):
    if name in modules:
        return modules[name]
    try:
        return __import__(name)
    except ImportError:
        return None


def _sensor_service_error(device, interface, transport, error):
    return SensorService(
        phase="error",
        device=device,
        interface=interface,
        driver_kind="",
        transport=transport,
        errors=(error,),
    )


def _safe_deinit(handle):
    if handle is None:
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
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


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


def _compact_metrics(metrics):
    return {key: value for key, value in metrics.items() if value is not None}


def _read_soil_metrics(transport, sensor):
    if transport is None or not hasattr(transport, "read_registers"):
        return {}
    registers = sensor.soil_registers
    scales = sensor.soil_scales
    if registers is None or scales is None:
        return {}
    return {
        "soil_temperature_c": _scale_register(transport.read_registers(registers.temperature, 1), scales.temperature),
        "soil_moisture_pct": _scale_register(transport.read_registers(registers.moisture, 1), scales.moisture),
        "soil_ec": _scale_register(transport.read_registers(registers.ec, 1), scales.ec),
        "soil_ph": _scale_register(transport.read_registers(registers.ph, 1), scales.ph),
        "soil_n": _scale_register(transport.read_registers(registers.n, 1), scales.n),
        "soil_p": _scale_register(transport.read_registers(registers.p, 1), scales.p),
        "soil_k": _scale_register(transport.read_registers(registers.k, 1), scales.k),
    }


def _scale_register(raw_value, scale):
    if raw_value is None:
        return None
    try:
        return round(float(raw_value) / float(scale or 1.0), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
