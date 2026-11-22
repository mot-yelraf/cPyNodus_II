"""Describe the editable derived-soil groups and their runtime representation.

Field order is shared by TOML loading, MQTT discovery, and the calibration UI.
"""

from cpynodus_ii.core.config import SoilNPKConfig, SoilStressConfig, SoilThresholdConfig

SOIL_DERIVED_GROUPS = {
    "SoilDeficit": (
        "soil_thresholds",
        SoilThresholdConfig,
        ("SPD_WET_THRESHOLD_PCT", "SPD_DRY_THRESHOLD_PCT"),
        ("wet_pct", "dry_pct"),
    ),
    "SoilStress": (
        "soil_stress",
        SoilStressConfig,
        (
            "SSI_TEMP_LOW_CRIT_C",
            "SSI_TEMP_LOW_OK_C",
            "SSI_TEMP_HIGH_OK_C",
            "SSI_TEMP_HIGH_CRIT_C",
            "SSI_MOISTURE_WEIGHT_PCT",
            "SSI_TEMP_WEIGHT_PCT",
        ),
        (
            "temp_low_crit_c",
            "temp_low_ok_c",
            "temp_high_ok_c",
            "temp_high_crit_c",
            "moisture_weight_pct",
            "temp_weight_pct",
        ),
    ),
    "NPK": (
        "soil_npk",
        SoilNPKConfig,
        ("N_TARGET", "P_TARGET", "K_TARGET", "SFI_MIN_WEIGHT_PCT"),
        ("n_target", "p_target", "k_target", "min_weight_pct"),
    ),
}


def load_soil_derived(document):
    """Load shared values and the configured probe overrides from sensor TOML."""
    settings = {}
    overrides = {}
    for group, (attribute, model, keys, fields) in SOIL_DERIVED_GROUPS.items():
        table = document.get(group, {})
        defaults = model()
        shared = model(
            **{
                field: float(table.get(key, getattr(defaults, field)))
                for key, field in zip(keys, fields)
            }
        )
        settings[attribute] = shared
        for channel in ("CH1", "CH2"):
            channel_table = table.get(channel)
            if isinstance(channel_table, dict) and channel_table:
                fields_saved = {
                    field: float(channel_table[key])
                    for key, field in zip(keys, fields)
                    if key in channel_table
                }
                if fields_saved:
                    overrides[channel, group] = fields_saved
    return settings, overrides or None


def soil_derived_snapshot(sensor, *, saved=None):
    """Return version 1 discovery with effective settings in wire order."""
    if sensor.family != "soil" or not sensor.modbus:
        return None
    channels = sensor.modbus.channels
    overrides = sensor.soil_derived_overrides or {}
    snapshot = {}
    for channel in channels:
        groups = {}
        for group, (attribute, model, keys, fields) in SOIL_DERIVED_GROUPS.items():
            if group == "NPK" and channel.variant.lower() != "soil_7in1":
                continue
            if saved is None:
                values = overrides.get((channel.name, group), {})
                shared = getattr(sensor, attribute) or model()
                groups[group] = [
                    values.get(field, getattr(shared, field)) for field in fields
                ]
            else:
                shared = saved.get(group, {})
                values = saved.get(group + "." + channel.name, {})
                defaults = model()
                groups[group] = [
                    float(values.get(key, shared.get(key, getattr(defaults, field))))
                    for key, field in zip(keys, fields)
                ]
        snapshot[channel.name] = {
            "metric_prefix": channel.name + " " if len(channels) > 1 else "",
            "groups": groups,
        }
    return {"sensor_id": sensor.sensor_id, "version": 1, "channels": snapshot}
