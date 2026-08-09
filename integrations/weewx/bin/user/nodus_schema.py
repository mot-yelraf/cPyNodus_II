"""Define the WeeWX schema for canonical Nodus observations.

The column inventory extends the archive table with sensor and switch values
used by the Nodus services while preserving WeeWX schema conventions.
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
