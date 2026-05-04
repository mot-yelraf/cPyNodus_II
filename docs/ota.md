# Over-the-Air Updates

This document is the implementation plan for adding over-the-air updates to
Nodus devices. The target flow is Sensorius-driven, one physical Nodus device
at a time, with a host-side command line tool available first so OTA can be
developed and tested without depending on Sensorius.

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
- Preserve a rollback path when download, validation, apply, or post-update
  boot verification fails.

## Non-Goals

- Updating the CircuitPython UF2 runtime over OTA.
- Updating the Pico bootloader.
- Updating multiple devices concurrently.
- Using MQTT for file transfer.
- Replacing AP onboarding or normal TOML-based configuration.

## Constraints

- Nodus runs CircuitPython `9.2.8` on Pico2 W.
- Heap and filesystem space are tight, so manifests and handlers must be small.
- OTA requires application-writable filesystem mode. Current `boot.py` enables
  app writes only when `GP14` is held low at boot and otherwise remounts the
  filesystem read-only.
- Required heap for firmware update mode is not yet known. The first
  implementation must measure free heap at OTA mode startup, after manifest
  parse, during each file upload, and before apply.
- MQTT-enabled profiles intentionally skip the normal-mode web server today.
  OTA therefore needs a temporary runtime path that brings up Wi-Fi and a small
  HTTP OTA server without MQTT, sensor loops, switch loops, or the full UI.
- Configuration files remain device-local state and should not be replaced by
  default templates unless explicitly requested by the package manifest.

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

- `nodus-ota` host CLI: creates packages, serves or pushes packages, and drives
  test updates directly against a Nodus device.
- Nodus OTA runtime: a small HTTP-only mode that accepts one update session,
  stages files, validates the package, applies the filesystem changes, records
  rollback metadata, and reboots.

Sensorius should later reuse the same package format and HTTP session protocol
used by the CLI.

## Package Tool

Add a command line tool under `tools/` or `scripts/`, with tests under `tests/`.
The first useful shape should be:

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
timestamp. When running Nodus from the REPL, the device logs the prepare command,
OTA-mode startup, HTTP begin/file/commit handling, and reboot scheduling.

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

The tool should avoid requiring Sensorius during early development. `serve`
can expose package files over HTTP, and `push` can call Nodus OTA endpoints
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
- `delete` is allowed but should be used sparingly and must be tested.
- The manifest should contain both package identity and required current
  firmware version so Nodus can reject mismatched updates.
- File paths must be relative, normalized, and must not contain `..`.
- Nodus verifies each transferred file with SHA-256 before accepting the file
  as staged. If the streamed SHA-256 does not match the manifest, Nodus rejects
  the file and the package cannot commit.

## Nodus OTA Mode

Add a temporary OTA mode that starts after a small persisted intent file or NVM
marker is set. This mode should:

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

Suggested OTA state:

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

Suggested endpoints in OTA mode:

- `GET /ota/status`
- `POST /ota/begin`
- `PUT /ota/file?path=<relative-path>`
- `POST /ota/commit`
- `POST /ota/abort`

Suggested flow:

1. Client waits for `/ota/status` to report `ready`.
2. Client sends `/ota/begin` with a compact manifest.
3. Nodus validates package target, current version, available space, and paths.
4. Client uploads files one at a time with `PUT /ota/file`.
5. Nodus streams each file to a staging path and records size and SHA-256
   status.
6. Client calls `/ota/commit`.
7. Nodus verifies all staged files, backs up replaced files, applies changes,
   records success, clears OTA mode, waits less than 10 seconds, and reboots.

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

Use a two-phase apply:

1. Stage all incoming files under `/_ota/stage/`.
2. Validate the complete package before touching live firmware paths.
3. Move current live files that will be replaced into `/_ota/backup/`.
4. Move staged files into their live paths.
5. Write `/_ota/state.json` phase `applied_pending_boot`.
6. Wait for a short confirmation window, less than 10 seconds.
7. Reboot.
8. On successful normal startup, mark OTA complete and remove old backup files.

Rollback triggers:

- manifest rejected;
- upload interrupted;
- SHA-256 or size mismatch;
- apply step fails;
- first boot after apply fails before the runtime reaches a defined healthy
  checkpoint.

Rollback behavior:

- restore files from `/_ota/backup/`;
- clear partial staged files;
- restore `prior_profile`;
- reboot;
- expose rollback reason in `/ota/status`, serial logs, reboot logs, and later
  Sensorius metadata.

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
6. Upload manifest and files sequentially.
7. Commit.
8. Wait for the device to reboot into its prior profile.
9. Confirm new retained `meta.version`, heartbeat, and expected profile.
10. Mark the update complete in Sensorius.

If HTTP cannot be reached after OTA mode starts, Sensorius should time out and
let Nodus return to the prior profile automatically.

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
   - Implement tag-range package creation.
   - Validate include/exclude behavior.
   - Generate `manifest.json`.
2. Host-only OTA CLI.
   - Add package `serve` and `push`.
   - Use the same HTTP protocol Sensorius will use later.
3. Nodus OTA state model.
   - Add private OTA state load/save helpers.
   - Add startup detection for OTA mode.
   - Preserve prior profile.
   - Add handling for `nodus/<device_id>/fwupdate` prepare commands.
4. Minimal Nodus OTA HTTP server.
   - Start Wi-Fi.
   - Skip MQTT and feature runtimes.
   - Implement `/ota/status`, `/ota/begin`, `/ota/file`, `/ota/commit`,
     and `/ota/abort`.
5. Staging and validation.
   - Stage files under `/_ota/stage/`.
   - Enforce path, size, version, and SHA-256 checks.
6. Apply and rollback.
   - Add backup/apply/restore routines.
   - Add boot health checkpoint.
   - Clear OTA state after success.
7. Sensorius integration.
   - Add UI/API to pick one target device and one package.
   - Use HTTP transfer after an OTA prepare command.
   - Record progress and final result.
8. Hardware soak tests.
   - Update sensor-only, switch-only, and sensor+switch devices.
   - Test interrupted upload, bad SHA-256, bad version, full filesystem,
     failed first boot, and normal recovery to the prior profile.

## Test Plan

Host tests:

- package creation from two temporary Git tags;
- manifest path normalization and exclusion rules;
- rejected missing tags;
- rejected path traversal;
- delete list validation;
- simulated upload session success;
- simulated SHA-256 mismatch;
- simulated rollback state transitions.

Hardware tests:

- update from one firmware tag to the next on a writable Nodus filesystem;
- confirm OTA is rejected or unavailable when `GP14` has not put the app
  filesystem in RWFS mode;
- update while the prior profile is `sensorius`;
- update while the prior profile is `homeassistant`;
- update a switch-only device and verify `switch.toml` remains the runtime
  switch gate;
- interrupt power during upload and confirm the device returns to the prior
  profile;
- interrupt power after apply and confirm rollback or successful completion;
- confirm MQTT is offline during OTA and resumes after reboot;
- confirm retained `meta.version` matches the updated firmware version;
- capture heap checkpoints during OTA mode startup, manifest parse, each file
  upload, and apply.

## Open Decisions

- How much free filesystem space to require before accepting an update.
- How long OTA mode should wait before automatically rebooting back into the
  prior profile when no HTTP client connects.
- How much heap OTA mode requires in practice, based on Pico2 W measurements
  during package transfer and apply.
