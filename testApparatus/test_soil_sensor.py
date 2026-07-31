"""Identify soil-probe registers over RS485 Modbus RTU.

``main`` probes both Waveshare Pico-2CH-RS485 HAT channels, supported baud
rates, addresses, and function codes before entering an optional measurement
loop. Run it only while the normal firmware application is stopped.

Workflow:

1. Probe channel, baud, address, and function-code combinations.
2. Detect 2-in-1, 4-in-1, or 7-in-1 profiles from readable register counts.
3. Sweep likely register ranges and compare candidate layouts.
4. Poll measurements using the selected profile.
"""

import time

import board
import busio

SCRIPT_VERSION = "soil_test_v8_register_id_2026-06-10"

CHANNELS = {
    "CH1": (board.GP0, board.GP1),
    "CH2": (board.GP4, board.GP5),
}

ADDRS = range(1, 6)
BAUDS = [9600, 4800, 2400]
FUNCTION_CODES = (0x03, 0x04)
POLL_INTERVAL_S = 10.0
SWEEP_START_REG = 0x0000
SWEEP_END_REG = 0x0040
SWEEP_CHUNK = 8
EXTRA_SWEEP_RANGES = (
    (0x0100, 0x0110),
    (0x07D0, 0x07D3),
)

METRIC_SCALES = {
    "moisture": 10.0,
    "temperature": 10.0,
    "ec": 1.0,
    "ph": 10.0,
    "n": 1.0,
    "p": 1.0,
    "k": 1.0,
}

METRIC_LABELS = {
    "moisture": "Soil Moisture",
    "temperature": "Soil Temp_C",
    "ec": "Soil EC",
    "ph": "Soil pH",
    "n": "Soil Nitrogen",
    "p": "Soil Phosphorus",
    "k": "Soil Potassium",
}

CANDIDATE_LAYOUTS = (
    (
        "nodus_default_temp_first",
        {
            "temperature": 0x0000,
            "moisture": 0x0001,
            "ec": 0x0002,
            "ph": 0x0003,
            "n": 0x0004,
            "p": 0x0005,
            "k": 0x0006,
        },
    ),
    (
        "observed_temp_first_ph7_ec12",
        {
            "temperature": 0x0000,
            "moisture": 0x0001,
            "ec": 0x000C,
            "ph": 0x0007,
            "n": 0x0004,
            "p": 0x0005,
            "k": 0x0006,
        },
    ),
    (
        "common_moisture_first",
        {
            "moisture": 0x0000,
            "temperature": 0x0001,
            "ec": 0x0002,
            "ph": 0x0003,
            "n": 0x0004,
            "p": 0x0005,
            "k": 0x0006,
        },
    ),
    (
        "moisture_first_npk_0x001e",
        {
            "moisture": 0x0000,
            "temperature": 0x0001,
            "ec": 0x0002,
            "ph": 0x0003,
            "n": 0x001E,
            "p": 0x001F,
            "k": 0x0020,
        },
    ),
)


def crc16_modbus(buf: bytes) -> int:
    crc = 0xFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            if (crc & 0x0001) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc


def build_read_req(
    addr: int,
    start: int,
    count: int,
    function_code: int = 0x03,
) -> bytes:
    frame = bytearray(
        [
            addr & 0xFF,
            function_code & 0xFF,
            (start >> 8) & 0xFF,
            start & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    crc = crc16_modbus(frame)
    frame += bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    return bytes(frame)


def _parse_register_response(resp, addr, function_code, reg_count):
    if not resp:
        return None

    expected_len = 5 + (int(reg_count) * 2)
    if len(resp) < expected_len:
        return None

    max_start = len(resp) - expected_len
    for start in range(max_start + 1):
        frame = resp[start : start + expected_len]
        body, lo, hi = frame[:-2], frame[-2], frame[-1]
        calc = crc16_modbus(body)
        if (lo != (calc & 0xFF)) or (hi != ((calc >> 8) & 0xFF)):
            continue
        if frame[0] != addr:
            continue
        if frame[1] == (function_code | 0x80):
            return None
        if frame[1] != function_code:
            continue

        byte_count = frame[2]
        data = frame[3 : 3 + byte_count]
        if byte_count != reg_count * 2 or len(data) != byte_count:
            continue

        regs = []
        for i in range(0, byte_count, 2):
            regs.append((data[i] << 8) | data[i + 1])
        return regs
    return None


def read_holding_regs(
    uart: busio.UART,
    addr: int,
    start_reg: int,
    reg_count: int,
    label: str = "",
    function_code: int = 0x03,
):
    req = build_read_req(addr, start_reg, reg_count, function_code=function_code)
    try:
        uart.reset_input_buffer()
    except Exception:
        pass
    uart.write(req)
    try:
        uart.flush()
    except Exception:
        pass
    time.sleep(0.05)

    return _parse_register_response(
        uart.read(128),
        addr,
        function_code,
        reg_count,
    )


def _s16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v


def _function_label(function_code):
    if int(function_code) == 0x03:
        return "holding"
    if int(function_code) == 0x04:
        return "input"
    return "func_0x{code:02X}".format(code=int(function_code))


def prompt_sensor_profile():
    print("Select soil sensor type:")
    print("  1) 2-in-1 (moisture + temp)")
    print("  2) 4-in-1 (moisture + temp + EC + pH)")
    print("  3) 7-in-1 (moisture + temp + EC + pH + NPK)")
    print("  4) Auto Detect")
    raw = input("Enter 1, 2, 3, or 4 [default 4]: ").strip().lower()

    if raw in ("3", "7", "7in1", "soil_7in1", "canonical"):
        return "soil_7in1"
    if raw in ("2", "4", "4in1", "soil_4in1"):
        return "soil_4in1"
    if raw in ("1", "2in1", "soil_2in1"):
        return "soil_2in1"
    return "auto"


def detect_sensor_profile(
    uart: busio.UART,
    addr: int,
    label: str = "",
    function_code: int = 0x03,
):
    regs7 = read_holding_regs(
        uart,
        addr,
        0x0000,
        7,
        label=label,
        function_code=function_code,
    )
    if regs7 is not None and len(regs7) == 7:
        return "soil_7in1"

    regs4 = read_holding_regs(
        uart,
        addr,
        0x0000,
        4,
        label=label,
        function_code=function_code,
    )
    if regs4 is not None and len(regs4) == 4:
        return "soil_4in1"

    regs2 = read_holding_regs(
        uart,
        addr,
        0x0000,
        2,
        label=label,
        function_code=function_code,
    )
    if regs2 is not None and len(regs2) == 2:
        return "soil_2in1"

    return None


def read_measurement(
    uart: busio.UART,
    addr: int,
    profile: str,
    label: str = "",
    function_code: int = 0x03,
    layout=None,
):
    if layout:
        by_register = _read_layout_register_values(
            uart,
            addr,
            function_code,
            layout,
        )
        if not by_register:
            print(label + "No/invalid response")
            return None
        decoded = _decode_layout_values(by_register, layout)
        if decoded:
            decoded["profile"] = profile
            return decoded
        print(label + "No decoded layout metrics")
        return None

    count = 2
    if profile in ("soil_4in1", "canonical", "soil_7in1"):
        count = 4
    if profile in ("canonical", "soil_7in1"):
        count = 7

    regs = read_holding_regs(
        uart,
        addr,
        0x0000,
        count,
        label=label,
        function_code=function_code,
    )
    if regs is None or len(regs) < count:
        print(label + "No/invalid response")
        return None

    out = {
        "moisture_pct": regs[0] / 10.0,
        "temp_c": _s16(regs[1]) / 10.0,
        "profile": profile,
    }
    out["temp_f"] = (out["temp_c"] * 9.0 / 5.0) + 32.0

    if count >= 4:
        out["ec_us_cm"] = regs[2]
        out["ph_raw"] = regs[3]
        out["ph"] = regs[3] / 10.0
    if count >= 7:
        out["n_mg_kg"] = regs[4]
        out["p_mg_kg"] = regs[5]
        out["k_mg_kg"] = regs[6]

    return out


def print_metrics(metrics, label=""):
    profile = metrics.get("profile", "unknown")
    base = (
        label
        + "profile={p} Moisture %: {m}, Temp C: {t}, Temp F: {tf}".format(
            p=profile,
            m=metrics.get("moisture_pct"),
            t=metrics.get("temp_c"),
            tf=metrics.get("temp_f"),
        )
    )

    if "ph" in metrics and "ec_us_cm" in metrics:
        base += ", pH_raw: {pr}, pH: {ph}, EC (uS/cm): {ec}".format(
            pr=metrics.get("ph_raw"),
            ph=metrics.get("ph"),
            ec=metrics.get("ec_us_cm"),
        )
    if "n_mg_kg" in metrics:
        base += ", N: {n} mg/kg, P: {p} mg/kg, K: {k} mg/kg".format(
            n=metrics.get("n_mg_kg"),
            p=metrics.get("p_mg_kg"),
            k=metrics.get("k_mg_kg"),
        )
    print(base)


def _read_register_snapshot(uart, addr, function_code, regions, chunk):
    values = {}
    for start_reg, end_reg in regions:
        reg = int(start_reg)
        while reg <= int(end_reg):
            remaining = int(end_reg) - reg + 1
            count = chunk if remaining >= chunk else remaining
            regs = read_holding_regs(
                uart,
                addr,
                reg,
                count,
                function_code=function_code,
            )
            if regs is None and count > 1:
                regs = []
                for offset in range(count):
                    single = read_holding_regs(
                        uart,
                        addr,
                        reg + offset,
                        1,
                        function_code=function_code,
                    )
                    if single is None:
                        regs.append(None)
                    else:
                        regs.append(single[0])
                    time.sleep(0.01)
            if regs is not None:
                for i, val in enumerate(regs):
                    if val is not None:
                        values[reg + i] = int(val)
            reg += count
            time.sleep(0.02)
    return values


def _read_layout_register_values(uart, addr, function_code, layout):
    values = {}
    seen = []
    for reg in layout.values():
        if reg in seen:
            continue
        seen.append(reg)
        regs = read_holding_regs(
            uart,
            addr,
            int(reg),
            1,
            function_code=function_code,
        )
        if regs is not None and len(regs) == 1:
            values[int(reg)] = int(regs[0])
        time.sleep(0.02)
    return values


def _format_register_pairs(items):
    return ", ".join(["0x{r:04X}={v}".format(r=r, v=v) for (r, v) in items])


def _metric_value(metric, raw):
    if raw is None:
        return None
    if metric == "temperature":
        return _s16(int(raw)) / METRIC_SCALES[metric]
    return float(raw) / METRIC_SCALES.get(metric, 1.0)


def _plausible_metric(metric, value):
    if value is None:
        return False
    if metric == "temperature":
        return -30.0 <= value <= 80.0
    if metric == "moisture":
        return 0.0 <= value <= 100.0
    if metric == "ph":
        return 3.0 <= value <= 10.5
    if metric == "ec":
        return 0.0 <= value <= 50000.0
    return 0.0 <= value <= 100000.0


def _decode_layout_values(register_values, layout):
    decoded = {}
    for metric, reg in layout.items():
        raw = register_values.get(reg)
        value = _metric_value(metric, raw)
        if value is None:
            continue
        if metric == "moisture":
            decoded["moisture_pct"] = value
        elif metric == "temperature":
            decoded["temp_c"] = value
            decoded["temp_f"] = (value * 9.0 / 5.0) + 32.0
        elif metric == "ec":
            decoded["ec_us_cm"] = value
        elif metric == "ph":
            decoded["ph_raw"] = raw
            decoded["ph"] = value
        elif metric == "n":
            decoded["n_mg_kg"] = value
        elif metric == "p":
            decoded["p_mg_kg"] = value
        elif metric == "k":
            decoded["k_mg_kg"] = value
    return decoded


def _score_layout(register_values, layout):
    present = 0
    plausible = 0
    for metric, reg in layout.items():
        raw = register_values.get(reg)
        if raw is None:
            continue
        present += 1
        if _plausible_metric(metric, _metric_value(metric, raw)):
            plausible += 1
    return present, plausible


def _print_layout_candidate(label, name, layout, register_values):
    present, plausible = _score_layout(register_values, layout)
    print(
        label
        + "layout {name}: present={present}/7 plausible={plausible}/7".format(
            name=name, present=present, plausible=plausible
        )
    )
    parts = []
    for metric in ("temperature", "moisture", "ec", "ph", "n", "p", "k"):
        reg = layout.get(metric)
        raw = register_values.get(reg)
        value = _metric_value(metric, raw)
        if raw is None:
            parts.append("{metric}@0x{reg:04X}=missing".format(metric=metric, reg=reg))
        else:
            parts.append(
                "{metric}@0x{reg:04X}=raw:{raw} scaled:{value}".format(
                    metric=metric,
                    reg=reg,
                    raw=raw,
                    value=value,
                )
            )
    print(label + "  " + "; ".join(parts))


def _print_toml_candidate(label, layout):
    print(label + "candidate sensor_soil.toml map:")
    print("[SoilSensorRegisters]")
    print("TEMPERATURE_REG = {reg}".format(reg=layout["temperature"]))
    print("MOISTURE_REG = {reg}".format(reg=layout["moisture"]))
    print("EC_REG = {reg}".format(reg=layout["ec"]))
    print("PH_REG = {reg}".format(reg=layout["ph"]))
    print("N_REG = {reg}".format(reg=layout["n"]))
    print("P_REG = {reg}".format(reg=layout["p"]))
    print("K_REG = {reg}".format(reg=layout["k"]))


def analyze_register_map(snapshots):
    if not snapshots:
        return
    print("\nAnalyzing candidate register layouts.")
    print(
        "Use the raw sweep plus a known condition change to confirm the winner; "
        "plausible scores are hints, not proof."
    )
    for snapshot in snapshots:
        c = snapshot["contact"]
        register_values = snapshot["values"]
        label = "[{ch} @ {baud} addr {addr} {func}] ".format(
            ch=c["channel"],
            baud=c["baud"],
            addr=c["addr"],
            func=_function_label(c.get("function_code", 0x03)),
        )
        best = None
        best_score = (-1, -1)
        tied_best = False
        for name, layout in CANDIDATE_LAYOUTS:
            _print_layout_candidate(label, name, layout, register_values)
            score = _score_layout(register_values, layout)
            score_key = (score[1], score[0])
            if score_key > best_score:
                best = (name, layout)
                best_score = score_key
                tied_best = False
            elif score_key == best_score:
                tied_best = True
        if best is not None and not tied_best:
            print(label + "best hint: {name}".format(name=best[0]))
            c["layout_name"] = best[0]
            c["layout"] = best[1]
            _print_toml_candidate(label, best[1])
        elif best is not None:
            print(
                label
                + "best hint: ambiguous; use the raw values and a known condition "
                "change to choose the register map"
            )


def _pin_name(pin):
    for name, pins in CHANNELS.items():
        if pins[0] is pin:
            return "GP0" if name == "CH1" else "GP4"
        if pins[1] is pin:
            return "GP1" if name == "CH1" else "GP5"
    return str(pin or "")


def print_contact_summary(contacts):
    if not contacts:
        return
    print("\nIdentified soil contact summary.")
    for c in contacts:
        label = "[{ch} @ {baud} addr {addr} {func}] ".format(
            ch=c["channel"],
            baud=c["baud"],
            addr=c["addr"],
            func=_function_label(c.get("function_code", 0x03)),
        )
        print(
            label
            + "profile={profile} tx={tx} rx={rx}".format(
                profile=c.get("profile", ""),
                tx=_pin_name(c.get("tx")),
                rx=_pin_name(c.get("rx")),
            )
        )
        print("[Modbus.{channel}]".format(channel=c["channel"]))
        print('UART_TX = "{pin}"'.format(pin=_pin_name(c.get("tx"))))
        print('UART_RX = "{pin}"'.format(pin=_pin_name(c.get("rx"))))
        print("MODBUS_ADDR = {addr}".format(addr=c["addr"]))
        print("MODBUS_BAUD = {baud}".format(baud=c["baud"]))
        print("MODBUS_TIMEOUT_S = 0.30")
        print('SOIL_VARIANT = "{variant}"'.format(variant=c.get("profile", "")))
        layout = c.get("layout")
        if layout:
            _print_toml_candidate(label, layout)
        else:
            print(label + "register map unresolved; inspect layout candidates above")


def prompt_measurement_loop():
    raw = input("Enter 10-second measurement loop? [y/N]: ").strip().lower()
    return raw in ("y", "yes")


def sweep_registers(
    contacts,
    start_reg=SWEEP_START_REG,
    end_reg=SWEEP_END_REG,
    chunk=SWEEP_CHUNK,
):
    if not contacts:
        return []
    print(
        "\nStarting register sweep (0x{start:04X}..0x{end:04X}, chunk={chunk})".format(
            start=start_reg, end=end_reg, chunk=chunk
        )
    )

    snapshots = []
    regions = ((start_reg, end_reg),) + EXTRA_SWEEP_RANGES
    for c in contacts:
        function_code = c.get("function_code", 0x03)
        uart = busio.UART(c["tx"], c["rx"], baudrate=c["baud"], timeout=0.3)
        label = "[{ch} @ {baud} addr {addr} {func}] ".format(
            ch=c["channel"],
            baud=c["baud"],
            addr=c["addr"],
            func=_function_label(function_code),
        )
        try:
            values = _read_register_snapshot(
                uart,
                c["addr"],
                function_code,
                regions,
                chunk,
            )
            non_zero = [
                (reg, value) for reg, value in sorted(values.items()) if int(value) != 0
            ]
            snapshots.append({"contact": c, "values": values})

            print(
                label
                + "readable registers: {readable}; non-zero: {non_zero}".format(
                    readable=len(values),
                    non_zero=len(non_zero),
                )
            )
            if non_zero:
                print(label + _format_register_pairs(non_zero))
            else:
                print(label + "none in sweep range")
        finally:
            try:
                uart.deinit()
            except Exception:
                pass
        time.sleep(0.1)
    return snapshots


def probe_contacts(profile: str):
    successes = []
    for ch_name, (uart_tx, uart_rx) in CHANNELS.items():
        for baud in BAUDS:
            print("\n[{}] probing baud {}".format(ch_name, baud))
            uart = busio.UART(uart_tx, uart_rx, baudrate=baud, timeout=0.3)
            try:
                for addr in ADDRS:
                    for function_code in FUNCTION_CODES:
                        label = "[{} @ {} addr {} {}] ".format(
                            ch_name,
                            baud,
                            addr,
                            _function_label(function_code),
                        )
                        print(label + "probe start")
                        active_profile = profile
                        if profile == "auto":
                            active_profile = detect_sensor_profile(
                                uart,
                                addr,
                                label=label,
                                function_code=function_code,
                            )
                            if active_profile is None:
                                print(label + "Auto Detect failed")
                                time.sleep(0.1)
                                continue
                            print(label + "Auto Detect -> {}".format(active_profile))

                        metrics = read_measurement(
                            uart,
                            addr,
                            active_profile,
                            label=label,
                            function_code=function_code,
                        )
                        if metrics is not None:
                            print(label + "contact success")
                            print_metrics(metrics, label=label)
                            successes.append(
                                {
                                    "channel": ch_name,
                                    "tx": uart_tx,
                                    "rx": uart_rx,
                                    "baud": baud,
                                    "addr": addr,
                                    "profile": active_profile,
                                    "function_code": function_code,
                                }
                            )
                            break
                        else:
                            print(label + "No/invalid response")
                        time.sleep(0.1)
            finally:
                try:
                    uart.deinit()
                except Exception:
                    pass
            time.sleep(0.2)
    return successes


def measurement_loop(contacts):
    if not contacts:
        print("\nNo successful contacts found during probe phase.")
        return

    print(
        "\nStarting 10-second measurement loop for {} contact(s).".format(
            len(contacts)
        )
    )
    print("Press Ctrl+C to stop.\n")

    active = []
    for c in contacts:
        uart = busio.UART(c["tx"], c["rx"], baudrate=c["baud"], timeout=0.3)
        c2 = dict(c)
        c2["uart"] = uart
        active.append(c2)

    try:
        while True:
            cycle_start = time.monotonic()
            print("=== cycle @ {:.1f}s ===".format(cycle_start))
            for c in active:
                label = "[{} @ {} addr {} {}] ".format(
                    c["channel"],
                    c["baud"],
                    c["addr"],
                    _function_label(c.get("function_code", 0x03)),
                )
                metrics = read_measurement(
                    c["uart"],
                    c["addr"],
                    c.get("profile", "unknown"),
                    label=label,
                    function_code=c.get("function_code", 0x03),
                    layout=c.get("layout"),
                )
                if metrics is not None:
                    print_metrics(metrics, label=label)
            elapsed = time.monotonic() - cycle_start
            sleep_s = POLL_INTERVAL_S - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\nMeasurement loop stopped by user.")
    finally:
        for c in active:
            try:
                c["uart"].deinit()
            except Exception:
                pass


def main():
    print("=== {} ===".format(SCRIPT_VERSION))
    profile = prompt_sensor_profile()
    print("Using profile: {}".format(profile))
    contacts = probe_contacts(profile)
    snapshots = sweep_registers(contacts)
    analyze_register_map(snapshots)
    print_contact_summary(contacts)
    if prompt_measurement_loop():
        print("=== entering measurement loop ({}) ===".format(SCRIPT_VERSION))
        measurement_loop(contacts)
    else:
        print("=== done ({}) ===".format(SCRIPT_VERSION))


if __name__ == "__main__":
    main()
