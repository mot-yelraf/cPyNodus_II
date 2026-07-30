# Over-the-Air Updates

This document describes authenticated cPyNodus II over-the-air updates.
Production OTA uses a signed `nodus-ota/v2` package, Sensorius or the host CLI
as the sender, and one physical Nodus at a time.

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
- Preserve a rollback path when download, validation, apply, or the first
  post-update boot fails.
- Authenticate the exact manifest before accepting any package content.

## Non-Goals

- Updating the CircuitPython UF2 runtime over OTA.
- Updating board bootloaders or CircuitPython itself.
- Updating multiple devices concurrently.
- Using MQTT for file transfer.
- Replacing AP onboarding or normal TOML-based configuration.

## Constraints

- OTA package targets are board-specific. Verified targets are `pico2w` on
  CircuitPython `9.2.8` and `xesp32s3` on CircuitPython `10.2.1`.
- OTA payloads must use target-specific compiled `.mpy` files for all
  CircuitPython modules except the required source-file exceptions `boot.py`,
  `code.py`, `dataclass.py`, and `dataclasses.py`. No other `.py` file may
  appear in an OTA package.
- When an OTA package installs a compiled module, its manifest must delete the
  matching `.py` source path. A device must not retain both `.py` and `.mpy`
  forms of the same module after apply.
- Heap and filesystem space are tight, so manifests and handlers must be small.
  Before accepting a manifest, Nodus checks that free space can hold staging,
  same-directory apply temporaries, backups, and a small metadata margin.
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
- OTA SHA-256 verification streams staged files from disk. It uses the
  built-in CircuitPython `hashlib` when available and falls back to a bounded
  in-module streaming SHA-256 implementation; it must not read a staged file
  into memory just to compute the digest.
- The CLI default per-request timeout is 300 seconds. Large commits can take
  over a minute because Nodus verifies staged files and backs up/replaces live
  files on CircuitPython storage.
- MQTT-enabled profiles intentionally skip the normal-mode web server today.
  OTA therefore needs a temporary runtime path that brings up Wi-Fi and a small
  HTTP OTA server without MQTT, sensor loops, switch loops, or the full UI.
- Temporary OTA mode attempts Wi-Fi join more aggressively than normal startup.
  If it still cannot reach station-ready networking or cannot start the OTA
  HTTP server, it clears the handoff state and reboots back to normal runtime
  instead of remaining in HTTP-unavailable OTA mode.
- Configuration files remain device-local state and should not be replaced by
  default templates unless explicitly requested by the package manifest.
- Packages that introduce this boot-health behavior must include the updated
  root `code.py`; it arms the first-boot checkpoint before importing the
  application, so an import/startup failure is recoverable on the next reboot.
- `/ota-public-key.json` is device trust state. Normal deploys and OTA packages
  preserve it unless an operator explicitly provisions a replacement.

## Why Signing Is Required

HTTPS and package signing protect different boundaries. HTTPS authenticates
one network connection and hides its contents in transit. It does not prove
that a package copied to Sensorius, retained on disk, or supplied by another
authorized host was produced by the firmware release owner. A detached
signature authenticates the package itself before Nodus writes staged files.

cPyNodus II keeps temporary OTA transport on HTTP/8000 and requires RSA-2048
PKCS#1 v1.5 with SHA-256 over the exact `manifest.json` bytes. The device
contains only a compact public key in `/ota-public-key.json`; the private key
remains off-device. The verifier is imported only in temporary OTA mode and
runs once at `/ota/begin`. File hashes remain streaming, so signing does not
add a TLS socket, certificate chain, or large long-lived buffer.

HTTP still exposes package bytes and device addressing to the local network.
Use an isolated management network when confidentiality is required. The
signature is the mandatory code-integrity boundary, not a claim that HTTP is
confidential.

Each MQTT prepare creates a one-use session bound to package ID, manifest
SHA-256, and signing key ID. The same session header is required on begin,
file, commit, and abort requests. Failed authentication clears the request
without deleting an earlier rollback backup.

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
large-package limits, and on-device validation of the boot-health rollback.
The signed v2 authentication path still requires on-device timing and heap
validation on both supported boards before broad field deployment.

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
through. First compile the target-specific MPY image with the compiler matching
the target CircuitPython release:

```text
scripts/nodus_mpy.sh --target pico2w
scripts/nodus_mpy.sh --target xesp32s3
```

Then build the OTA package from those compiled artifacts and the requested Git
range. For example, to update a Pico2 W already on the verified baseline to
`v0.26.174.1`:

```text
python scripts/nodus_ota.py keygen \
  --private-key /secure/path/cpynodusii-ota-private.pem \
  --public-key build/keys/ota-public-key.json

python scripts/nodus_ota.py package \
  --from OTA-Verified---baseline \
  --to v0.26.174.1 \
  --target pico2w \
  --compiled-root build/firmware/pico2w \
  --out build/ota/cpynodusii_v0.26.201.2_to_v0.26.174.1_pico2w \
  --signing-key /secure/path/cpynodusii-ota-private.pem
```

Provision the generated public document during an operator-owned deployment:

```text
scripts/deploy_nodus.sh --target <CIRCUITPY-or-staging-target> \
  --content <pico2w-mpy-or-xesp32s3-mpy> \
  --ota-public-key build/keys/ota-public-key.json
```

Normal deploys preserve an existing `/ota-public-key.json` when this option is
omitted. Do not commit the private key.

The resulting `manifest.json` records `from_tag`, `to_tag`, package id, file
hashes, and the firmware version derived from the source tree at the `--from`
ref. Sensorius should use that manifest data to match the selected package to
the retained device version before initiating OTA.

The required compiled tag-range package interface is:

```text
nodus-ota package --from tagA --to tagB --target pico2w \
  --compiled-root build/firmware/pico2w --out build/ota/tagA_tagB \
  --signing-key /secure/path/cpynodusii-ota-private.pem
nodus-ota push build/ota/tagA_tagB \
  --prepare --broker <broker> --device-id <device-id> \
  --device http://co2-ykdvea.local:8000
```

Required repository interface after compiled-artifact support is implemented:

```text
python scripts/nodus_ota.py package --from tagA --to tagB --target pico2w \
  --compiled-root build/firmware/pico2w --out build/ota/tagA_tagB \
  --signing-key /secure/path/cpynodusii-ota-private.pem
python scripts/nodus_ota.py push build/ota/tagA_tagB \
  --prepare --broker <broker> --device-id co2-ykdvea \
  --device http://co2-ykdvea.local:8000
```

`package-worktree` is available for pre-tag validation. It uses the selected
target's compiled artifact root for importable modules and enforces the same
`.mpy` payload, matching `.py` deletion, signing, and path rules as a tag-range
package.

The `push --prepare` flow publishes `prepare` to
`nodus/<device-id>/fwupdate`, waits briefly for Nodus to reboot into temporary
OTA mode, then transfers the package over HTTP. The CLI logs each step with a
timestamp, including per-chunk offsets, per-file elapsed time, effective bytes
per second, commit time, and a package summary. When running Nodus from the
REPL, the device logs the prepare command, OTA-mode startup, HTTP
begin/file/commit handling, verification/apply timing, and reboot scheduling.
The combined flow creates the one-use session automatically. When `prepare`
and `push` are run separately, supply the same 32-or-more-character
`--session-id` to both commands.

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
- require an explicit target and a complete, current compiled artifact root;
- translate changed CircuitPython module paths from `.py` to the corresponding
  target-specific `.mpy` artifacts;
- allow `.py` payloads only for `boot.py`, `code.py`, `dataclass.py`, and
  `dataclasses.py`;
- add the matching `.py` path to `delete` whenever a compiled `.mpy` module is
  included;
- reject a package containing any other `.py` path or containing both source
  and compiled forms of one module;
- include only deployable firmware paths by default;
- include board TOML templates under `boards/`;
- exclude development-only paths such as `.git/`, `tests/`, `docs/`,
  `__pycache__/`, and host build output;
- support explicit include/exclude overrides for recovery testing;
- compute size and SHA-256 for each file;
- emit a compact JSON manifest.

Recommended initial package layout:

```text
manifest.json
manifest.sig
files/
  code.py
  boot.py
  dataclasses.py
  cpynodus_ii/app.mpy
  cpynodus_ii/core/settings.mpy
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
  "schema": "nodus-ota/v2",
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
      "path": "cpynodus_ii/app.mpy",
      "size": 12345,
      "sha256": "..."
    }
  ],
  "delete": [
    "cpynodus_ii/app.py"
  ],
  "preserve": [
    "settings.toml",
    "sensor_i2c.toml",
    "sensor_soil.toml",
    "switch.toml",
    "ota-public-key.json"
  ],
  "post_apply": {
    "reboot": true,
    "resume_profile": "previous"
  }
}
```

Manifest rules:

- `boot.py`, `code.py`, `dataclass.py`, and `dataclasses.py` are the only
  permitted `.py` payload paths. Every other CircuitPython module must use its
  `.mpy` path.
- Each `.mpy` entry must have its matching `.py` source path in `delete` so
  stale source and compiled modules cannot coexist on the device.
- `settings.toml`, `sensor_i2c.toml`, `sensor_soil.toml`, and `switch.toml`
  are preserved unless listed explicitly in `files`.
- `ota-public-key.json` is preserved. Generated packages do not deploy or
  delete device-specific OTA trust identity.
- `target.platform` should match the deploy target, currently `pico2w` or
  `xesp32s3`; `target.circuitpython` should match that target's verified
  CircuitPython runtime.
- `requires.version` must exactly match installed firmware. There is no force
  or downgrade bypass.
- `manifest.sig` signs the exact `manifest.json` bytes and identifies the
  trusted key. Editing or reformatting the manifest after signing invalidates
  it.
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

If the transfer is explicitly aborted, `/ota/begin` receives invalid JSON or a
rejected manifest, or OTA startup cannot reach network/HTTP readiness, Nodus
removes `/_ota/state.json` so the next reboot resumes normal runtime. A retry
requires a fresh MQTT prepare command.

Ready OTA mode waits five minutes for `/ota/begin`. Once staging starts, each
accepted request refreshes a 15-minute inactivity timer. Expiring either timer,
or losing the OTA HTTP poll loop, clears the session and reboots to normal
runtime. This prevents a lost Sensorius job from stranding the device in OTA
mode.

The OTA intent should live outside the normal public config schema, for
example `/_ota/state.json`, so ordinary configuration remains stable. Keep this
file small and rewrite it atomically where possible.

Current OTA state shape:

```json
{
  "mode": "ota",
  "prior_profile": "sensorius",
  "package_id": "ota-v0.26.123.3-to-v0.26.124.1",
  "session_id": "32-or-more-random-hex-characters",
  "manifest_sha256": "64-lowercase-hex-characters",
  "key_id": "trusted-key-id",
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
2. Client sends `/ota/begin` with the exact manifest bytes, detached signature,
   trusted key ID, and one-use session header.
3. Nodus authenticates the manifest, then validates package identity, installed
   version, board/runtime target, MPY/source-deletion rules, and safe paths.
4. For each file, client calls `/ota/file/begin`.
5. Client sends 1024-byte chunks to `/ota/file/chunk` with the current offset.
6. Nodus appends each chunk to `/_ota/stage/<relative-path>`. An offset
   mismatch response includes the current device offset so the client can
   resume after a lost response.
7. Client calls `/ota/file/end`.
8. Nodus reports `Verifying file...`, streams the staged file from disk, then
   reports `Verified...` or `Verification failed...` before accepting or
   rejecting the file.
9. Client calls `/ota/commit`.
10. Nodus verifies all staged files again, backs up replaced files, applies
    changes, records `applied_pending_boot`, waits less than 10 seconds, and
    reboots.

Every mutating request carries `X-Nodus-OTA-Session`. `/ota/begin` also carries
`X-Nodus-OTA-Key-Id` and `X-Nodus-OTA-Signature`.

The legacy whole-file upload endpoint follows the same authenticated session,
but
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

Nodus uses a journaled single-slot workspace and transactional apply:

1. On `/ota/begin`, clear stale `/_ota/stage/`, stale `/_ota/backup/`, and
   stale `/_ota/*.tmp` files from prior attempts.
2. Stage all incoming files under `/_ota/stage/`.
3. Validate the complete package before touching live firmware paths.
4. Persist `/_ota/transaction.json` with every affected path and whether it
   existed before apply, then write OTA state phase `applying`.
5. Copy each staged file to a same-directory `.ota-new` temporary, back up an
   existing live file, and rename the temporary into place. Apply manifest
   deletions only after backing up their live files.
6. On apply failure, restore replaced and deleted files and remove files that
   did not exist before the transaction.
7. Write `/_ota/state.json` phase `applied_pending_boot`, remove staging, wait
   less than 10 seconds, and reboot.
8. On the first normal boot, write phase `boot_pending`. A reset before the
   prior profile reaches its health checkpoint causes the next boot to restore
   the journaled transaction.
9. MQTT profiles mark the update `applied` only after the MQTT client
   successfully sends the queued `fwupdate/result`. Non-MQTT profiles mark it
   applied after their runtime services start.
10. Publish `nodus/<device_id>/fwupdate/result` with `phase="applied"`.

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
- remove `/_ota/state.json` for failed or aborted updates so normal startup
  resumes the configured profile;
- reboot;
- expose rollback reason in `/ota/status`, serial logs, reboot logs, and later
  Sensorius metadata.

Power loss during phase `applying` is recovered before normal profile startup.
The retained `boot_pending` phase also provides a one-boot health checkpoint;
for MQTT profiles that checkpoint requires reconnect and startup publication.

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

Sensorius retains its 60-second settle plus 90-second readiness windows and
shows `Nodus OTA mode booting...` during both. On timeout it makes a best-effort
abort request. It makes three total attempts per file and stops a device update
after 30 minutes. Nodus independently returns to normal runtime on its own
five-minute begin or 15-minute staging inactivity timeout.

## Security Model

- Production packages use `nodus-ota/v2` and a detached RSA-2048
  PKCS#1-v1.5/SHA-256 signature.
- Nodus authenticates the exact manifest before staging package content.
- MQTT prepare binds one random session to the package ID, manifest digest, and
  trusted key ID.
- All mutating HTTP requests require that session.
- The device holds only `/ota-public-key.json`; private signing keys remain
  off-device and outside Git.
- HTTP provides no confidentiality. Use an isolated management network when
  package contents or device addressing must not be observable.
- Do not include Wi-Fi or MQTT credentials in OTA packages.

## Implementation Phases

1. Package builder and tests.
   - Implemented target-specific compiled tag-range and worktree packages.
   - Implemented include/exclude behavior.
   - Generates signed `manifest.json` plus `manifest.sig`.
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
   - Implemented journaled backup/apply/restore routines for apply failures and
     power loss during apply.
   - Implemented a retained one-boot `boot_pending` checkpoint; MQTT reconnect
     and startup publication mark the new image `applied`.
   - A second boot before that checkpoint restores the prior image.
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
- target-specific `.mpy` artifact selection for changed source modules;
- rejection of `.py` payloads other than `boot.py`, `code.py`, and
  `dataclass.py`/`dataclasses.py`;
- required matching `.py` deletion for every packaged `.mpy` module;
- rejection when both `.py` and `.mpy` forms of a module are present;
- manifest path normalization and exclusion rules;
- rejected missing tags;
- rejected path traversal;
- delete list validation;
- simulated upload session success;
- chunked file staging and offset mismatch;
- simulated SHA-256 mismatch;
- simulated rollback state transitions.
- exact signed-manifest verification and tamper rejection;
- rejected unsigned v1 manifests, wrong targets, wrong installed versions,
  missing source deletion, wrong sessions, and wrong signing keys.

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
- remaining: verify RSA manifest authentication, wrong-signature rejection,
  session rejection, heap headroom, and authentication time on both `pico2w`
  and `xesp32s3`.

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
cpynodus_ii/core/ntp.mpy                  9,329 B   40s
cpynodus_ii/features/payloads.mpy        18,778 B   80s
cpynodus_ii/hardware/switch_adapter.mpy   4,207 B   21s
cpynodus_ii/ota/runtime.mpy               4,125 B   18s
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

- Whether commit can safely skip the second SHA-256 pass when `/ota/file/end`
  has already verified each staged file.
- Whether a future release should support an overlap window with two trusted
  public keys for key rotation.
