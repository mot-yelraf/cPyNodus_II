"""Tests for temporary OTA runtime startup behavior."""

import asyncio
from types import SimpleNamespace

from cpynodus_ii.core.config import RuntimeConfig
from cpynodus_ii.ota.runtime import run_ota_mode
from cpynodus_ii.ota.state import FwUpdateState, load_ota_state
from tests.test_ota_http import _FakeServerModule


def test_run_ota_mode_marks_state_ready_and_skips_feature_startup(tmp_path):
    logs = []
    network_builder_kwargs = []

    def _network_builder(runtime_config, **kwargs):
        network_builder_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            phase="ready",
            ssid=runtime_config.network.ssid,
            ip_address="10.0.0.213",
            socket_pool=object(),
            errors=(),
        )

    result = asyncio.run(
        run_ota_mode(
            RuntimeConfig(),
            FwUpdateState(
                prior_profile="homeassistant",
                package_id="ota-tagA-to-tagB",
                phase="requested",
            ),
            settings_root=tmp_path,
            version="v0.26.123.5",
            network_builder=_network_builder,
            server_module=_FakeServerModule,
            log_fn=lambda prefix, message: logs.append((prefix, message)),
            idle_s=0,
        )
    )

    state = load_ota_state(str(tmp_path / "_ota" / "state.json"))

    assert result.phase == "ready"
    assert result.network_phase == "ready"
    assert result.http_phase == "ready"
    assert result.package_id == "ota-tagA-to-tagB"
    assert state.phase == "ready"
    assert state.prior_profile == "homeassistant"
    assert len(network_builder_kwargs) == 1
    assert network_builder_kwargs[0]["mdns_mode"] == "ota"
    assert network_builder_kwargs[0]["preconnect_scan"] is True
    assert isinstance(network_builder_kwargs[0]["log_start_monotonic"], float)
    assert [prefix for prefix, _message in logs].count("ota") == 4
    assert any("phase=ota_startup" in message for _prefix, message in logs)
    assert any("phase=ota_ready" in message for _prefix, message in logs)
    assert any("phase=http http_phase=ready" in message for _prefix, message in logs)


def test_run_ota_mode_passes_supplied_network_log_start(tmp_path):
    network_builder_kwargs = []

    def _network_builder(runtime_config, **kwargs):
        network_builder_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            phase="ready",
            ssid=runtime_config.network.ssid,
            ip_address="10.0.0.213",
            socket_pool=object(),
            errors=(),
        )

    asyncio.run(
        run_ota_mode(
            RuntimeConfig(),
            FwUpdateState(package_id="ota-tagA-to-tagB", phase="requested"),
            settings_root=tmp_path,
            network_builder=_network_builder,
            server_module=_FakeServerModule,
            log_start_monotonic=123.0,
            idle_s=0,
        )
    )

    assert network_builder_kwargs == [
        {
            "mdns_mode": "ota",
            "preconnect_scan": True,
            "log_start_monotonic": 123.0,
        }
    ]
