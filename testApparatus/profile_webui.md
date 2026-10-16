# Host-side NodusWeb profiling

Run from the repository on a host with Python 3 and curl. This helper does not
run on CircuitPython, open the serial port, or require device deployment.

```sh
python3 testApparatus/profile_webui.py http://192.0.2.10:8000 \
  --serial-log ~/cu.usbmodemYOUR_DEVICE.log > /tmp/nodus-web-profile.jsonl
```

The default sweep requests `/`, `/setup` twice, `/time-setup`, `/mqtt-setup`,
`/display-setup`, `/calibration`, `/switch-setup`, `/automations-ui`, `/info`,
and `/current-data` sequentially, with a 15-second
request deadline and five seconds to settle after each response. There are no
automatic retries, redirects, assets, JavaScript fetches, or state mutations.
Switch and automation pages may legitimately return 404 on sensor-only devices.
Exit status is nonzero for incomplete transfers or any non-200 status; inspect
the individual records to distinguish expected absent routes from failures.

Use repeated `--path /setup` arguments for an explicit smaller sequence. Any
repeated `/current-data` sequence is a separately paced socket-release stress
check; record the chosen `--delay` (minimum two seconds).

Output records contain UTC request start, status, received bytes, advertised
Content-Length, curl exit code/error, connection/first-byte/total timings, and
matching serial events collected through the settling window. A 200 response
with a truncated body is incomplete. Completeness is transport-level only;
this does not verify HTML, browser layout, or Safari rendering. Serial windows
may also contain unrelated activity, so avoid simultaneous browser requests.

The first record identifies the latest boot found in the existing serial log.
Check its firmware, profile, and device identity before interpreting results.
If no new serial events arrive, the capture may be stale: timing alone does not
establish device-side heap or recovery behavior. Current firmware logs render
failures but does not supply successful per-stage heap/latency measurements;
this tool cannot measure allocations or diagnose heap fragmentation remotely.

Response headers are temporary and bodies are discarded to avoid saving
credentials embedded in Network or Profile & MQTT. Serial excerpts can still
contain identifiers or other operational details; review them before sharing.
