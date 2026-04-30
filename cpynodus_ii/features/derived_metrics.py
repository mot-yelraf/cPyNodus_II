"""Compute derived environmental metrics from normalized sensor readings.

These helpers are intentionally pure so feature services can enrich snapshots
with VPD, dew-point, and related calculated values without embedding the math
inside hardware-facing code.
"""

import math


DEFAULT_PPFD_LUX_FACTOR = 54.0
DLI_SECONDS_PER_DAY_MICROMOL = 0.0864


def enrich_metrics(device, metrics, *, runtime_config):
    """Return a copy of metrics enriched with derived values for the device."""
    enriched = dict(metrics or {})

    if device in {"aht", "aqi", "co2", "avpd", "apvpd", "apvpd_aht"}:
        _add_temp_humidity_derivatives(enriched, humidity_digits=3 if device == "co2" else 1)
    if device in {"apvpd", "apvpd_aht"}:
        _add_prefixed_temp_humidity_derivatives(
            enriched,
            temp_key="Plant Temperature",
            rh_key="Plant Rel-Humidity",
            prefix="Plant",
            humidity_digits=1,
        )

    if device == "aqi":
        enriched["Air Quality"] = estimate_aqi(
            enriched.get("Gas"),
            enriched.get("Rel-Humidity"),
        )

    if device == "lux":
        lux = enriched.get("Light Intensity")
        ppfd = estimate_ppfd_from_lux(lux)
        if ppfd is not None:
            enriched["Estimated PPFD"] = round(ppfd, 0)
            enriched["Visible Light Intensity"] = estimate_dli_from_ppfd(enriched["Estimated PPFD"])

    if device == "soil":
        _add_soil_derivatives(enriched, runtime_config)
        for prefix in _soil_metric_prefixes(enriched):
            _add_soil_derivatives(enriched, runtime_config, prefix=prefix)

    return {key: value for key, value in enriched.items() if value is not None}


def calculate_vpd(temp_c, rh_pct):
    if temp_c is None or rh_pct is None:
        return None
    try:
        temp_c = float(temp_c)
        rh_pct = float(rh_pct)
    except (TypeError, ValueError):
        return None
    es = 0.6108 * (2.718281828 ** ((17.27 * temp_c) / (temp_c + 237.3)))
    ea = es * (rh_pct / 100.0)
    return es - ea


def calculate_dewpoint(temp_c, rh_pct):
    if temp_c is None or rh_pct is None:
        return None
    try:
        temp_c = float(temp_c)
        rh_pct = float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh_pct <= 0:
        return None
    a = 17.62
    b = 243.12
    rh_pct = min(max(rh_pct, 0.1), 100.0)
    gamma = math.log(rh_pct / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def calculate_absolute_humidity(temp_c, rh_pct):
    if temp_c is None or rh_pct is None:
        return None
    try:
        temp_c = float(temp_c)
        rh_pct = float(rh_pct)
    except (TypeError, ValueError):
        return None
    mw = 18.016
    r_const = 8314.3
    svp = 610.78 * 10 ** ((7.5 * temp_c) / (237.3 + temp_c))
    avp = svp * (rh_pct / 100.0)
    temp_k = temp_c + 273.15
    if temp_k <= 0:
        return None
    return (avp * mw) / (r_const * temp_k) * 1000


def calculate_dewpoint_deficit(temp_c, rh_pct):
    dewpoint_c = calculate_dewpoint(temp_c, rh_pct)
    if dewpoint_c is None or temp_c is None:
        return None
    try:
        return float(temp_c) - float(dewpoint_c)
    except (TypeError, ValueError):
        return None


def calculate_dewvpd_risk(
    temp_c,
    rh_pct,
    vpd_kpa,
    *,
    delta_t_low=0.5,
    delta_t_high=4.0,
    vpd_low=0.4,
    vpd_target_low=0.8,
    vpd_target_high=1.2,
    vpd_high=2.0,
    dew_weight=0.65,
):
    if temp_c is None or rh_pct is None or vpd_kpa is None:
        return None
    delta_t = calculate_dewpoint_deficit(temp_c, rh_pct)
    if delta_t is None:
        return None
    try:
        dt_lo = float(delta_t_low)
        dt_hi = float(delta_t_high)
        vp_lo = float(vpd_low)
        vp_target_lo = float(vpd_target_low)
        vp_target_hi = float(vpd_target_high)
        vp_hi = float(vpd_high)
        weight = float(dew_weight)
        vpd_kpa = float(vpd_kpa)
    except (TypeError, ValueError):
        return None
    if dt_hi <= dt_lo or vp_target_lo <= vp_lo or vp_target_hi <= vp_target_lo or vp_hi <= vp_target_hi:
        return None
    weight = min(max(weight, 0.0), 1.0)
    dew_risk = (dt_hi - delta_t) / (dt_hi - dt_lo)
    dew_risk = min(max(dew_risk, 0.0), 1.0)
    if vpd_kpa <= vp_lo:
        vpd_risk = 1.0
    elif vpd_kpa < vp_target_lo:
        vpd_risk = (vp_target_lo - vpd_kpa) / (vp_target_lo - vp_lo)
    elif vpd_kpa <= vp_target_hi:
        vpd_risk = 0.0
    elif vpd_kpa < vp_hi:
        vpd_risk = (vpd_kpa - vp_target_hi) / (vp_hi - vp_target_hi)
    else:
        vpd_risk = 1.0
    return 100.0 * (weight * dew_risk + (1.0 - weight) * vpd_risk)


def estimate_aqi(gas_ohms, rh_percent=None):
    poor_threshold = 5100
    great_threshold = 995100
    if gas_ohms is None or gas_ohms < poor_threshold:
        return 500
    gas_ohms = float(gas_ohms)
    if rh_percent is not None:
        rh_delta = float(rh_percent) - 45.0
        rh_scale = max(-10, min(10, rh_delta * 0.25))
        gas_ohms *= 1 + (rh_scale / 100.0)
    gas_ohms = max(poor_threshold, min(gas_ohms, great_threshold))
    log_val = -math.log(gas_ohms, math.e)
    log_min = -math.log(great_threshold, math.e)
    log_max = -math.log(poor_threshold, math.e)
    scaled = (log_val - log_min) / (log_max - log_min) * 500.0
    return round(min(500, max(0, scaled)))


def estimate_ppfd_from_lux(lux, factor=DEFAULT_PPFD_LUX_FACTOR):
    if lux is None:
        return None
    try:
        return float(lux) / float(factor)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def estimate_dli_from_ppfd(ppfd):
    """Estimate mol/m2/day from the current PPFD value."""
    if ppfd is None:
        return None
    try:
        return round(float(ppfd) * DLI_SECONDS_PER_DAY_MICROMOL, 2)
    except (TypeError, ValueError):
        return None


def _add_temp_humidity_derivatives(metrics, *, humidity_digits):
    _add_prefixed_temp_humidity_derivatives(
        metrics,
        temp_key="Temperature",
        rh_key="Rel-Humidity",
        prefix="",
        humidity_digits=humidity_digits,
    )


def _add_prefixed_temp_humidity_derivatives(metrics, *, temp_key, rh_key, prefix, humidity_digits):
    temp_c = metrics.get(temp_key)
    rh_pct = metrics.get(rh_key)
    label_prefix = "{} ".format(prefix) if prefix else ""
    if temp_c is not None:
        metrics["{}Temperature_F".format(label_prefix)] = round((float(temp_c) * 9.0 / 5.0) + 32.0, 1)
    absolute_humidity = calculate_absolute_humidity(temp_c, rh_pct)
    if absolute_humidity is not None:
        metrics["{}Humidity".format(label_prefix)] = round(absolute_humidity, humidity_digits)
    vpd_kpa = calculate_vpd(temp_c, rh_pct)
    if vpd_kpa is not None:
        metrics["Ambient VPD" if not prefix else "{} VPD".format(prefix)] = round(max(0.0, min(vpd_kpa, 5.0)), 3)
    dewpoint_c = calculate_dewpoint(temp_c, rh_pct)
    if dewpoint_c is not None:
        metrics["{}Dew Point".format(label_prefix)] = round(dewpoint_c, 2)
        metrics["{}Dew Point_F".format(label_prefix)] = round((dewpoint_c * 9.0 / 5.0) + 32.0, 1)
    dew_deficit = calculate_dewpoint_deficit(temp_c, rh_pct)
    if dew_deficit is not None:
        metrics["{}Dew Point Deficit".format(label_prefix)] = round(dew_deficit, 2)
    dew_vpd_risk = calculate_dewvpd_risk(temp_c, rh_pct, vpd_kpa)
    if dew_vpd_risk is not None:
        metrics["DewVPD Risk" if not prefix else "{} DewVPD Risk".format(prefix)] = round(
            max(0.0, min(dew_vpd_risk, 100.0)),
            1,
        )


def _soil_metric_prefixes(metrics):
    prefixes = []
    suffixes = (" Soil Temp_C", " Soil Moisture")
    for key in tuple(metrics.keys()):
        text = str(key or "")
        for suffix in suffixes:
            if text.endswith(suffix):
                prefix = text[: -len(suffix)]
                if prefix and prefix not in prefixes:
                    prefixes.append(prefix)
    return tuple(prefixes)


def _add_soil_derivatives(metrics, runtime_config, prefix=""):
    soil = runtime_config.sensor
    label_prefix = "{} ".format(prefix) if prefix else ""
    temp_c = metrics.get("{}Soil Temp_C".format(label_prefix))
    moisture = metrics.get("{}Soil Moisture".format(label_prefix))
    if temp_c is not None:
        metrics["{}Soil Temp_F".format(label_prefix)] = (
            (float(temp_c) * 9.0 / 5.0) + 32.0
        )

    wet = getattr(getattr(soil, "soil_thresholds", None), "wet_pct", None)
    dry = getattr(getattr(soil, "soil_thresholds", None), "dry_pct", None)
    if (
        moisture is not None
        and wet is not None
        and dry is not None
        and float(wet) > float(dry)
    ):
        deficit = 100.0 * ((float(wet) - float(moisture)) / (float(wet) - float(dry)))
        metrics["{}Soil Moisture Deficit".format(label_prefix)] = round(
            min(max(deficit, 0.0), 100.0),
            1,
        )

    stress = getattr(soil, "soil_stress", None)
    deficit = metrics.get("{}Soil Moisture Deficit".format(label_prefix))
    temp_stress = _soil_temp_stress_pct(temp_c, stress)
    if deficit is not None and temp_stress is not None and stress is not None:
        total = float(stress.moisture_weight_pct) + float(stress.temp_weight_pct)
        if total > 0:
            ssi = (
                (
                    float(deficit) * float(stress.moisture_weight_pct)
                ) + (temp_stress * float(stress.temp_weight_pct))
            ) / total
            metrics["{}Soil Stress Index".format(label_prefix)] = round(
                min(max(ssi, 0.0), 100.0),
                1,
            )


def _soil_temp_stress_pct(temp_c, stress):
    if temp_c is None or stress is None:
        return None
    low_crit = float(stress.temp_low_crit_c)
    low_ok = float(stress.temp_low_ok_c)
    high_ok = float(stress.temp_high_ok_c)
    high_crit = float(stress.temp_high_crit_c)
    if not (low_crit < low_ok <= high_ok < high_crit):
        return None
    temp_c = float(temp_c)
    if temp_c <= low_crit or temp_c >= high_crit:
        return 100.0
    if low_ok <= temp_c <= high_ok:
        return 0.0
    if temp_c < low_ok:
        return min(max(100.0 * ((low_ok - temp_c) / (low_ok - low_crit)), 0.0), 100.0)
    return min(max(100.0 * ((temp_c - high_ok) / (high_crit - high_ok)), 0.0), 100.0)
