# Over-the-Air Updates

This document describes the current Nodus over-the-air update implementation,
the verified hardware baseline, and the remaining hardening plan. The target
production flow is Sensorius-driven, one physical Nodus device at a time. The
host-side command line tool is available now and uses the same MQTT prepare
plus HTTP transfer flow that Sensorius should reuse.

## Goals

- Build update packages from Git tag ranges, for example `tagA..tagB`.
- Transfer packages to Nodus over HTTP, not MQTT.
- Stop MQTT and other nonessential runtime tasks before applying an update.
- Use MQTT only for the small prepare/control command on
  `nodus/<device_id>/fwupdate`.
- Use a temporary OTA mode when the normal profile does not leave enough memory
  to update or revert reliably.
- Reboot into the same runtime profile, Wi-Fi network, MQTT broker, and device
  identity after a successful update.
- Preserve a rollback path when download, validation, or apply fails. A
  fuller post-update boot-health rollback remains future work.

## Non-Goals

- Updating the CircuitPython UF2 runtime over OTA.
- Updating board bootloaders or CircuitPython itself.
- Updating multiple devices concurrently.
- Using MQTT for file transfer.
- Replacing AP onboarding or normal TOML-based configuration.

## Constraints

- OTA package targets are board-specific. Verified targets are `pico2w` on
  CircuitPython `9.2.8` and `xesp32s3` on CircuitPython `10.2.1`.
- Heap and filesystem space are tight, so manifests and handlers must be small.
  Pico2 W testing has shown an out-of-memory condition when attempting to OTA a
  very large `cpynodus_ii/app.mpy`; keep package slices small and characterize
  large runtime-module updates before field use.
- OTA requires application-writable filesystem mode. Current `boot.py` enables
  app writes when the board-specific RW guard is held low at boot (`GP14` on
  Pico2 W, `D8` on XIAO ESP32-S3) and otherwise remounts the filesystem
  read-only.
- OTA file upload uses 1024-byte HTTP chunks by default. The legacy whole-file
  `/ota/file` endpoint remains for small/debug transfers, but chunked transfer
  is the normal path because larger files can fail full-body allocation on
  CircuitPython.
- The CLI default per-request timeout is 300 seconds. Large commits can take
  over a minute because Nodus verifies staged files and backs up/replaces live
  files on CircuitPython storage.
- MQTT-enabled profiles intentionally skip the normal-mode web server today.
  OTA therefore needs a temporary runtime path that brings up Wi-Fi and a small
  HTTP OTA server without MQTT, sensor loops, switch loops, or the full UI.
- Configuration files remain device-local state and should not be replaced by
  default templates unless explicitly requested by the package manifest.

## Hardware Verification Status

Baseline OTA verification is complete as of 2026-06-23 on both supported
targets:

- `pico2w`: Raspberry Pi Pico2 W on CircuitPython `9.2.8`
- `xesp32s3`: Seeed Studio XIAO ESP32-S3 Sense on CircuitPython `10.2.1`

The verified baseline covers MQTT prepare, reboot into temporary OTA mode,
chunked HTTP transfer, commit/apply, reboot back into the prior runtime, and
post-update completion reporting for bounded package sizes. Pico2 W testing
also exposed an out-of-memory condition when updating with a very large
`cpynodus_ii/app.mpy`, so large-package behavior is not part of the baseline.
Remaining hardware work is fault injection, profile/device matrix soak testing,
filesystem-space limits, large-package limits, and post-update boot-health
rollback.

## High-Level Architecture

```
Git tags -> OTA package tool -> package directory/archive
                                  |
                                  v
Sensorius or OTA CLI -> HTTP -> Nodus temporary OTA mode
                                  |
                                  v
                         staged files + manifest
                                  |
                                  v
                         validate, apply, reboot
                                  |
                                  v
                         prior profile resumes
```

The OTA transport has two sides:

- `nodus-ota` host CLI: creates packages, publishes the MQTT prepare command,
  pushes packages over HTTP, and logs transfer/commit timing directly against a
  Nodus device.
- Nodus OTA runtime: a small HTTP-only mode that accepts one update session,
  stages files, validates the package, applies the filesystem changes, records
  rollback metadata, and reboots.

Sensorius should later reuse the same package format and HTTP session protocol
used by the CLI.

## Package Tool

The command line tool lives under `scripts/` with tests under `tests/`.
OTA packages are built from committed Git history, not from whichever files
are currently copied to a device. The baseline tag for the 2026-06-23
hardware-verified OTA state is `OTA-Verified---baseline`, the Git-safe spelling
of "OTA-Verified - baseline". After deployed devices are confirmed to be
running that baseline, create each future release tag on the commit to deploy
and build the package from the baseline or previous deployed release tag to the
new tag.

Typical tag workflow:

```text
git tag --list "OTA-Verified---baseline"
git tag -a v0.26.174.1 -m "Nodus firmware v0.26.174.1"
git push origin OTA-Verified---baseline v0.26.174.1
```

Build the OTA package from the exact tag range the device is expected to move
through. For example, to update a device already on the verified baseline to
`v0.26.174.1`:

```text
python scripts/nodus_ota.py package \
  --from OTA-Verified---baseline \
  --to v0.26.174.1 \
  --out build/ota/OTA-Verified---baseline_to_v0.26.174.1
```

The resulting `manifest.json` records `from_tag`, `to_tag`, package id, file
hashes, and the firmware version read from `cpynodus_ii/__init__.py` at the
`--from` tag. Sensorius should use that manifest data to match the selected
package to the retained device version before initiating OTA.

The generic tag-range package shape is:

```text
nodus-ota package --from tagA --to tagB --out build/ota/tagA_tagB
nodus-ota push build/ota/tagA_tagB --device http://co2-ykdvea.local:8000
```

Current repository entrypoint:

```text
python scripts/nodus_ota.py package --from tagA --to tagB --out build/ota/tagA_tagB
python scripts/nodus_ota.py push build/ota/tagA_tagB --device http://co2-ykdvea.local:8000
```

For the first single-file transfer attempts with `ota_test.py`, before formal
baseline tags exist:

```text
python scripts/nodus_ota.py package-worktree \
  --out build/ota/ota_test_only \
  --package-id ota-test-1 \
  --include ota_test.py

python scripts/nodus_ota.py push build/ota/ota_test_only \
  --prepare \
  --broker <mqtt-broker-host-or-ip> \
  --device-id co2-ykdvea \
  --device http://10.0.0.213:8000
```

The `push --prepare` flow publishes `prepare` to
`nodus/<device-id>/fwupdate`, waits briefly for Nodus to reboot into temporary
OTA mode, then transfers the package over HTTP. The CLI logs each step with a
timestamp, including per-chunk offsets, per-file elapsed time, effective bytes
per second, commit time, and a package summary. When running Nodus from the
REPL, the device logs the prepare command, OTA-mode startup, HTTP
begin/file/commit handling, verification/apply timing, and reboot scheduling.

Recommended push command for current hardware testing:

```text
python scripts/nodus_ota.py push build/ota/OTA-Verified---baseline_to_v0.26.174.1 \
  --prepare \
  --broker <mqtt-broker-host-or-ip> \
  --username <mqtt-user> \
  --password "$HA_MQTT_PASSWORD" \
  --device-id <device-id> \
  --device http://<device-ip>:8000 \
  --wait-after-prepare 15 \
  --timeout 300
```

`--chunk-size` defaults to `1024`. Set `--chunk-size 0` only to force the
legacy whole-file endpoint for small diagnostic transfers.

The package command should:

- require both tags to exist;
- derive changed files with Git, using `tagA..tagB`;
- include only deployable firmware paths by default;
- exclude development-only paths such as `.git/`, `tests/`, `docs/`,
  `__pycache__/`, and host build output;
- support explicit include/exclude overrides for recovery testing;
- compute size and SHA-256 for each file;
- emit a compact JSON manifest.

Recommended initial package layout:

```text
manifest.json
files/
  code.py
  boot.py
  cpynodus_ii/app.py
  cpynodus_ii/core/settings.py
```

The tool avoids requiring Sensorius during early development. `push` publishes
the MQTT prepare command when requested and calls Nodus OTA HTTP endpoints
against a single device.

## Manifest

Use a compact schema that is easy for CircuitPython to validate incrementally.
The device should not need to keep the whole manifest plus all file data in
memory at once.

Example:

```json
{
  "schema": "nodus-ota/v1",
  "package_id": "ota-v0.26.123.3-to-v0.26.124.1",
  "from_tag": "v0.26.123.3",
  "to_tag": "v0.26.124.1",
  "created_at": "2026-05-03T00:00:00Z",
  "target": {
    "platform": "pico2w",
    "circuitpython": "9.2.8"
  },
  "requires": {
    "version": "v0.26.123.3"
  },
  "files": [
    {
      "path": "cpynodus_ii/app.py",
      "size": 12345,
      "sha256": "..."
    }
  ],
  "delete": [],
  "preserve": [
    "settings.toml",
    "sensor_i2c.toml",
    "sensor_soil.toml",
    "switch.toml"
  ],
  "post_apply": {
    "reboot": true,
    "resume_profile": "previous"
  }
}
```

Manifest rules:

- `settings.toml`, `sensor_i2c.toml`, `sensor_soil.toml`, and `switch.toml`
  are preserved unless listed explicitly in `files`.
- `target.platform` should match the deploy target, currently `pico2w` or
  `xesp32s3`; `target.circuitpython` should match that target's verified
  CircuitPython runtime.
- `delete` is allowed but should be used sparingly and must be tested.
- The manifest contains both package identity and required current firmware
  version. The current device-side validation binds the package to the MQTT
  prepare state and validates paths, size, and SHA-256. Sensorius should also
  compare `requires.version` with retained `meta.version` before starting an
  update.
- File paths must be relative, normalized, and must not contain `..`.
- Nodus verifies each transferred file with SHA-256 before accepting the file
  as staged. With chunked transfer, Nodus writes chunks directly to
  `/_ota/stage/...`, then streams the staged file from disk during
  `/ota/file/end` to verify size and SHA-256. If the SHA-256 does not match the
  manifest, Nodus rejects the file and the package cannot commit.

## Nodus OTA Mode

Nodus has a temporary OTA mode that starts after a small persisted intent file
is set. This mode:

1. Load enough settings to join the existing Wi-Fi network.
2. Remember the prior active profile.
3. Skip sensor detection, switch setup, MQTT startup, Home Assistant discovery,
   periodic publish loops, and the full web UI.
4. Start a minimal HTTP OTA server.
5. Apply or reject exactly one update session.
6. Reboot back into the prior active profile after success or rollback.

The OTA intent should live outside the normal public config schema, for
example `/_ota/state.json`, so ordinary configuration remains stable. Keep this
file small and rewrite it atomically where possible.

Current OTA state shape:

```json
{
  "mode": "ota",
  "prior_profile": "sensorius",
  "package_id": "ota-v0.26.123.3-to-v0.26.124.1",
  "phase": "requested"
}
```

Boot behavior:

- Normal runtime receives an OTA prepare request from Sensorius or the CLI on
  `nodus/<device_id>/fwupdate`.
- Nodus persists OTA state with `prior_profile`.
- Nodus shuts down MQTT if it is running.
- Nodus soft reboots.
- Startup sees OTA state and enters OTA mode instead of the prior profile.

This avoids trying to free enough memory inside the fully active normal
runtime.

## HTTP Protocol

Keep the device-side protocol simple and sequential.

Current endpoints in OTA mode:

- `GET /ota/status`
- `POST /ota/begin`
- `PUT /ota/file?path=<relative-path>` legacy whole-file upload
- `POST /ota/file/begin?path=<relative-path>`
- `PUT /ota/file/chunk?path=<relative-path>&offset=<byte-offset>`
- `POST /ota/file/end?path=<relative-path>`
- `POST /ota/commit`
- `POST /ota/abort`

Current chunked flow:

1. Client waits for `/ota/status` to report `ready`.
2. Client sends `/ota/begin` with a compact manifest.
3. Nodus validates package identity and safe manifest paths.
4. For each file, client calls `/ota/file/begin`.
5. Client sends 1024-byte chunks to `/ota/file/chunk` with the current offset.
6. Nodus appends each chunk to `/_ota/stage/<relative-path>` and rejects offset
   mismatches.
7. Client calls `/ota/file/end`.
8. Nodus streams the staged file from disk, verifies size and SHA-256, and
   accepts or rejects the file.
9. Client calls `/ota/commit`.
10. Nodus verifies all staged files again, backs up replaced files, applies
    changes, records `applied_pending_boot`, waits less than 10 seconds, and
    reboots.

The legacy whole-file upload endpoint follows the same validation result, but
it requires one contiguous request-body allocation and should not be used for
larger runtime modules.

All responses should be small JSON envelopes:

```json
{
  "accepted": true,
  "phase": "staging",
  "message": "ok"
}
```

Error responses should include a short machine-readable reason:

```json
{
  "accepted": false,
  "phase": "staging",
  "error": "sha256_mismatch"
}
```

## Apply and Rollback Strategy

Nodus currently uses a single-slot workspace plus two-phase apply:

1. On `/ota/begin`, clear stale `/_ota/stage/`, stale `/_ota/backup/`, and
   stale `/_ota/*.tmp` files from prior attempts.
2. Stage all incoming files under `/_ota/stage/`.
3. Validate the complete package before touching live firmware paths.
4. Copy current live files that will be replaced into `/_ota/backup/`.
5. Copy staged files into their live paths.
6. Write `/_ota/state.json` phase `applied_pending_boot`.
7. Remove `/_ota/stage/` and stale `/_ota/*.tmp`.
8. Wait for a short confirmation window, less than 10 seconds.
9. Reboot.
10. On normal startup, mark OTA state `applied`.
11. Publish `nodus/<device_id>/fwupdate/result` with `phase="applied"`.

Only the most recent backup is retained. The next accepted `/ota/begin` clears
the previous backup before staging the new package.

Current rollback triggers:

- manifest rejected;
- upload interrupted;
- SHA-256 or size mismatch;
- apply step fails.

Rollback behavior:

- restore files from `/_ota/backup/`;
- clear partial staged files;
- restore `prior_profile`;
- reboot;
- expose rollback reason in `/ota/status`, serial logs, reboot logs, and later
  Sensorius metadata.

Current implementation restores backups if the apply step itself fails. A
post-reboot health-check rollback, where the first normal boot must reach a
defined healthy checkpoint before the update is considered fully safe, is still
planned.

The first healthy checkpoint can be conservative: Wi-Fi joined and the prior
profile startup plan reached the point where it would normally start its
profile services. For MQTT profiles, a later checkpoint can require MQTT
connect plus retained `meta` publish.

## Sensorius Flow

Sensorius should orchestrate only one physical Nodus host at a time:

1. Select device and package.
2. Confirm package `requires.version` matches the retained Nodus `meta.version`.
3. Publish a prepare command to `nodus/<device_id>/fwupdate` to tell Nodus to
   enter OTA mode. The update bytes still move over HTTP.
4. Wait for the device to reboot into OTA mode.
5. Connect over HTTP to the device address from metadata, mDNS, or current
   network discovery.
6. Upload manifest and files sequentially using the chunked HTTP endpoints.
7. Commit.
8. Wait for the device to reboot into its prior profile.
9. Confirm new retained `meta.version`, heartbeat, and expected profile.
10. Mark the update complete in Sensorius.

If HTTP cannot be reached after OTA mode starts, Sensorius should time out and
surface the failure. Current OTA mode waits indefinitely for an HTTP client
once entered; automatic return to the prior profile is still an open decision.

## Security Model

Initial development can use network trust plus package hash validation. Before
field use, add package authenticity:

- sign the manifest on the host;
- store the public verification key on Nodus;
- reject unsigned or invalid manifests;
- include nonce/session fields if replay becomes a practical risk.

Do not include Wi-Fi or MQTT credentials in OTA packages.

## Implementation Phases

1. Package builder and tests.
   - Implemented tag-range and worktree package creation.
   - Implemented include/exclude behavior.
   - Generates `manifest.json`.
2. Host-only OTA CLI.
   - Implemented package creation, prepare publish, and HTTP `push`.
   - Uses the same HTTP protocol Sensorius will use later.
3. Nodus OTA state model.
   - Implemented private OTA state load/save helpers.
   - Implemented startup detection for OTA mode.
   - Preserves prior profile.
   - Handles `nodus/<device_id>/fwupdate` prepare commands.
4. Minimal Nodus OTA HTTP server.
   - Start Wi-Fi.
   - Skip MQTT and feature runtimes.
   - Implemented `/ota/status`, `/ota/begin`, `/ota/file`, `/ota/file/begin`,
     `/ota/file/chunk`, `/ota/file/end`, `/ota/commit`, and `/ota/abort`.
5. Staging and validation.
   - Implemented staging under `/_ota/stage/`.
   - Enforces path, size, package identity, and SHA-256 checks.
6. Apply and rollback.
   - Implemented backup/apply/restore routines for apply failures.
   - Implemented post-boot `applied` marking and result publish.
   - Boot health checkpoint rollback remains future work.
7. Sensorius integration.
   - Add UI/API to pick one target device and one package.
   - Use HTTP transfer after an OTA prepare command.
   - Record progress and final result.
8. Hardware soak tests.
   - Baseline OTA prepare, transfer, commit, reboot, and post-update reporting
     are verified on `pico2w` and `xesp32s3`.
   - Remaining soak work: update sensor-only, switch-only, and sensor+switch
     devices across supported profiles.
   - Remaining fault work: interrupted upload, bad SHA-256, bad version, full
     filesystem, failed first boot, and normal recovery to the prior profile.

## Test Plan

Host tests:

- package creation from two temporary Git tags;
- worktree package creation for pre-tag validation;
- manifest path normalization and exclusion rules;
- rejected missing tags;
- rejected path traversal;
- delete list validation;
- simulated upload session success;
- chunked file staging and offset mismatch;
- simulated SHA-256 mismatch;
- simulated rollback state transitions.

Hardware tests:

- baseline verified on 2026-06-23: update from one firmware version to the next
  on writable `pico2w` and `xesp32s3` Nodus filesystems;
- baseline verified on 2026-06-23: MQTT prepare enters temporary OTA mode,
  package bytes move over chunked HTTP, commit applies the update, and the
  device reboots back to normal runtime with a completion report;
- remaining: confirm OTA is rejected or unavailable when the app filesystem is
  not in RWFS mode;
- remaining: update while the prior profile is `sensorius`;
- remaining: update while the prior profile is `homeassistant`;
- remaining: update a switch-only device and verify `switch.toml` remains the
  runtime switch gate;
- remaining: interrupt power during upload and confirm the device returns to
  the prior profile;
- remaining: interrupt power after apply and confirm rollback or successful
  completion;
- remaining: confirm MQTT is offline during OTA and resumes after reboot;
- remaining: confirm retained `meta.version` matches the updated firmware
  version;
- remaining: capture heap checkpoints during OTA mode startup, manifest parse,
  each file upload, and apply;
- remaining: capture CLI timing summary and serial timing for file verify and
  commit verify/apply phases.

## Historical Characterization

On `co2-ph244` running `v0.26.124.10`, an earlier 4-file, 36,439-byte package
using 1024-byte chunks completed successfully:

```text
Transfer start: 13:32:43
Last file accepted: 13:35:31
Commit requested: 13:35:31
Commit accepted: 13:37:13
Reboot executed: 13:37:18
Normal boot: 13:37:32
```

Observed serial-side file windows:

```text
cpynodus_ii/core/ntp.py                  9,329 B   40s
cpynodus_ii/features/payloads.py        18,778 B   80s
cpynodus_ii/hardware/switch_adapter.py   4,207 B   21s
cpynodus_ii/ota/runtime.py               4,125 B   18s
```

Commit took about 102 seconds for this package. This is acceptable for early
field testing, but larger packages should keep using the 300-second CLI timeout
and should be characterized before broad deployment.

Later Pico2 W testing also showed an out-of-memory condition while attempting
to update with a very large `cpynodus_ii/app.mpy`. Treat that as a current
package-sizing limit: prefer smaller OTA slices, avoid bundling large compiled
runtime modules into one update unless specifically testing that path, and keep
large-package validation separate from the baseline success criteria above.

## Open Decisions

- How much free filesystem space to require before accepting an update.
- How long OTA mode should wait before automatically rebooting back into the
  prior profile when no HTTP client connects.
- Whether commit can safely skip the second SHA-256 pass when `/ota/file/end`
  has already verified each staged file.
- What boot-health checkpoint should trigger post-update rollback.
