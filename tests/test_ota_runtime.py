"""Tests for temporary OTA runtime startup behavior."""

import asyncio
from types import SimpleNamespace

from cpynodus_ii.core.config import RuntimeConfig
from cpynodus_ii.ota import FwUpdateState, load_ota_state, run_ota_mode
from tests.test_ota_http import _FakeServerModule


def test_run_ota_mode_marks_state_ready_and_skips_feature_startup(tmp_path):
    logs = []

    def _network_builder(runtime_config):
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
    assert [prefix for prefix, _message in logs].count("ota") == 4
    assert any("phase=ota_startup" in message for _prefix, message in logs)
    assert any("phase=ota_ready" in message for _prefix, message in logs)
    assert any("phase=http http_phase=ready" in message for _prefix, message in logs)
