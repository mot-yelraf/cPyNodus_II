"""Extend the WeeWX archive schema with canonical Nodus observations.

Importing this module copies the standard extended schema and adds any missing
Nodus scalar columns and day summaries. The resulting ``schema`` object is the
public value consumed by WeeWX configuration.
"""

from weewx.schemas.wview_extended import schema as _weewx_schema

_NODUS_COLUMNS = (
    "vpd",
    "absoluteHumidity",
    "dewpointDepression",
    "dewVpdRisk",
    "gasResistance",
    "airQuality",
    "co2",
    "soilTemperature",
    "soilMoisturePct",
    "soilMoistureDeficit",
    "soilStressIndex",
    "soilPh",
    "soilEc",
    "soilNitrogen",
    "soilPhosphorus",
    "soilPotassium",
    "soilFertilityIndex",
)

schema = dict(_weewx_schema)
schema["table"] = list(_weewx_schema["table"])
_existing = {column[0] for column in schema["table"]}
_added = [name for name in _NODUS_COLUMNS if name not in _existing]
schema["table"].extend((name, "REAL") for name in _added)
schema["day_summaries"] = list(_weewx_schema["day_summaries"])
schema["day_summaries"].extend((name, "scalar") for name in _added)
