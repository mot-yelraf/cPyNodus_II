"""Checks for the units and archive schema installed on the WeeWX host.

The cases inspect extension modules to ensure Nodus observations, groups, and
archive fields remain aligned with the host integration contract.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

USER_ROOT = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nodus_schema_extends_weewx_schema_without_duplicates(monkeypatch):
    weewx = ModuleType("weewx")
    schemas = ModuleType("weewx.schemas")
    extended = ModuleType("weewx.schemas.wview_extended")
    extended.schema = {
        "table": [("dateTime", "INTEGER"), ("co2", "REAL")],
        "day_summaries": [("co2", "scalar")],
    }
    monkeypatch.setitem(sys.modules, "weewx", weewx)
    monkeypatch.setitem(sys.modules, "weewx.schemas", schemas)
    monkeypatch.setitem(sys.modules, "weewx.schemas.wview_extended", extended)

    module = _load("nodus_schema_test", USER_ROOT / "nodus_schema.py")
    columns = [name for name, _type in module.schema["table"]]
    summaries = [name for name, _type in module.schema["day_summaries"]]

    assert columns.count("co2") == 1
    assert summaries.count("co2") == 1
    assert "vpd" in columns
    assert "soilMoisturePct" in columns


def test_nodus_units_registers_observation_groups(monkeypatch):
    weewx = ModuleType("weewx")
    units = ModuleType("weewx.units")
    units.obs_group_dict = {}
    units.USUnits = {}
    units.MetricUnits = {}
    units.MetricWXUnits = {}
    units.conversionDict = {}
    units.default_unit_format_dict = {}
    units.default_unit_label_dict = {}
    engine = ModuleType("weewx.engine")

    class StdService:
        def __init__(self, _engine, _config_dict):
            pass

    engine.StdService = StdService
    weewx.units = units
    monkeypatch.setitem(sys.modules, "weewx", weewx)
    monkeypatch.setitem(sys.modules, "weewx.units", units)
    monkeypatch.setitem(sys.modules, "weewx.engine", engine)

    module = _load("nodus_units_test", USER_ROOT / "nodus_units.py")
    module.install_units()

    assert units.obs_group_dict["vpd"] == "group_nodus_vpd"
    assert units.obs_group_dict["absoluteHumidity"] == (
        "group_nodus_absolute_humidity"
    )
    assert units.obs_group_dict["soilTemperature"] == "group_temperature"
    assert units.MetricWXUnits["group_nodus_absolute_humidity"] == (
        "gram_per_meter_cubed"
    )
    assert units.MetricWXUnits["group_nodus_vpd"] == "kPa"
    assert units.default_unit_format_dict["kPa"] == "%.3f"
    assert units.default_unit_format_dict["gram_per_meter_cubed"] == "%.2f"
