"""Tests for transferred Nodus recovery-log analysis."""

from scripts.nodus_log_analyze import (
    analyze_device_dir,
    analyze_log_root,
    format_text_report,
    parse_reboot_log,
    parse_recovery_log,
)


def _header(event, timestamp="2026-05-09 12:32:17"):
    return (
        "=== recovery event: {} | ts={} | version=v0.26.129.3 "
        "| device=co2-ph244 ==="
    ).format(event, timestamp)


def _reboot_header(reason, timestamp="2026-05-09 12:35:51"):
    return (
        "=== recovery soft reboot: {} | ts={} | version=v0.26.129.3 "
        "| device=co2-ph244 ==="
    ).format(reason, timestamp)


def test_parse_recovery_log_extracts_events_and_fields(tmp_path):
    log_path = tmp_path / "_recovery.log"
    log_path.write_text(
        "\n".join(
            (
                _header("phase_change", "unknown"),
                "previous=idle phase=mqtt wifi_ready=True mqtt_connected=False",
                "",
                _header("mqtt_rebuild"),
                (
                    "reason=subscribe_failure topic=nodus/co2-ph244/config/set "
                    "queues pub=12 sub=4 rx=0"
                ),
                "",
            )
        ),
        encoding="utf-8",
    )

    events = parse_recovery_log(log_path)

    assert len(events) == 2
    assert events[0]["event"] == "phase_change"
    assert events[0]["timestamp"] == "unknown"
    assert events[0]["fields"]["phase"] == "mqtt"
    assert events[1]["fields"]["reason"] == "subscribe_failure"
    assert events[1]["fields"]["topic"] == "nodus/co2-ph244/config/set"


def test_analyze_device_dir_summarizes_failure_modes(tmp_path):
    device_dir = tmp_path / "co2-ph244"
    device_dir.mkdir()
    (device_dir / "_recovery.log").write_text(
        "\n".join(
            (
                _header("mqtt_rebuild"),
                (
                    "reason=subscribe_failure topic=nodus/co2-ph244/config/set "
                    "queues pub=12 sub=4 rx=0"
                ),
                "",
                _header("mqtt_rebuild", "2026-05-09 12:40:18"),
                (
                    "reason=subscribe_failure topic=nodus/co2-ph244/config/set "
                    "queues pub=12 sub=4 rx=0"
                ),
                "",
                _header("phase_change", "2026-05-09 12:40:24"),
                "previous=mqtt phase=idle wifi_ready=True mqtt_connected=True",
                "",
            )
        ),
        encoding="utf-8",
    )
    (device_dir / "_reboot.log").write_text(
        "\n".join(
            (
                _reboot_header("mqtt_recovery_timeout"),
                "Traceback",
                "",
            )
        ),
        encoding="utf-8",
    )

    report = analyze_device_dir(device_dir)

    assert report["device_id"] == "co2-ph244"
    assert report["event_count"] == 3
    assert report["events"]["mqtt_rebuild"] == 2
    assert report["reasons"]["subscribe_failure"] == 2
    assert report["topics"]["nodus/co2-ph244/config/set"] == 2
    assert report["reboot"]["reason"] == "mqtt_recovery_timeout"
    assert report["phase_transitions"]["mqtt->idle"] == 1


def test_analyze_log_root_and_text_report(tmp_path):
    device_dir = tmp_path / "co2-ph244"
    device_dir.mkdir()
    (device_dir / "_recovery.log").write_text(
        "\n".join(
            (
                _header("phase_change", "2026-05-09 12:40:24"),
                "previous=mqtt phase=idle wifi_ready=True mqtt_connected=True",
                "",
            )
        ),
        encoding="utf-8",
    )

    reports = analyze_log_root(tmp_path)
    text = format_text_report(reports)

    assert len(reports) == 1
    assert "co2-ph244" in text
    assert "phase_change: 1" in text


def test_parse_reboot_log_handles_missing_header(tmp_path):
    log_path = tmp_path / "_reboot.log"
    log_path.write_text("plain text\n", encoding="utf-8")

    assert parse_reboot_log(log_path) == {"path": str(log_path)}
