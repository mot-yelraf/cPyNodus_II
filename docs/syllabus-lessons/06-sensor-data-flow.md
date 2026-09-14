# Lesson 6: Sensor data flow

[Previous](05-startup-and-configuration.md) · [Index](README.md) · [Next](07-mqtt-and-integrations.md)

## What you will learn

Follow driver properties into normalized metrics, distinguish calibration from
derivation, and test acquisition without physical I2C. Allow 120 minutes.

## Preparation and reading

Use a host checkout and pytest; no network profile is needed. Read
[extending](../extending.md) and the sensor sections of
[configuration](../configuration.md). Inspect `start_sensor_service` and
`read_sensor_snapshot` in [sensor_service.py](../../cpynodus_ii/features/sensor_service.py),
[the hardware adapter](../../cpynodus_ii/hardware/sensor_adapter.py), and
`enrich_metrics` in [derived_metrics.py](../../cpynodus_ii/features/derived_metrics.py).
Use [service tests](../../tests/test_feature_services.py) as examples of doubles.

## Walkthrough

The hardware adapter opens a configured bus. The service creates a driver on
that transport. Reads become a `SensorSnapshot` with phase, identity, metrics,
and errors. For BME280 temperature, system and device offsets are applied before
derived humidity/VPD metrics are computed. Display choices do not rename the
canonical MQTT metric keys. UART/Modbus soil sensors follow a separate service
path with register decoding and response validation.

## Lab: a driver double

Save this host-only starter as `/tmp/test_nodus_sensor_lesson.py`. Replace the
TODO values with your predictions before running it; it is not device firmware.

```python
"""Exercise sensor normalization on the host.

A small driver double supplies readings without opening an I2C bus.
"""
from types import SimpleNamespace

from cpynodus_ii.core.config import DetectedSensor, RuntimeConfig, SensorCalibration
from cpynodus_ii.features.sensor_service import SensorService, read_sensor_snapshot


def test_bme280_temperature_offsets():
    """Check the normalized temperature after both configured offsets."""
    sensor = DetectedSensor(
        device="avpd", interface="i2c", sensor_id="avpd-lesson",
        calibration_system=SensorCalibration(temp_offset=1.0),
        calibration_device=SensorCalibration(temp_offset=-0.5),
    )
    service = SensorService(
        phase="ready", device="avpd", interface="i2c", driver_kind="lesson",
        driver=SimpleNamespace(temperature=20.0, relative_humidity=50.0, pressure=1000.0),
    )
    snapshot = read_sensor_snapshot(service, RuntimeConfig(sensor=sensor))
    assert snapshot.phase == "TODO"
    assert snapshot.metrics["Temperature"] == "TODO"
```

Run `PYTHONPATH=. pytest /tmp/test_nodus_sensor_lesson.py`. Then add a second
case whose temperature property raises `OSError("read failed")`; predict and
assert the snapshot phase and whether its metrics are empty. Read the exception
classification code before choosing an expected error token.

Run `pytest tests/test_feature_services.py tests/test_hardware_adapters.py`.
Draw a path from bus binding to calibrated temperature to derived metrics and
`build_sensor_data_payload` in [payloads.py](../../cpynodus_ii/features/payloads.py).

## Acceptance and debugging

Submit the completed host tests, results, and a diagram labeling raw units,
calibrated units, and output metric names. Both ordinary data and a failed read
must be covered. The exercise bypasses real bus binding, so it cannot establish
correct wiring, I2C timing, or driver startup on CircuitPython.

If the snapshot is an error, inspect its `errors` before indexing metrics.
Check the BME280 driver's `relative_humidity` property name. Sensor-not-ready is
a distinct condition for drivers that expose readiness.

## Reference solution and reflection

Replace the TODOs with `"ready"` and `20.5`. For the failure case, define a
class with `relative_humidity = 50.0` and a `temperature` property that raises
the error, then replace `service.driver` by constructing a new `SensorService`.
Expect `phase == "error"` and `metrics == {}`. These frozen dataclasses should
not be mutated in place.

Why calculate derived metrics after base calibration? What extra evidence is
needed before adding a new sensor to factory auto-detection?
