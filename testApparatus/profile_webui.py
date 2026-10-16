"""Profile a running NodusWeb device from the host with paced read-only GETs.

Run with host Python, not CircuitPython. Curl measures each transfer while an
existing serial capture supplies device-side evidence. Response bodies and
headers are discarded because setup pages can contain saved credentials.
"""

import argparse
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROUTES = (
    "/", "/setup", "/setup", "/time-setup", "/mqtt-setup", "/display-setup",
    "/calibration", "/switch-setup",
    "/automations-ui", "/info", "/current-data",
)
SERIAL_EVENT = re.compile(r"\b(?:web|memory|health)\b|boot version=|Traceback")


def serial_delta(path, offset):
    """Read new bytes from an existing capture without opening the serial port."""
    if path is None:
        return offset, []
    with path.open("rb") as capture:
        if capture.seek(0, 2) < offset:
            offset = 0
        capture.seek(offset)
        data = capture.read()
        # Leave an unfinished record for the next request window.
        end = data.rfind(b"\n") + 1
    lines = data[:end].decode("utf-8", errors="replace").splitlines()
    return offset + end, [line for line in lines if SERIAL_EVENT.search(line)]


def probe(base_url, route, timeout):
    """Measure one GET without redirects, retries, or retained response content."""
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="nodus-web-profile-") as directory:
        headers = Path(directory) / "headers"
        result = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--noproxy", "*",
                "--max-time", str(timeout), "--connect-timeout", str(timeout),
                "--dump-header", str(headers), "--output", "/dev/null",
                "--write-out",
                "%{http_code} %{size_download} %{time_connect} "
                "%{time_starttransfer} %{time_total}",
                base_url.rstrip("/") + route,
            ],
            capture_output=True, text=True, timeout=timeout + 5,
            check=False,
        )
        fields = result.stdout.split()
        status, size, connect, first, total = fields
        lengths = re.findall(
            r"(?im)^content-length:\s*(\d+)",
            headers.read_text(errors="replace") if headers.exists() else "",
        )
    expected = int(lengths[-1]) if lengths else None
    size = int(float(size))
    return {
        "started_utc": started, "path": route, "status": int(status),
        "bytes": size, "content_length": expected,
        "complete": result.returncode == 0 and (
            expected is None or size == expected
        ),
        "connect_s": float(connect), "first_byte_s": float(first),
        "total_s": float(total), "curl_exit": result.returncode,
        "error": result.stderr.strip(),
    }


def main(argv=None):
    """Run a sequential sweep and emit one JSON record per request to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Example: http://192.0.2.10:8000")
    parser.add_argument("--serial-log", type=Path)
    parser.add_argument("--delay", type=float, default=5,
                        help="Settle seconds after each GET (minimum 2)")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--path", action="append", choices=sorted(set(ROUTES)),
                        help="Only these routes, in order; repeat explicitly")
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    if (url.scheme != "http" or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        parser.error("base_url must be an HTTP origin without credentials")
    if not 2 <= args.delay <= 300 or not 1 <= args.timeout <= 120:
        parser.error("delay must be 2..300 and timeout must be 1..120 seconds")
    offset = 0
    if args.serial_log:
        args.serial_log = args.serial_log.expanduser()
        try:
            offset = args.serial_log.stat().st_size
            # Include just the most recent boot identity, not historical traffic.
            with args.serial_log.open(errors="replace") as capture:
                boot = next((line.strip() for line in reversed(
                    capture.readlines()) if "boot version=" in line), None)
        except OSError as exc:
            parser.error(str(exc))
        print(json.dumps({"serial_log": str(args.serial_log),
                          "latest_boot": boot}), flush=True)
    failed = False
    for route in args.path or ROUTES:
        try:
            record = probe(args.base_url, route, args.timeout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            record = {"path": route, "complete": False, "error": str(exc)}
        time.sleep(args.delay)
        if args.serial_log:
            try:
                offset, events = serial_delta(args.serial_log, offset)
                record["serial_events"] = events
            except OSError as exc:
                record["serial_error"] = str(exc)
                failed = True
        print(json.dumps(record), flush=True)
        failed |= not record["complete"] or record.get("status") != 200
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
