"""Tests for profile-driven web route selection.

The cases pin which setup, status, calibration, switch, and automation paths
are exposed for each runtime mode.
"""

from cpynodus_ii.core.config import RuntimeConfig, SwitchConfig
from cpynodus_ii.features.web_routes import build_web_route_table, route_paths


def test_nodusweb_route_table_includes_operational_routes_only():
    runtime_config = RuntimeConfig(active_profile="nodusweb")

    paths = route_paths(runtime_config)

    assert "/" in paths
    assert "/current-data" in paths
    assert "/setup" in paths
    assert "/calibration" in paths
    assert "/info" in paths
    assert "/config" in paths
    assert "/set-switch-state" in paths
    assert "/restart" in paths
    assert "/itaot-init" not in paths
    assert "/itaot-meta" not in paths


def test_ap_mode_route_table_includes_bootstrap_and_operational_routes():
    runtime_config = RuntimeConfig(active_profile="nodusweb", ap_mode=True)

    paths = route_paths(runtime_config)

    assert "/setup" in paths
    assert "/itaot-init" in paths
    assert "/itaot-meta" in paths


def test_automation_routes_require_nodusweb_and_present_switch():
    runtime_config = RuntimeConfig(
        active_profile="nodusweb",
        switch=SwitchConfig(present=True, device_id="switch-test"),
    )

    paths = route_paths(runtime_config)

    assert "/automations" in paths
    assert "/automations/delete" in paths
    assert "/switch-setup" in paths
    assert "/automations-ui" in paths
    runtime_config.active_profile = "weewx"
    assert build_web_route_table(runtime_config) == ()

    runtime_config.ap_mode = True
    ap_paths = route_paths(runtime_config)
    assert "/itaot-init" in ap_paths
    assert "/automations" not in ap_paths
    assert "/automations/delete" not in ap_paths
    assert "/switch-setup" not in ap_paths
    assert "/automations-ui" not in ap_paths


def test_sensorius_route_table_is_empty_when_web_is_disabled():
    runtime_config = RuntimeConfig(active_profile="sensorius")

    assert build_web_route_table(runtime_config) == ()


def test_homeassistant_route_table_is_empty_when_web_disabled():
    runtime_config = RuntimeConfig(active_profile="homeassistant")

    assert build_web_route_table(runtime_config) == ()
