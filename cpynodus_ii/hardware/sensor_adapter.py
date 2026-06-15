"""Bind configured sensors to board-level hardware transports.

This module maps normalized sensor configuration to concrete bus objects such
as I2C and Modbus transports while keeping hardware setup separate from sensor
service logic.
"""

from dataclasses import dataclass

from cpynodus_ii.core.board_profile import selected_board_profile


@dataclass(frozen=True)
class SensorHardwareAdapter:
    """Describe the hardware binding for a sensor runtime."""

    phase: str
    interface: str
    transport_kind: str
    transport_target: str
    transport: object | None = None
    secondary_transport: object | None = None
    i2c_fallbacks: tuple = ()
    errors: tuple = ()


def bind_sensor_hardware(
    sensor_runtime, runtime_config, *, board_module=None, busio_module=None
):
    """Bind the sensor runtime to CircuitPython transport objects when possible."""
    if sensor_runtime.phase != "ready":
        return SensorHardwareAdapter(
            phase=sensor_runtime.phase,
            interface=sensor_runtime.interface,
            transport_kind="",
            transport_target=sensor_runtime.transport_target,
            errors=sensor_runtime.errors,
        )

    errors = []
    board_module = board_module or _try_import_module(
        "board", "board_module_unavailable", errors
    )
    busio_module = busio_module or _try_import_module(
        "busio", "busio_module_unavailable", errors
    )
    if errors:
        return SensorHardwareAdapter(
            phase="error",
            interface=sensor_runtime.interface,
            transport_kind="",
            transport_target=sensor_runtime.transport_target,
            errors=tuple(errors),
        )

    sensor = runtime_config.sensor
    if sensor_runtime.interface == "i2c" and sensor.i2c is not None:
        i2c_fallbacks = _single_i2c_fallback_specs(
            sensor, board_module, busio_module
        )
        scl = _resolve_pin(
            board_module, sensor.i2c.scl_pin, "missing_i2c_scl_pin_object", errors
        )
        sda = _resolve_pin(
            board_module, sensor.i2c.sda_pin, "missing_i2c_sda_pin_object", errors
        )
        secondary_scl = None
        secondary_sda = None
        if sensor.device in {"apvpd", "apvpd_aht"} and sensor.secondary_i2c is not None:
            secondary_scl = _resolve_pin(
                board_module,
                sensor.secondary_i2c.scl_pin,
                "missing_secondary_i2c_scl_pin_object",
                errors,
            )
            secondary_sda = _resolve_pin(
                board_module,
                sensor.secondary_i2c.sda_pin,
                "missing_secondary_i2c_sda_pin_object",
                errors,
            )
        if errors:
            return SensorHardwareAdapter(
                phase="error",
                interface=sensor_runtime.interface,
                transport_kind="i2c",
                transport_target=sensor_runtime.transport_target,
                i2c_fallbacks=i2c_fallbacks,
                errors=tuple(errors),
            )
        transport = None
        try:
            transport = busio_module.I2C(scl, sda)
        except Exception as exc:
            fallback = _open_first_i2c_fallback(busio_module, i2c_fallbacks)
            if fallback is None:
                return SensorHardwareAdapter(
                    phase="error",
                    interface=sensor_runtime.interface,
                    transport_kind="i2c",
                    transport_target=sensor_runtime.transport_target,
                    i2c_fallbacks=i2c_fallbacks,
                    errors=("i2c_open_failed:{}".format(_error_text(exc)),),
                )
            fallback_transport, fallback_spec, remaining_specs = fallback
            return SensorHardwareAdapter(
                phase="bound",
                interface=sensor_runtime.interface,
                transport_kind="i2c_fallback",
                transport_target=fallback_spec.get(
                    "target", sensor_runtime.transport_target
                ),
                transport=fallback_transport,
                i2c_fallbacks=remaining_specs,
                errors=(
                    "i2c_open_failed:{}".format(_error_text(exc)),
                    "i2c_fallback:{}".format(fallback_spec.get("target", "")),
                ),
            )
        secondary_transport = None
        if sensor.device in {"apvpd", "apvpd_aht"} and sensor.secondary_i2c is not None:
            secondary_transport = busio_module.I2C(secondary_scl, secondary_sda)
        return SensorHardwareAdapter(
            phase="bound",
            interface=sensor_runtime.interface,
            transport_kind="i2c_dual" if secondary_transport is not None else "i2c",
            transport_target=sensor_runtime.transport_target,
            transport=transport,
            secondary_transport=secondary_transport,
            i2c_fallbacks=i2c_fallbacks,
            errors=(),
        )

    if sensor_runtime.interface == "modbus_rs485" and sensor.modbus is not None:
        channel_specs = tuple(
            getattr(sensor.modbus, "channels", ()) or (sensor.modbus,)
        )
        transports = []
        for channel in channel_specs:
            prefix = "{}:".format(getattr(channel, "name", "") or "MODBUS")
            tx = _resolve_pin(
                board_module,
                channel.uart_tx,
                "{}missing_modbus_uart_tx_object".format(prefix),
                errors,
            )
            rx = _resolve_pin(
                board_module,
                channel.uart_rx,
                "{}missing_modbus_uart_rx_object".format(prefix),
                errors,
            )
            transports.append((channel, tx, rx))
        if errors:
            return SensorHardwareAdapter(
                phase="error",
                interface=sensor_runtime.interface,
                transport_kind="uart",
                transport_target=sensor_runtime.transport_target,
                errors=tuple(errors),
            )
        bound_transports = []
        for channel, tx, rx in transports:
            bound_transports.append(
                (
                    channel,
                    busio_module.UART(
                        tx,
                        rx,
                        baudrate=channel.baud,
                        timeout=channel.timeout_s,
                    ),
                )
            )
        if len(bound_transports) == 1:
            transport = bound_transports[0][1]
        else:
            transport = tuple(bound_transports)
        return SensorHardwareAdapter(
            phase="bound",
            interface=sensor_runtime.interface,
            transport_kind="uart" if len(bound_transports) == 1 else "uart_dual",
            transport_target=sensor_runtime.transport_target,
            transport=transport,
            errors=(),
        )

    return SensorHardwareAdapter(
        phase="error",
        interface=sensor_runtime.interface,
        transport_kind="",
        transport_target=sensor_runtime.transport_target,
        errors=("unsupported_sensor_runtime_interface",),
    )


def _try_import_module(module_name, error_code, errors):
    try:
        return __import__(module_name)
    except ImportError:
        errors.append(error_code)
        return None


def _resolve_pin(board_module, pin_name, error_code, errors):
    pin = getattr(board_module, pin_name, None)
    if pin is None:
        errors.append(error_code)
    return pin


def _single_i2c_fallback_specs(sensor, board_module, busio_module):
    if str(getattr(sensor, "interface", "") or "") != "i2c":
        return ()
    if str(getattr(sensor, "device", "") or "") in {"apvpd", "apvpd_aht"}:
        return ()
    i2c = getattr(sensor, "i2c", None)
    if i2c is None:
        return ()
    configured = (
        str(getattr(i2c, "scl_pin", "") or ""),
        str(getattr(i2c, "sda_pin", "") or ""),
    )
    address = int(getattr(i2c, "address", 0) or 0)
    fallback_specs = []
    profile = selected_board_profile(board_module=board_module)
    for bus_index, pins in enumerate(tuple(getattr(profile, "i2c_pins", ()) or ())):
        if len(pins) < 2:
            continue
        scl_name, sda_name = str(pins[0] or ""), str(pins[1] or "")
        if (scl_name, sda_name) == configured:
            continue
        scl = getattr(board_module, scl_name, None)
        sda = getattr(board_module, sda_name, None)
        if scl is None or sda is None:
            continue
        fallback_specs.append(
            {
                "bus": int(bus_index),
                "scl_pin": scl_name,
                "sda_pin": sda_name,
                "scl": scl,
                "sda": sda,
                "busio_module": busio_module,
                "address": address,
                "target": "i2c:{}@0x{:02x}".format(bus_index, address),
            }
        )
    return tuple(fallback_specs)


def _open_first_i2c_fallback(busio_module, fallback_specs):
    remaining = []
    for index, spec in enumerate(tuple(fallback_specs or ())):
        try:
            transport = busio_module.I2C(spec.get("scl"), spec.get("sda"))
            remaining.extend(tuple(fallback_specs or ())[index + 1 :])
            return transport, spec, tuple(remaining)
        except Exception:
            continue
    return None


def _error_text(exc):
    return str(exc or "").strip().replace(" ", "_") or type(exc).__name__
