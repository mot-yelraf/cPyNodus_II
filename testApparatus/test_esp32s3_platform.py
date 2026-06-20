"""Standalone XIAO ESP32-S3 GPIO and ADC platform apparatus.

Copy this file to the CIRCUITPY root and run it while the normal app is
stopped:

    import test_esp32s3_platform
    test_esp32s3_platform.run()

The default run probes every configured XIAO ESP32-S3 GPIO alias except
GPIO43/GPIO44. Pass ``pins=...`` only when intentionally filtering the run.

Focus on GPIO5:

    test_esp32s3_platform.run(pins=5, samples=5, sample_interval_s=1.0)

By default the apparatus does not drive output levels. Disconnect external
circuits before enabling output tests:

    test_esp32s3_platform.run(drive_outputs=True)

The apparatus tests XIAO ESP32-S3 GPIO aliases D0-D12 and intentionally skips
GPIO43 and GPIO44. It prints each attempted configuration and observed status.
It does not write files or change settings. Output-drive mode should only be
used with external circuits disconnected.
"""

SCRIPT_VERSION = "esp32s3_platform_test_v3_2026-06-19"
IMPORT_MARKER = "test_esp32s3_platform_loaded_v3"

print(
    "test_esp32s3_platform import version={} marker={}".format(
        SCRIPT_VERSION,
        IMPORT_MARKER,
    )
)

PIN_SPECS = (
    (1, ("D0", "A0", "IO1", "GPIO1")),
    (2, ("D1", "A1", "IO2", "GPIO2")),
    (3, ("D2", "A2", "IO3", "GPIO3")),
    (4, ("D3", "A3", "IO4", "GPIO4")),
    (5, ("D4", "A4", "SDA", "IO5", "GPIO5")),
    (6, ("D5", "A5", "SCL", "IO6", "GPIO6")),
    (7, ("D8", "SCK", "IO7", "GPIO7")),
    (8, ("D9", "MISO", "IO8", "GPIO8")),
    (9, ("D10", "MOSI", "IO9", "GPIO9")),
    (42, ("D11", "A11", "IO42", "GPIO42")),
    (41, ("D12", "A12", "IO41", "GPIO41")),
)

SKIPPED_PIN_SPECS = (
    (43, ("D6", "TX", "IO43", "GPIO43")),
    (44, ("D7", "RX", "IO44", "GPIO44")),
)


def run(
    pins=None,
    samples=1,
    sample_interval_s=0.0,
    settle_s=0.03,
    drive_outputs=False,
):
    """Probe XIAO ESP32-S3 GPIO digital pulls and ADC support."""
    import gc
    import time

    try:
        import board
    except ImportError:
        print("esp32s3_platform phase=done result=fail error=board_unavailable")
        return False

    try:
        import digitalio
    except ImportError:
        digitalio = None

    try:
        import analogio
    except ImportError:
        analogio = None

    gc.collect()
    selected = _pin_filter(pins)
    sample_count = max(1, int(samples or 1))
    interval = max(0.0, float(sample_interval_s or 0.0))
    settle = max(0.0, float(settle_s or 0.0))
    print(
        (
            "esp32s3_platform phase=start version={} marker={} board={} "
            "pins={} samples={} interval_s={} drive_outputs={}"
        ).format(
            SCRIPT_VERSION,
            IMPORT_MARKER,
            str(getattr(board, "board_id", "") or "unknown"),
            _join(selected) if selected is not None else "all",
            sample_count,
            interval,
            1 if drive_outputs else 0,
        )
    )

    for gpio, aliases in SKIPPED_PIN_SPECS:
        if _selected(gpio, selected):
            present = _present_aliases(board, aliases)
            message = (
                "pin gpio={} aliases={} present={} status=skipped "
                "reason=requested_skip"
            )
            print(
                message.format(
                    gpio,
                    _join(present) or "none",
                    1 if present else 0,
                )
            )

    probed = 0
    for gpio, aliases in PIN_SPECS:
        if not _selected(gpio, selected):
            continue
        probed += 1
        _probe_pin(
            board,
            digitalio,
            analogio,
            time,
            gpio,
            aliases,
            sample_count,
            interval,
            settle,
            drive_outputs,
        )
        gc.collect()

    print(
        "esp32s3_platform phase=done result=pass probed={} skipped_gpio=43,44".format(
            probed
        )
    )
    return True


def _probe_pin(
    board,
    digitalio,
    analogio,
    time,
    gpio,
    aliases,
    samples,
    interval,
    settle,
    drive_outputs,
):
    pin, alias = _resolve_pin(board, aliases)
    present = _present_aliases(board, aliases)
    print(
        "pin gpio={} alias={} aliases_present={} present={}".format(
            gpio,
            alias or "none",
            _join(present) or "none",
            1 if pin is not None else 0,
        )
    )
    if pin is None:
        return

    for sample in range(1, samples + 1):
        _digital_input(digitalio, time, gpio, alias, pin, "none", None, sample, settle)
        pull_up = getattr(getattr(digitalio, "Pull", None), "UP", None)
        pull_down = getattr(getattr(digitalio, "Pull", None), "DOWN", None)
        _digital_input(digitalio, time, gpio, alias, pin, "up", pull_up, sample, settle)
        _digital_input(
            digitalio,
            time,
            gpio,
            alias,
            pin,
            "down",
            pull_down,
            sample,
            settle,
        )
        _analog_input(analogio, time, gpio, alias, pin, sample, settle)
        if drive_outputs:
            _digital_output(digitalio, time, gpio, alias, pin, False, sample, settle)
            _digital_output(digitalio, time, gpio, alias, pin, True, sample, settle)
        else:
            message = (
                "digital gpio={} alias={} sample={} mode=output "
                "status=skipped reason=drive_outputs_disabled"
            )
            print(
                message.format(
                    gpio,
                    alias,
                    sample,
                )
            )
        if sample < samples and interval:
            time.sleep(interval)


def _digital_input(digitalio, time, gpio, alias, pin, pull_name, pull, sample, settle):
    if digitalio is None:
        message = (
            "digital gpio={} alias={} sample={} mode=input pull={} "
            "status=error error=digitalio_unavailable"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                pull_name,
            )
        )
        return
    dio = None
    try:
        dio = digitalio.DigitalInOut(pin)
        dio.switch_to_input(pull=pull)
        if settle:
            time.sleep(settle)
        message = (
            "digital gpio={} alias={} sample={} mode=input pull={} "
            "value={} status=ok"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                pull_name,
                _level(dio.value),
            )
        )
    except Exception as exc:
        message = (
            "digital gpio={} alias={} sample={} mode=input pull={} "
            "status=error error={}:{}"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                pull_name,
                type(exc).__name__,
                _clean(exc),
            )
        )
    _deinit(dio)


def _digital_output(digitalio, time, gpio, alias, pin, value, sample, settle):
    if digitalio is None:
        return
    dio = None
    try:
        dio = digitalio.DigitalInOut(pin)
        dio.switch_to_output(value=value)
        if settle:
            time.sleep(settle)
        message = (
            "digital gpio={} alias={} sample={} mode=output drive={} "
            "value={} status=ok"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                _level(value),
                _level(dio.value),
            )
        )
    except Exception as exc:
        message = (
            "digital gpio={} alias={} sample={} mode=output drive={} "
            "status=error error={}:{}"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                _level(value),
                type(exc).__name__,
                _clean(exc),
            )
        )
    _deinit(dio)


def _analog_input(analogio, time, gpio, alias, pin, sample, settle):
    if analogio is None:
        message = (
            "analog gpio={} alias={} sample={} status=error "
            "error=analogio_unavailable"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
            )
        )
        return
    adc = None
    try:
        adc = analogio.AnalogIn(pin)
        if settle:
            time.sleep(settle)
        raw = int(adc.value)
        ref = float(getattr(adc, "reference_voltage", 0.0) or 0.0)
        voltage = "unknown"
        if ref > 0:
            voltage = "{:.4f}".format((float(raw) * ref) / 65535.0)
        message = (
            "analog gpio={} alias={} sample={} raw={} voltage={} ref_v={:.4f} "
            "status=ok"
        )
        print(
            message.format(
                gpio,
                alias,
                sample,
                raw,
                voltage,
                ref,
            )
        )
    except Exception as exc:
        print(
            "analog gpio={} alias={} sample={} status=error error={}:{}".format(
                gpio,
                alias,
                sample,
                type(exc).__name__,
                _clean(exc),
            )
        )
    _deinit(adc)


def _resolve_pin(board, aliases):
    for alias in aliases:
        pin = getattr(board, alias, None)
        if pin is not None:
            return pin, alias
    return None, ""


def _present_aliases(board, aliases):
    result = []
    for alias in aliases:
        if getattr(board, alias, None) is not None:
            result.append(alias)
    return tuple(result)


def _pin_filter(pins):
    if pins is None:
        return None
    if isinstance(pins, (int, str)):
        pins = (pins,)
    result = []
    for item in pins:
        gpio = _gpio_number(item)
        if gpio is not None and gpio not in result:
            result.append(gpio)
    return tuple(result)


def _gpio_number(value):
    if isinstance(value, int):
        return value
    text = str(value or "").strip().upper()
    if text.startswith("GPIO"):
        return _to_int(text[4:])
    if text.startswith("IO"):
        return _to_int(text[2:])
    if text.isdigit():
        return int(text)
    for gpio, aliases in PIN_SPECS + SKIPPED_PIN_SPECS:
        for alias in aliases:
            if text == alias.upper():
                return gpio
    return None


def _selected(gpio, selected):
    return selected is None or gpio in selected


def _level(value):
    if value is True:
        return "high"
    if value is False:
        return "low"
    return "unknown"


def _join(values):
    return ",".join(str(value) for value in values or ())


def _deinit(obj):
    if obj is None:
        return
    try:
        obj.deinit()
    except Exception:
        pass


def _clean(exc):
    text = str(exc) or "unknown"
    for old, new in (("\r", " "), ("\n", " "), ("\t", " "), (" ", "_")):
        text = text.replace(old, new)
    return text


def _to_int(value):
    try:
        return int(value)
    except Exception:
        return None


main = run
