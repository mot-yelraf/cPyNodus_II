"""Register canonical Nodus observations and units with WeeWX.

``install_units`` updates WeeWX's global observation, format, label, and
conversion tables; ``NodusUnits`` installs them as a service. Registration is
idempotent so repeated service initialization does not duplicate converters.
"""

import weewx.units
from weewx.engine import StdService

OBS_GROUPS = {
    "vpd": "group_nodus_vpd",
    "absoluteHumidity": "group_nodus_absolute_humidity",
    "dewpointDepression": "group_nodus_delta_temperature",
    "dewVpdRisk": "group_percent",
    "gasResistance": "group_nodus_resistance",
    "airQuality": "group_nodus_index",
    "co2": "group_fraction",
    "soilTemperature": "group_temperature",
    "soilMoisturePct": "group_percent",
    "soilMoistureDeficit": "group_percent",
    "soilStressIndex": "group_percent",
    "soilPh": "group_nodus_ph",
    "soilEc": "group_nodus_conductivity",
    "soilNitrogen": "group_nodus_mass_ratio",
    "soilPhosphorus": "group_nodus_mass_ratio",
    "soilPotassium": "group_nodus_mass_ratio",
    "soilFertilityIndex": "group_percent",
}

GROUP_UNITS = (
    ("group_nodus_vpd", "kPa", "kPa"),
    (
        "group_nodus_absolute_humidity",
        "gram_per_meter_cubed",
        "gram_per_meter_cubed",
    ),
    (
        "group_nodus_delta_temperature",
        "degree_F_delta",
        "degree_C_delta",
    ),
    ("group_nodus_resistance", "ohm", "ohm"),
    ("group_nodus_ph", "pH", "pH"),
    (
        "group_nodus_conductivity",
        "millisiemens_per_centimeter",
        "millisiemens_per_centimeter",
    ),
    (
        "group_nodus_mass_ratio",
        "milligram_per_kilogram",
        "milligram_per_kilogram",
    ),
    ("group_nodus_index", "index", "index"),
)

UNIT_FORMATS = {
    "kPa": "%.3f",
    "gram_per_meter_cubed": "%.2f",
    "degree_C_delta": "%.2f",
    "degree_F_delta": "%.2f",
    "ohm": "%.0f",
    "pH": "%.1f",
    "millisiemens_per_centimeter": "%.2f",
    "milligram_per_kilogram": "%.0f",
    "index": "%.0f",
}

UNIT_LABELS = {
    "kPa": " kPa",
    "gram_per_meter_cubed": " g/m³",
    "degree_C_delta": " °C",
    "degree_F_delta": " °F",
    "ohm": " Ω",
    "pH": " pH",
    "millisiemens_per_centimeter": " mS/cm",
    "milligram_per_kilogram": " mg/kg",
    "index": "",
}


def install_units():
    """Install canonical Nodus observation groups and display units."""
    for obs_name, group_name in OBS_GROUPS.items():
        weewx.units.obs_group_dict[obs_name] = group_name
    for group_name, us_unit, metric_unit in GROUP_UNITS:
        weewx.units.USUnits[group_name] = us_unit
        weewx.units.MetricUnits[group_name] = metric_unit
        weewx.units.MetricWXUnits[group_name] = metric_unit
    for unit_name in UNIT_FORMATS:
        if unit_name not in weewx.units.conversionDict:
            weewx.units.conversionDict[unit_name] = {}
    weewx.units.conversionDict["degree_C_delta"] = {
        "degree_F_delta": lambda value: value * 9.0 / 5.0,
    }
    weewx.units.conversionDict["degree_F_delta"] = {
        "degree_C_delta": lambda value: value * 5.0 / 9.0,
    }
    weewx.units.default_unit_format_dict.update(UNIT_FORMATS)
    weewx.units.default_unit_label_dict.update(UNIT_LABELS)


class NodusUnits(StdService):
    """Register Nodus units before packet conversion and reporting."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)
        install_units()
