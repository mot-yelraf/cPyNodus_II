"""Analyze transferred Nodus recovery and reboot logs.

The parsing and analysis helpers build per-device summaries from the bounded
log formats, while ``format_text_report`` and ``main`` provide human-readable
and JSON command-line output without modifying source logs.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

DEFAULT_LOG_ROOT = "build/nodus-logs"
EVENT_HEADER_RE = re.compile(
    r"^=== recovery event: (?P<event>[^|]+) \| ts=(?P<timestamp>[^|]+) "
    r"\| version=(?P<version>[^|]+) \| device=(?P<device>[^=]+) ===$"
)
REBOOT_HEADER_RE = re.compile(
    r"^=== recovery soft reboot: (?P<reason>[^|]+) \| ts=(?P<timestamp>[^|]+) "
    r"\| version=(?P<version>[^|]+) \| device=(?P<device>[^=]+) ===$"
)


def analyze_log_root(root):
    """Analyze every transferred Nodus log directory under root."""
    root_path = Path(root)
    reports = []
    for device_dir in sorted(path for path in root_path.glob("*") if path.is_dir()):
        reports.append(analyze_device_dir(device_dir))
    return reports


def analyze_device_dir(device_dir):
    """Analyze one transferred Nodus device log directory."""
    device_path = Path(device_dir)
    recovery_path = device_path / "_recovery.log"
    reboot_path = device_path / "_reboot.log"
    events = parse_recovery_log(recovery_path)
    reboot = parse_reboot_log(reboot_path)
    device_id = _first_nonempty(
        [event.get("device", "") for event in events],
        reboot.get("device", ""),
        device_path.name,
    )
    versions = sorted(
        {
            value
            for value in [event.get("version", "") for event in events]
            + [reboot.get("version", "")]
            if value
        }
    )
    event_counts = Counter(event["event"] for event in events)
    reason_counts = Counter()
    topic_counts = Counter()
    broker_counts = Counter()
    phase_pairs = Counter()
    for event in events:
        fields = event.get("fields", {})
        reason = fields.get("reason", "")
        topic = fields.get("topic", "")
        broker = fields.get("broker", "")
        if reason:
            reason_counts[reason] += 1
        if topic:
            topic_counts[topic] += 1
        if broker:
            broker_counts[broker] += 1
        previous = fields.get("previous", "")
        phase = fields.get("phase", "")
        if previous or phase:
            phase_pairs["{}->{}".format(previous or "?", phase or "?")] += 1

    timestamps = [
        event["timestamp"]
        for event in events
        if event.get("timestamp") and event.get("timestamp") != "unknown"
    ]
    return {
        "device_id": device_id,
        "path": str(device_path),
        "versions": versions,
        "event_count": len(events),
        "first_timestamp": timestamps[0] if timestamps else "",
        "last_timestamp": timestamps[-1] if timestamps else "",
        "unknown_timestamp_count": sum(
            1 for event in events if event.get("timestamp") == "unknown"
        ),
        "events": dict(sorted(event_counts.items())),
        "reasons": dict(sorted(reason_counts.items())),
        "topics": dict(sorted(topic_counts.items())),
        "brokers": dict(sorted(broker_counts.items())),
        "phase_transitions": dict(sorted(phase_pairs.items())),
        "reboot": reboot,
    }


def parse_recovery_log(path):
    """Parse one transferred _recovery.log into event dictionaries."""
    log_path = Path(path)
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = []
    current = None
    body = []
    for line in lines:
        match = EVENT_HEADER_RE.match(line.strip())
        if match:
            if current is not None:
                current["fields"] = _parse_fields(body)
                events.append(current)
            current = {
                "event": match.group("event").strip(),
                "timestamp": match.group("timestamp").strip(),
                "version": match.group("version").strip(),
                "device": match.group("device").strip(),
                "fields": {},
            }
            body = []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        current["fields"] = _parse_fields(body)
        events.append(current)
    return events


def parse_reboot_log(path):
    """Parse one transferred _reboot.log summary."""
    log_path = Path(path)
    if not log_path.exists():
        return {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = REBOOT_HEADER_RE.match(line.strip())
        if match:
            return {
                "reason": match.group("reason").strip(),
                "timestamp": match.group("timestamp").strip(),
                "version": match.group("version").strip(),
                "device": match.group("device").strip(),
                "path": str(log_path),
            }
    return {"path": str(log_path)}


def format_text_report(reports):
    """Render device reports as compact human-readable text."""
    lines = []
    for report in reports:
        lines.append(report["device_id"])
        lines.append("  path: {}".format(report["path"]))
        lines.append("  versions: {}".format(", ".join(report["versions"]) or "none"))
        lines.append("  events: {}".format(report["event_count"]))
        if report["first_timestamp"] or report["last_timestamp"]:
            lines.append(
                "  window: {} -> {}".format(
                    report["first_timestamp"] or "unknown",
                    report["last_timestamp"] or "unknown",
                )
            )
        if report["unknown_timestamp_count"]:
            lines.append(
                "  unknown timestamps: {}".format(report["unknown_timestamp_count"])
            )
        reboot = report.get("reboot") or {}
        if reboot.get("reason"):
            lines.append(
                "  reboot: {} at {}".format(
                    reboot.get("reason", ""),
                    reboot.get("timestamp", ""),
                )
            )
        _append_counter(lines, "event types", report["events"])
        _append_counter(lines, "reasons", report["reasons"])
        _append_counter(lines, "topics", report["topics"])
        _append_counter(lines, "brokers", report["brokers"])
        _append_counter(lines, "phase transitions", report["phase_transitions"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    """Run the Nodus recovery-log analyzer."""
    parser = argparse.ArgumentParser(prog="nodus-log-analyze")
    parser.add_argument("--root", default=DEFAULT_LOG_ROOT)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.device_id:
        reports = [analyze_device_dir(Path(args.root) / args.device_id)]
    else:
        reports = analyze_log_root(args.root)
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    else:
        print(format_text_report(reports), end="")
    return 0


def _parse_fields(lines):
    fields = {}
    text = "\n".join(line.strip() for line in lines if line.strip()).strip()
    for token in text.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _append_counter(lines, label, mapping):
    if not mapping:
        return
    lines.append("  {}:".format(label))
    for key, count in sorted(mapping.items(), key=lambda item: (-item[1], item[0])):
        lines.append("    {}: {}".format(key, count))


def _first_nonempty(values, *fallbacks):
    for value in list(values) + list(fallbacks):
        if str(value or "").strip():
            return str(value).strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
