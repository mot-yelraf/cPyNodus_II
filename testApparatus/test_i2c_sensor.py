"""
Standalone I2C sensor apparatus for Nodus CircuitPython devices.

Copy this file to the CIRCUITPY root and run it while the normal app is
stopped:

    import test_i2c_sensor
    records = test_i2c_sensor.run()

The apparatus scans the two Pico2 W Nodus I2C buses:
  - I2C0: SCL=GP1 SDA=GP0
  - I2C1: SCL=GP3 SDA=GP2

It then tries the supported Nodus I2C sensor drivers at detected addresses and
prints a compact sampled data set. It does not write files or change settings.
"""

import gc
import time

import board
import busio

SCRIPT_VERSION = "i2c_sensor_test_v1_2026-06-15"

I2C_BUSES = (
    ("I2C0", "GP1", "GP0"),
    ("I2C1", "GP3", "GP2"),
)

ADDRESS_LABELS = {
    0x10: "lux/veml7700",
    0x38: "aht/ahtx0",
    0x61: "co2/scd30",
    0x62: "co2/scd4x",
    0x76: "avpd/bme280",
    0x77: "aqi/bme680",
}


def run(sample_count=5, sample_interval_s=5.0, warmup_s=2.0):
    """Scan both Nodus I2C buses and sample all detected Nodus sensor types."""
    gc.collect()
    started = time.monotonic()
    print(
        "i2c_sensor_test phase=start version={} samples={} interval_s={}".format(
            SCRIPT_VERSION,
            int(sample_count or 0),
            float(sample_interval_s or 0.0),
        )
    )

    scans = scan_buses()
    _print_detection_summary(scans)
    sensors = start_detected_sensors(scans)
    if not sensors:
        print("i2c_sensor_test phase=done result=no_supported_sensors")
        return []

    if warmup_s:
        print("dataset phase=warmup seconds={}".format(float(warmup_s)))
        time.sleep(float(warmup_s))

    records = collect_dataset(
        sensors,
        sample_count=sample_count,
        sample_interval_s=sample_interval_s,
    )
    stop_sensors(sensors)
    gc.collect()
    done_message = (
        "i2c_sensor_test phase=done result=pass "
        "sensors={} records={} elapsed_s={:.1f}"
    )
    print(done_message.format(len(sensors), len(records), time.monotonic() - started))
    return records


def scan_buses():
    """Return scan results for each configured Nodus I2C bus."""
    scans = []
    for bus_name, scl_name, sda_name in I2C_BUSES:
        bus = None
        addresses = ()
        error = ""
        try:
            scl = getattr(board, scl_name)
            sda = getattr(board, sda_name)
            bus = busio.I2C(scl, sda)
            if _lock_i2c(bus, timeout_s=0.5):
                try:
                    addresses = tuple(sorted(bus.scan()))
                finally:
                    _safe_unlock(bus)
            else:
                error = "lock_timeout"
        except Exception as exc:
            error = "{}:{}".format(type(exc).__name__, _clean_error(exc))
        finally:
            _safe_deinit(bus)

        print(
            "scan bus={} scl={} sda={} addrs={} labels={} errors={}".format(
                bus_name,
                scl_name,
                sda_name,
                _format_addresses(addresses),
                _format_address_labels(addresses),
                error or "none",
            )
        )
        scans.append(
            {
                "bus": bus_name,
                "scl": scl_name,
                "sda": sda_name,
                "addresses": addresses,
                "error": error,
            }
        )
    return scans


def start_detected_sensors(scans):
    """Start supported sensor drivers for scanned Nodus I2C addresses."""
    sensors = []
    for scan in scans:
        i2c = None
        bus_sensor_count = 0
        for address in scan.get("addresses", ()):
            starter_names = _starter_names_for_address(address)
            if not starter_names:
                continue
            if i2c is None:
                try:
                    i2c = _open_i2c(scan)
                except Exception as exc:
                    print(
                        "detect bus={} phase=failed_open error={}:{}".format(
                            scan.get("bus", ""),
                            type(exc).__name__,
                            _clean_error(exc),
                        )
                    )
                    break
            sensor = _try_start_first_sensor(scan, address, starter_names, i2c)
            if sensor is not None:
                sensor["owns_i2c"] = bus_sensor_count == 0
                bus_sensor_count += 1
                sensors.append(sensor)
        if i2c is not None and bus_sensor_count == 0:
            _safe_deinit(i2c)
    return sensors


def collect_dataset(sensors, sample_count=5, sample_interval_s=5.0):
    """Collect and print sampled records for started sensor drivers."""
    records = []
    count = max(1, int(sample_count or 1))
    interval = max(0.0, float(sample_interval_s or 0.0))
    for sample_index in range(1, count + 1):
        for sensor in sensors:
            record = _sample_sensor(sensor, sample_index)
            records.append(record)
            print(_format_record(record))
        if sample_index < count and interval:
            time.sleep(interval)
    return records


def stop_sensors(sensors):
    """Deinitialize all sensor drivers and buses used by the apparatus."""
    for sensor in sensors or ():
        _safe_deinit(sensor.get("driver"))
    for sensor in sensors or ():
        if not sensor.get("owns_i2c"):
            continue
        _safe_deinit(sensor.get("i2c"))


def _try_start_first_sensor(scan, address, starter_names, i2c):
    for starter_name in starter_names:
        try:
            sensor = _start_sensor(starter_name, i2c, address)
            sensor.update(
                {
                    "bus": scan.get("bus", ""),
                    "scl": scan.get("scl", ""),
                    "sda": scan.get("sda", ""),
                    "address": int(address),
                    "i2c": i2c,
                }
            )
            ready_message = (
                "detect bus={} addr=0x{:02X} nodus_device={} "
                "driver={} phase=ready"
            )
            print(
                ready_message.format(
                    sensor.get("bus", ""),
                    int(address),
                    sensor.get("device", ""),
                    sensor.get("driver_kind", ""),
                )
            )
            return sensor
        except ImportError as exc:
            missing_message = (
                "detect bus={} addr=0x{:02X} driver={} "
                "phase=missing_driver error={}"
            )
            print(
                missing_message.format(
                    scan.get("bus", ""),
                    int(address),
                    starter_name,
                    _clean_error(exc),
                )
            )
        except Exception as exc:
            print(
                "detect bus={} addr=0x{:02X} driver={} phase=failed error={}:{}".format(
                    scan.get("bus", ""),
                    int(address),
                    starter_name,
                    type(exc).__name__,
                    _clean_error(exc),
                )
            )
    return None


def _start_sensor(starter_name, i2c, address):
    if starter_name == "bme680":
        module = __import__("adafruit_bme680")
        driver = module.Adafruit_BME680_I2C(i2c, address=address)
        return {
            "device": "aqi",
            "driver_kind": "adafruit_bme680",
            "driver": driver,
            "reader": _read_bme680,
        }

    if starter_name == "bme280":
        module = __import__("adafruit_bme280.basic")
        basic = module.basic
        driver = basic.Adafruit_BME280_I2C(i2c, address=address)
        return {
            "device": "avpd",
            "driver_kind": "adafruit_bme280",
            "driver": driver,
            "reader": _read_bme280,
        }

    if starter_name == "ahtx0":
        module = __import__("adafruit_ahtx0")
        driver = module.AHTx0(i2c, address=address)
        return {
            "device": "aht",
            "driver_kind": "adafruit_ahtx0",
            "driver": driver,
            "reader": _read_ahtx0,
        }

    if starter_name == "scd30":
        module = __import__("adafruit_scd30")
        driver = module.SCD30(i2c)
        return {
            "device": "co2",
            "driver_kind": "adafruit_scd30",
            "driver": driver,
            "reader": _read_scd,
        }

    if starter_name == "scd4x":
        module = __import__("adafruit_scd4x")
        driver = module.SCD4X(i2c, address=address)
        try:
            driver.start_periodic_measurement()
        except Exception:
            pass
        return {
            "device": "co2",
            "driver_kind": "adafruit_scd4x",
            "driver": driver,
            "reader": _read_scd,
        }

    if starter_name == "veml7700":
        module = __import__("adafruit_veml7700")
        driver = module.VEML7700(i2c, address=address)
        return {
            "device": "lux",
            "driver_kind": "adafruit_veml7700",
            "driver": driver,
            "reader": _read_veml7700,
        }

    raise RuntimeError("unsupported_starter:{}".format(starter_name))


def _starter_names_for_address(address):
    address = int(address)
    if address == 0x77:
        return ("bme680", "bme280")
    if address == 0x76:
        return ("bme280", "bme680")
    if address == 0x38:
        return ("ahtx0",)
    if address == 0x61:
        return ("scd30",)
    if address == 0x62:
        return ("scd4x",)
    if address == 0x10:
        return ("veml7700",)
    return ()


def _open_i2c(scan):
    scl = getattr(board, scan.get("scl"))
    sda = getattr(board, scan.get("sda"))
    return busio.I2C(scl, sda)


def _sample_sensor(sensor, sample_index):
    values = {}
    errors = ()
    try:
        reader = sensor.get("reader")
        if callable(reader):
            values = reader(sensor.get("driver")) or {}
        else:
            errors = ("missing_reader",)
    except Exception as exc:
        errors = ("{}:{}".format(type(exc).__name__, _clean_error(exc)),)

    return {
        "sample": int(sample_index),
        "elapsed_s": round(time.monotonic(), 1),
        "bus": sensor.get("bus", ""),
        "scl": sensor.get("scl", ""),
        "sda": sensor.get("sda", ""),
        "address": int(sensor.get("address", 0)),
        "device": sensor.get("device", ""),
        "driver": sensor.get("driver_kind", ""),
        "values": values,
        "errors": errors,
    }


def _read_bme680(driver):
    return _compact(
        {
            "Temperature": _round_or_none(_safe_get(driver, "temperature"), 2),
            "Rel-Humidity": _round_or_none(_safe_get(driver, "humidity"), 0),
            "Baro-Pressure": _round_or_none(
                _pressure_hpa(_safe_get(driver, "pressure")),
                0,
            ),
            "Gas": _round_or_none(_safe_get(driver, "gas"), 0),
        }
    )


def _read_bme280(driver):
    return _compact(
        {
            "Temperature": _round_or_none(_safe_get(driver, "temperature"), 2),
            "Rel-Humidity": _round_or_none(
                _safe_get(driver, "relative_humidity"),
                0,
            ),
            "Baro-Pressure": _round_or_none(
                _pressure_hpa(_safe_get(driver, "pressure")),
                0,
            ),
        }
    )


def _read_ahtx0(driver):
    return _compact(
        {
            "Temperature": _round_or_none(_safe_get(driver, "temperature"), 2),
            "Rel-Humidity": _round_or_none(
                _safe_get(driver, "relative_humidity"),
                0,
            ),
        }
    )


def _read_scd(driver):
    if not _co2_data_ready(driver):
        return {"Data Ready": False}
    return _compact(
        {
            "CO2": _round_or_none(_safe_get(driver, "CO2"), 0),
            "Temperature": _round_or_none(_safe_get(driver, "temperature"), 2),
            "Rel-Humidity": _round_or_none(
                _safe_get(driver, "relative_humidity"),
                0,
            ),
        }
    )


def _read_veml7700(driver):
    return _compact(
        {
            "Light Intensity": _round_or_none(_safe_get(driver, "lux"), 0),
            "Auto Light": _round_or_none(_safe_get(driver, "autolux"), 0),
            "Visible Light": _round_or_none(_safe_get(driver, "light"), 0),
            "White Light": _round_or_none(_safe_get(driver, "white"), 0),
        }
    )


def _co2_data_ready(driver):
    for name in ("data_available", "data_ready"):
        value = _safe_get(driver, name)
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        return bool(value)
    return True


def _safe_get(obj, name):
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _compact(values):
    result = {}
    for key, value in values.items():
        if value is not None:
            result[key] = value
    return result


def _round_or_none(value, digits):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits)


def _pressure_hpa(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 2000.0:
        return number / 100.0
    return number


def _lock_i2c(bus, timeout_s=0.5):
    try:
        try_lock = getattr(bus, "try_lock", None)
        if not callable(try_lock):
            return True
        started = time.monotonic()
        while not try_lock():
            if time.monotonic() - started >= float(timeout_s):
                return False
            time.sleep(0.01)
        return True
    except Exception:
        return False


def _safe_unlock(bus):
    try:
        unlock = getattr(bus, "unlock", None)
        if callable(unlock):
            unlock()
    except Exception:
        pass


def _safe_deinit(handle):
    if handle is None:
        return
    try:
        deinit = getattr(handle, "deinit", None)
        if callable(deinit):
            deinit()
    except Exception:
        pass


def _format_addresses(addresses):
    addresses = tuple(addresses or ())
    if not addresses:
        return "none"
    return ",".join("0x{:02X}".format(int(address)) for address in addresses)


def _format_address_labels(addresses):
    labels = []
    for address in tuple(addresses or ()):
        label = ADDRESS_LABELS.get(int(address), "")
        if label:
            labels.append("{}=0x{:02X}".format(label, int(address)))
    return ",".join(labels) if labels else "none"


def _format_values(values):
    values = values or {}
    if not values:
        return "none"
    parts = []
    for key in sorted(values):
        parts.append("{}={}".format(key.replace(" ", "_"), values[key]))
    return ",".join(parts)


def _format_record(record):
    return (
        "dataset sample={} bus={} scl={} sda={} addr=0x{:02X} "
        "device={} driver={} values={} errors={}"
    ).format(
        record.get("sample", 0),
        record.get("bus", ""),
        record.get("scl", ""),
        record.get("sda", ""),
        int(record.get("address", 0)),
        record.get("device", ""),
        record.get("driver", ""),
        _format_values(record.get("values")),
        ",".join(record.get("errors", ())) if record.get("errors") else "none",
    )


def _print_detection_summary(scans):
    bme280_buses = _buses_with_address(scans, 0x76)
    aht_buses = _buses_with_address(scans, 0x38)
    if len(bme280_buses) >= 2:
        print(
            "detect pair nodus_device=apvpd addr=0x76 buses={}".format(
                ",".join(bme280_buses)
            )
        )
    if len(aht_buses) >= 2:
        print(
            "detect pair nodus_device=apvpd_aht addr=0x38 buses={}".format(
                ",".join(aht_buses)
            )
        )


def _buses_with_address(scans, address):
    buses = []
    for scan in scans:
        if int(address) in tuple(scan.get("addresses", ()) or ()):
            buses.append(str(scan.get("bus", "")))
    return buses


def _clean_error(exc):
    text = str(exc or "").strip().replace(" ", "_")
    return text or "unknown"
