"""Tests for bounded recovery-log append and trim behavior."""

from cpynodus_ii import __version__
from cpynodus_ii.core.recovery_log import append_recovery_event


def test_append_recovery_event_writes_header_and_detail(tmp_path):
    log_path = tmp_path / "_recovery.log"

    written = append_recovery_event(
        "phase_change",
        "previous=idle phase=mqtt wifi_ready=True mqtt_connected=False",
        path=str(log_path),
        device_id="soil-bd1234",
    )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert "=== recovery event: phase_change | ts=" in content
    assert " | version={} | device=soil-bd1234 ===".format(__version__) in content
    assert "previous=idle phase=mqtt wifi_ready=True mqtt_connected=False" in content


def test_append_recovery_event_trims_log_to_max_size(tmp_path):
    log_path = tmp_path / "_recovery.log"
    log_path.write_text(
        "=== old-entry ===\n"
        + ("x" * 300)
        + "\n"
        + "=== recent-entry ===\n"
        + ("y" * 300)
        + "\n",
        encoding="utf-8",
    )

    written = append_recovery_event(
        "mqtt_rebuild",
        "broker=homeassistant.local socket_pool=ready",
        path=str(log_path),
        device_id="co2-ph244",
        max_bytes=360,
    )

    assert written is True
    content = log_path.read_text(encoding="utf-8")
    assert len(content.encode("utf-8")) <= 360
    assert "=== recovery event: mqtt_rebuild | ts=" in content
    assert "broker=homeassistant.local socket_pool=ready" in content
    assert "=== old-entry ===" not in content
