# Warm-Start MQTT Investigation - 2026-05-21

## Goal

Resolve the app warm-start failure where TCP to the broker succeeds, but MQTT
CONNACK times out with `OSError:[Errno 116] ETIMEDOUT`, followed by repeated
MiniMQTT `Connect failure`.

## Failure Pattern

Observed on warm app starts:

- Wi-Fi connects and has an IP.
- Broker TCP preflight succeeds quickly.
- Raw MQTT connect probe times out waiting for CONNACK.
- Normal MiniMQTT connect then fails.
- The device can continue retrying until the hard-reset guard fires.
- A hard reset clears the condition and the next cold boot connects.

This pattern is distinct from normal transient Wi-Fi startup failures such as
unknown failure 205, authentication noise, or scan misses.

## Overnight Baseline

Two devices ran `v0.26.140.26` overnight from cold starts:

- `co2-frank`, captured by the Raspberry Pi MQTT log.
- `aqi-wfcp7p`, captured by `cu.usbmodem1334301.log`.

Both showed broker-visible MQTT messages. Cold starts were broadly healthy, so
the focus moved to warm-start behavior.

## Platform Test Observations

The platform warm diagnostics command repeatedly succeeded in the same
environment where app warm starts failed:

```python
import gc; gc.collect(); import platform_test
platform_test.run(
    mqtt_mode="warm_diagnostics",
    scans=0,
    reset_station=False,
    sync_ntp=False,
    mqtt_rounds=3,
)
```

Each warm diagnostics round performs:

- TCP probe.
- Raw MQTT CONNECT/CONNACK probe.
- Firmware-style MiniMQTT connect/close.
- Direct plain MiniMQTT connect/close.
- Direct optional-kwargs MiniMQTT connect/close.
- Firmware-style connect, publish, sync, and close.

The broker saw the `platform_test/warm_diagnostics` publishes.

## Ruled-Out Suspects

These variations still passed:

- `sync_ntp=True`
- `scans=1`
- `reset_station=True`
- `scans=1, reset_station=True, sync_ntp=True`

This weakens NTP, scan behavior, and station reset as primary causes.

## Key Narrowing Result

A single full warm diagnostics round also passed:

```python
import gc; gc.collect(); import platform_test
platform_test.run(
    mqtt_mode="warm_diagnostics",
    scans=0,
    reset_station=False,
    sync_ntp=False,
    mqtt_rounds=1,
)
```

Serial showed:

- TCP OK.
- Raw MQTT CONNACK OK.
- Firmware connect OK.
- Direct plain connect OK.
- Direct optional connect OK.
- Publish synced.
- Summary `passed=1 failed=0`.

Broker confirmation:

```text
2026-05-21T11:56:21-0600 nodus/aqi-wfcp7p/platform_test/warm_diagnostics platform_test:warm_diagnostics:aqi-wfcp7p:1:1932.358
```

This suggests repetition is not required; one full platform-style conditioning
cycle is enough to exercise the successful path.

## App Gap Found

The app had an MQTT conditioner, but it was not the full proven platform
sequence:

- It skipped direct-plain by default.
- It did not publish a broker-visible warm diagnostics message.
- It treated partial success as sufficient.

That did not match the successful `platform_test warm_diagnostics` behavior.

## Runtime Changes Made

`v0.26.141.10` restored the short hard-reset behavior for the unrecoverable
warm-start MQTT condition.

`v0.26.141.11` changes the app CONNACK-timeout conditioner to more closely
match the proven platform sequence:

- Direct-plain connect is included by default.
- A broker-visible publish/sync probe is included.
- The conditioner reports publish status in its summary.
- A full conditioner pass requires raw, firmware, direct plain, direct optional,
  and publish probes to pass.
- CONNACK-timeout warmup rounds are set to 1 to match the broker-proven
  single-round result.
- The existing 60-second hard-reset fallback remains the final guard.

`v0.26.141.12` changes controlled warm-start cleanup to be closer to
`platform_test._reset_station()`:

- Warm-start cleanup now does `stop_ap`, `disconnect`, `stop_station`, and
  `start_station`.
- It no longer toggles `wifi.radio.enabled` during warm-start cleanup.
- Controlled soft-reboot/recovery shutdown no longer requests a radio power
  cycle during network teardown.

`v0.26.141.13` adds the next warm-start recovery path after the line 10411
test showed `Errno 119 EINPROGRESS` before MiniMQTT:

- TCP preflight and raw MQTT connect-probe now retry bounded socket-progress
  failures before MiniMQTT is allowed to connect.
- A successful preflight/probe pair now gets a short settle delay before
  MiniMQTT connect, matching the older cPyNodus behavior.
- Persistent `Errno 119` is classified as `socket_progress`, not as
  `raw_pattern=none`.
- The app avoids falling into MiniMQTT for persistent socket-progress failures.
  It resets station state without a radio power cycle, rebuilds socket
  artifacts, runs the full platform-style conditioner, and only then retries
  normal MQTT connect.

`v0.26.141.14` aligns the preflight-to-MiniMQTT timing with the older
cPyNodus `cPyMQTTClient.mqtt_reconnect()` pattern:

- After successful TCP preflight and raw MQTT connect-probe, the app waits
  5 seconds before calling MiniMQTT connect.
- If MiniMQTT connect still fails after clean probes, the delay increases by
  3 seconds up to a bounded cap.
- A successful MQTT connection resets the delay back to the 5-second base.

The line 10411 serial-log review showed `v0.26.141.14` improved clean
preflight starts, but did not fix the later warm-start `Errno 116` path:

- `v0.26.141.12` failed with `Errno 119`; after `platform_test
  warm_diagnostics`, the same `.12` app warm-started successfully.
- `v0.26.141.14` was deployed after that and inherited the good state.
- A later `.14` warm start failed with TCP preflight OK but raw MQTT CONNACK
  timeout `Errno 116`.
- The app ran the MQTT conditioner, but did not first perform the full
  platform-test-style station reset and socket rebuild from the failed state.
- The failed conditioner then fell through into MiniMQTT, delaying the hard
  reset path.

`v0.26.141.15` moves the immediate `Errno 116` CONNACK-timeout path closer to
the proven platform-test sequence:

- Close/mark the MQTT client disconnected on the raw CONNACK timeout.
- Reset station state without a radio power cycle.
- Rebuild socket artifacts and the MQTT adapter.
- Run the full platform-style MQTT conditioner after the reset/rebuild.
- Re-run the normal preconnect probe after a conditioner pass.
- Do not call MiniMQTT unless the follow-up raw preconnect path is clean.

The line 10852 serial-log review showed `.15` was operationally non-viable:

- The first conditioner pass consumed more than the 60-second hard-reset
  budget before checking again.
- `firmware_connect` called MiniMQTT even after its own raw connect-probe had
  already failed.
- `direct_plain_connect` then consumed another long MiniMQTT failure window.
- The new `will_minimqtt=0` branches skipped the old hard-reset decision path,
  so the app kept looping with `hard_reset_elapsed_s` well over budget.

`v0.26.141.16` makes the failure path bounded:

- The conditioner stops after failed raw/firmware probes instead of continuing
  into later MiniMQTT probes.
- `firmware_connect` no longer calls MiniMQTT after its preconnect probe fails.
- Every `will_minimqtt=0` recovery branch checks the hard-reset budget before
  continuing.
- Station-reset/rebuild branches check the budget before launching another
  conditioner pass.

`v0.26.141.17` simplifies the reset decision after a failed preflight:

- The first `116`/persistent `119` preflight failure still closes MQTT, resets
  station state, and rebuilds socket artifacts.
- After that reset, the app immediately re-runs the bounded preflight/probe
  path on the rebuilt stack.
- If the rebuilt-stack preflight/probe still fails, the app skips the
  conditioner and MiniMQTT and records a post-reset preflight failure.
- Three repeated MQTT preflight failures are now enough to trigger the reset
  path, even before the 60-second budget expires.

## Cold-Boot Primer Finding

The `nocode.py` experiment separated app auto-start from platform conditioning:

- Rename `code.py` aside so cold boot lands in REPL with no app running.
- Run `platform_test warm_diagnostics`.
- Start the app manually with `supervisor.set_next_code_file("nocode.py")`
  and `supervisor.reload()`.
- Repeated app starts then connect cleanly.

Broker-visible confirmation:

- `13:58:48`, `13:58:49`, `13:58:51`: three
  `platform_test/warm_diagnostics` publishes.
- `13:59:48`: one more single-round `platform_test/warm_diagnostics` publish.
- `14:00:38`, `14:02:02`, `14:08:52`: repeated app `/meta`, heartbeat,
  availability, and data publishes from `v0.26.141.17`.

This shows the useful part is not repeated app cleanup alone. A small
platform-style MQTT conditioning pass run before the first app start can make
later soft app restarts behave.

`v0.26.141.18` adds that as a deliberate boot stage:

- New root `primer.py` runs one minimal `warm_diagnostics`-style MQTT sequence.
- `boot.py` schedules `primer.py` once after true cold/power/brownout boot.
- A new NVM byte at index `3` prevents primer loops across the primer-to-app
  soft reload.
- `primer.py` always chains into `code.py`, even if the primer fails.
- The primer publishes to the same broker topic:
  `nodus/<device>/platform_test/warm_diagnostics`.

The line 11505 test did not exercise the primer. The serial log showed direct
`code.py` startup with no `[boot.py] cold-boot primer scheduled` line, no
`primer` lines, and no broker-visible primer publish. The warm-start failure
therefore remained the same `tcp_ok` plus `Errno 116` CONNACK timeout pattern.

`v0.26.141.19` changes primer scheduling from reset-reason based to marker
based:

- Power/brownout boots still clear NVM byte `3`.
- Any boot with primer marker byte `3` clear schedules `primer.py`.
- Requested, done, and failed marker values prevent primer loops across the
  primer-to-app reload.

The line 11719 test showed `boot.py` alone still did not prove sufficient:

- The serial log entered `code.py` directly at `v0.26.141.19`.
- There was no `[boot.py] cold-boot primer scheduled` line.
- There were no `primer` serial lines.
- There was no broker-visible `platform_test/warm_diagnostics` publish before
  the app started.
- The next warm start returned to the familiar `tcp_ok` plus raw MQTT CONNACK
  timeout path.

`v0.26.141.20` makes `code.py` consume the primer marker before importing the
app:

- If NVM byte `3` is clear/requested, `code.py` imports `primer.py` before
  loading `cpynodus_ii.app`.
- This preserves `boot.py` as the cold-boot marker setup, but no longer depends
  on `supervisor.set_next_code_file("primer.py")` taking effect immediately.
- `DONE` and `FAILED` markers still stop the primer so the primer-to-app reload
  cannot loop.
- A visible `runtime primer action=import ...` line should now appear before
  primer serial output when `code.py` is the file CircuitPython launches.

Follow-up deploy review found the practical reason this still could not run:

- `scripts/deploy_nodus.sh --content runtime` only copied `boot.py`, `code.py`,
  and `dataclasses.py` from the CIRCUITPY root.
- `scripts/ota_package.py` also did not include `primer.py` in its root
  deployable allowlist.
- Both paths now include `primer.py`, so deploys and OTA packages can actually
  place the primer at the root where `code.py` expects it.

The line 11912 test showed `primer.py` was finally present and executable, but
the first production primer was still not equivalent to the proven manual
platform-test path:

- `primer.py` ran one warm-diagnostic round, published successfully, and
  immediately chained into `code.py`.
- The app then failed with repeated `Errno 119 EINPROGRESS` TCP preflight
  failures.
- This differs from the proven REPL workflow, which used three
  `platform_test/warm_diagnostics` publishes and had a short human-scale gap
  before app startup.

`v0.26.141.21` makes the primer closer to that proven workflow:

- Run three warm-diagnostic rounds instead of one.
- Log a pass/fail summary across those rounds.
- Wait 15 seconds before chaining to `code.py`, avoiding an immediate app
  restart on top of the just-exercised MQTT/socket path.

The line 12060 test showed that automatic cold-boot primer integration still
does not match the successful manual experiment:

- `primer.py` ran three diagnostic cycles and all three passed.
- The app then started from the primer-triggered soft reload and hit
  `tcp_ok` followed by raw MQTT CONNACK timeout `Errno 116`.
- The app hard-reset path fired at repeated-count `3`, and the following hard
  reset connected successfully.
- A later normal warm start again reproduced the same `tcp_ok` plus
  `Errno 116` pattern.

`v0.26.141.22` backs out the automatic primer startup hooks:

- `primer.py` remains in the repository and deployable root file set.
- `boot.py` no longer schedules `primer.py`.
- `code.py` no longer imports `primer.py` before the app.
- The primer remains available as an explicit/manual diagnostic and controlled
  experiment tool rather than part of normal startup.

## Remaining Validation

Deploy the current build and validate the normal app path:

1. Deploy firmware with `primer.py` still present at the CIRCUITPY root.
2. Confirm normal cold boot enters `code.py` directly with no automatic primer
   serial lines.
3. Keep using explicit `primer.py` or `platform_test.py` REPL runs to compare
   the manual conditioning path against app warm starts.

`v0.26.141.23` narrows `primer.py` back toward the earlier successful
connect-only warm diagnostic path:

- `platform_test.py` started the day with a warm-diagnostics path that exercised
  TCP, raw MQTT CONNECT/CONNACK, firmware preflight/connect, and direct
  MiniMQTT connect probes without a broker-visible publish.
- The later broker-visible `platform_test/warm_diagnostics` publish was added
  for validation, and `primer.py` had copied that heavier path.
- `primer.py` now runs one connect-only warm diagnostic round and does not
  publish a platform-test MQTT message.
- The manual primer-to-app delay is now 5 seconds, preserving a short settle
  interval without the previous 15-second delay.

The 15:20-15:22 test gives a useful positive signal:

- `v0.26.141.23` started and connected with `tcp_ok`, `connack_ok`, the
  5-second MiniMQTT delay, and `mqtt connect phase=connected`.
- After KBINT, the manually run minimal `primer.py` performed one connect-only
  cycle: TCP OK, raw CONNACK OK, firmware preflight/connect OK, direct plain
  MiniMQTT connect OK, and direct optional-kwargs MiniMQTT connect OK.
- The primer did not publish a `platform_test/warm_diagnostics` broker message.
- The primer chained into `code.py`; the app connected cleanly.
- A subsequent REPL soft reboot, without running primer again, also connected
  cleanly. This is the strongest evidence so far that the minimal primer path
  may condition persistent warm-state rather than only producing a one-shot
  successful transition.

`v0.26.141.24` makes `code.py` own the startup primer call:

- `code.py` imports `primer.py`, calls `primer.run()`, and then starts the app
  in the same VM pass.
- `primer.py` no longer reloads into `code.py` merely because it was imported.
- `primer.run_and_chain()` preserves the manual REPL workflow when an explicit
  primer-to-app reload is wanted.
- `STARTUP_PRIMER_ENABLED` in `code.py` is the simple kill switch for this
  experiment.
- NVM byte `3` is now only a one-start skip marker for standalone/manual
  primer reloads, not a normal-startup requirement.

The line 12666 `.24` boot showed no `runtime primer` or `primer` output before
the app startup logs. That made the startup path ambiguous, and the NVM skip
logic was more complex than the experiment needs.

`v0.26.141.25` simplifies `code.py` further:

- `STARTUP_PRIMER_ENABLED` remains the single kill switch.
- When enabled, `code.py` always imports `primer.py`, logs
  `runtime primer action=run`, calls `primer.run()`, removes `primer` from
  `sys.modules`, runs GC, and then imports/starts the app.
- There is no NVM marker gate in the normal startup path.
- A missing or broken `primer.py` logs `runtime primer action=error ...` and
  continues into the app instead of blocking startup.

The `.25` inline-primer test failed after the primer passed:

- Primer completed one connect-only round successfully.
- `code.py` then hit `MemoryError: memory allocation failed, allocating
  10881 bytes` while importing `cpynodus_ii.app`.
- Deleting `primer` from `sys.modules` and running GC was not enough on
  CircuitPython; the primer imports still consumed too much heap for app import.

`v0.26.141.26` restores the required VM boundary while keeping the logic small:

- If `STARTUP_PRIMER_ENABLED` is true and NVM byte `3` is clear, `code.py`
  calls `primer.run_and_chain()`.
- `primer.run_and_chain()` runs the minimal connect-only primer, writes byte
  `3` as pass/fail, and reloads into `code.py`.
- On that next `code.py` pass, byte `3` causes one skip; `code.py` clears it
  and starts the app.
- This should run primer before every normal app startup, but not loop during
  the primer-to-app reload.

The line 12844 `.26` run showed this flow working:

- First `code.py` pass logged `runtime primer action=run_and_chain marker=0`.
- The minimal primer needed two Wi-Fi attempts because the first saw
  `No network with that ssid`, then completed one connect-only MQTT cycle.
- Primer wrote marker `12`, waited 5 seconds, set next code file to `code.py`,
  and reloaded.
- Second `code.py` pass logged `runtime primer action=skip marker=12 reset=1`.
- The app started with a clean VM boundary, connected to Wi-Fi, saw MQTT
  `tcp_ok` and `connack_ok`, waited 5 seconds, and connected successfully.
- This is functional enough for overnight testing on multiple devices, even
  though the two-pass startup is not the final desired shape.

The same `.26` run exposed a serious memory regression:

- The primer pass still had usable headroom: `free_mem=251584` before the
  warm diagnostic round and `free_mem=217136` after it.
- After the primer-to-app reload, the app pass started MQTT connect with only
  `free_mem=69120 mem_alloc=330336`.
- After MQTT connected, the app reported `free_mem=72320 mem_alloc=327136`.
- The later periodic GC line showed the runtime could drop to
  `free_mem=32784` before GC recovered it to `free_mem=70176`.
- This means the `.25` inline-primer `MemoryError` was not just primer residue.
  The app import/runtime footprint has grown enough that adding any sizable
  same-VM startup helper is unsafe.

Next memory direction:

- Keep the primer isolated behind the reload boundary while testing.
- Stop adding recovery code to the main app until memory is trimmed.
- First trim target is today's app-level investigation code: conditioner,
  warmup, repeated long diagnostic format strings, and duplicate preflight
  decision paths that primer/platform_test now cover better.
- Target should be to restore well over 100 KB free after MQTT connect before
  treating this startup workaround as production-ready.

`v0.26.141.27` slims `primer.py` to the minimum functional path:

- No NTP.
- No broker-visible publish.
- No firmware MQTT adapter import.
- No MiniMQTT import.
- No `Settings` import.
- `primer.py` now reads only the needed settings from `settings.toml`, joins
  Wi-Fi, resolves the broker, opens a TCP socket, performs one raw MQTT
  CONNECT/CONNACK exchange, marks byte `3`, and reloads into `code.py`.
- The only firmware import left on the password path is
  `cpynodus_ii.core.obfuscation.decode_password`, and only when the stored
  Wi-Fi password starts with `obf1:`.

`v0.26.141.28` removes the failed in-app MQTT conditioner/warmup experiment:

- The old app-side conditioner/warmup helpers were deleted from `app.py`.
- `app.py` no longer imports or builds throwaway platform-style MQTT clients
  for warm-start recovery.
- The CONNACK-timeout recovery path still closes MQTT, resets/rebuilds the
  station/socket state, and preserves the bounded hard-reset decision, but logs
  `mqtt warmup phase=skipped reason=external_primer`.
- Current working-tree line counts after the cut: `app.py` is 4053 lines and
  `primer.py` is 611 lines.
- This keeps the warm-start workaround outside the main app heap while leaving
  the normal preflight, reconnect, and reset guardrails in place.

The first `.28` device log confirmed that the remaining memory pressure is
app-side, not primer-side:

- Minimal primer before raw MQTT: `free_mem=302832 mem_alloc=81168`.
- Minimal primer after raw MQTT: `free_mem=302800 mem_alloc=81200`.
- App MQTT connect context after the reload: `free_mem=95168 mem_alloc=316512`.
- App post-MQTT-connect: `free_mem=98368 mem_alloc=313312`.

This is much better than the `.26` app pass at roughly 69-72 KB free, but still
short of the earlier 120-140 KB target.

`v0.26.141.29` starts reducing app import cost:

- `app.py` no longer imports from the `cpynodus_ii.features` barrel module.
  That barrel imports web runtime/routes/handlers/services/config and other
  feature modules even when the current profile does not use them.
- `app.py` now imports the sensor, switch, service, and steady-state helpers
  directly.
- `WebRuntimeController` is imported lazily only when `plan.web_enabled` is
  true.
- `steady_state.py` imports onboarding web-services lazily only when a
  settings root is provided for startup publish.

## 2026-05-22 Rollback

`v0.26.142.1` rolls back automatic primer startup:

- `code.py` no longer imports or runs `primer.py` before the app.
- `scripts/deploy_nodus.sh` no longer copies `primer.py` during normal device
  deploys. Runtime deploy omits it from the root-file list; full deploy excludes
  it explicitly.
- `scripts/ota_package.py` also excludes `primer.py` from the OTA deployable
  root-file allowlist.
- `primer.py` remains in the repository for explicit REPL experiments.
- The app-side memory/import reductions and MQTT preflight/recovery work remain
  in place.

Reason:

- The primer workaround proved useful, but it is still an investigation tool.
- We need more controlled `platform_test.py` and `primer.py` experiments to
  isolate which warm-diagnostics step actually conditions the Pico2W/MQTT
  state.
- Normal firmware deploys should not silently depend on the primer until that
  mechanism is understood.

## 2026-05-22 Startup Test Apparatus

Added `testApparatus/startup_test.py` as a smaller mechanism-isolation tool.

Purpose:

- Test the suspected startup-conditioning actions one at a time, without
  importing the full app or the full `platform_test.py` MQTT stack.
- Keep `primer.py` available for manual comparison, but stop treating it as the
  production workaround.

Modes:

- `wifi`: settings load, password decode, hostname set, Wi-Fi connect.
- `pool`: `wifi` plus `socketpool.SocketPool(radio)`.
- `dns`: `pool` plus broker hostname/IP resolution.
- `tcp`: `dns` plus one broker TCP socket open/close.
- `raw`: `dns` plus one raw MQTT CONNECT/CONNACK/DISCONNECT.
- `primer`: reproduces the current minimal primer path, `dns` plus TCP then raw
  MQTT.
- `firmware_probe`: build the firmware MQTT adapter and run TCP/CONNACK
  preflight only.
- `firmware_connect`: run firmware preflight, MiniMQTT connect, and disconnect.
- `direct_plain`: run direct MiniMQTT connect/disconnect without optional
  kwargs.
- `direct`: run direct MiniMQTT connect/disconnect with app-style optional
  kwargs.
- `firmware_publish`: run firmware connect, publish one broker-visible
  startup-test message, and disconnect.
- `sequence`: run selected steps in platform-test order.
- `platform_like`: run the startup-test copy of the full platform-test
  warm-diagnostics sequence.

Example REPL command:

```python
import gc; gc.collect(); from testApparatus import startup_test
startup_test.run(mode="tcp", chain=True, chain_delay_s=5)
```

The planned experiment is to cold boot, run one mode with `chain=True`, let
`code.py` start, then test whether subsequent warm boots still fail or now
succeed. The smallest mode that makes warm boots succeed is the best current
candidate for the real mechanism.

## 2026-05-22 Sequence Isolation

The objective of `startup_test.py` is not to be a separate production startup
path. Its job is to reproduce the working `platform_test:warm_diagnostics`
startup shape with selectable steps, so we can identify which subset actually
conditions the Pico2W/MQTT state.

Useful command pattern when `startup_test.py` is copied to the CIRCUITPY root:

```python
import gc; gc.collect(); import startup_test
startup_test.run(
    mode="sequence",
    steps="tcp,raw,firmware_connect,direct,firmware_publish",
    chain=True,
    chain_delay_s=5,
)
```

The MQTT monitor was expanded to include:

```text
nodus/aqi-wfcp7p/startup_test/#
```

`startup_test.py` was also adjusted to make the MQTT side easier to correlate:

- Startup logs include the normalized `steps=` list.
- Each step logs start/done status.
- `firmware_publish` publishes to
  `nodus/<device>/startup_test/firmware_publish`.
- A run id is included in the broker-visible payload, so repeated REPL runs are
  distinguishable even when command-line editing/backspacing makes the serial
  command hard to read.

Test outcomes from the 1334301 serial/MQTT logs:

- `tcp,raw,firmware_connect` failed to make the following app warm start
  reliable.
- `tcp,raw,firmware_connect,firmware_publish` also failed.
- `tcp,raw,firmware_connect,direct_plain,direct,firmware_publish` had mixed
  results. The backspaced REPL command line was not considered causal after
  repeat testing.
- `direct_plain` was not required for `firmware_publish`, and it was not the
  differentiator.
- `tcp,raw,firmware_connect,direct,firmware_publish` became the best candidate:
  it produced several clean app starts, but the overall result was still only
  about `3/5`, so it is a useful clue rather than a proven production fix.

The important sequence difference versus current app startup was this:

- The app built the MQTT adapter, then put normal MQTT connect behind TCP/raw
  preflight.
- In the bad warm-start state, the first TCP/raw preflight could hit
  `EINPROGRESS` or raw CONNACK timeout, so the app never exercised the direct
  MiniMQTT path that appeared in the successful diagnostic sequence.
- The successful candidate exercised a direct MiniMQTT connect/disconnect before
  the final firmware publish/app path.

`primer.py` was updated to match the current best candidate for manual
comparison:

```python
PRIMER_STEPS = ("tcp", "raw", "firmware_connect", "direct", "firmware_publish")
```

`primer.run_and_chain()` remains a manual experiment. It is not reliable enough
to be the production path by itself.

## 2026-05-22 App Startup Patch

`v0.26.142.5` patches the hole directly in `cpynodus_ii/app.py`:

- After Wi-Fi/network setup and MQTT adapter construction, the app runs one
  direct MiniMQTT connect/disconnect conditioning probe.
- The direct probe intentionally does not set a custom `client_id`, matching the
  successful `startup_test` direct step.
- The probe uses bounded startup-only timeout/retry values.
- The app closes the direct client/socket, collects garbage, and rebuilds the
  normal MQTT adapter before entering the usual sensor/switch startup and MQTT
  preflight path.
- Existing TCP/raw preflight, reconnect, station-reset, and hard-reset guard
  behavior remains in place.

Expected serial markers:

```text
mqtt startup_conditioning phase=start
mqtt startup_conditioning phase=connected
mqtt startup_conditioning disconnect phase=ok
```

Then the normal app path should continue into:

```text
mqtt preflight phase=tcp_ok
mqtt connect_probe phase=connack_ok
mqtt connect phase=connected
```

If this patch fails, the next log review should separate two cases:

- `startup_conditioning` itself fails before the normal app path.
- `startup_conditioning` succeeds, but the rebuilt normal preflight still hits
  `EINPROGRESS` or raw CONNACK timeout afterward.

Host-side verification for the patch:

```text
python -m py_compile cpynodus_ii/app.py cpynodus_ii/__init__.py primer.py testApparatus/startup_test.py
pytest tests/test_app_startup.py tests/test_mqtt_client_adapter.py
```

Result: `98 passed`.

## 2026-05-22 Cold-Start Regression

The line 3900 review showed `v0.26.142.5` made cold starts worse:

- `startup_conditioning` itself connected and disconnected successfully.
- The normal app path then saw TCP preflight OK and raw CONNACK OK.
- The first normal MiniMQTT connect still failed with `Connect failure`.
- After that, raw connect probes degraded into `Errno 116 ETIMEDOUT` and
  sometimes `Errno 119 EINPROGRESS`.

Representative shape:

```text
mqtt startup_conditioning phase=connected
mqtt connect_probe phase=connack_ok
mqtt connect phase=error ... ('Connect failure', None)
mqtt connect_probe phase=connack_error ... Errno 116
```

Conclusion:

- Direct MiniMQTT conditioning before normal app MQTT connect is not safe as a
  default cold-start step.
- The earlier `3/5` sequence result was not strong enough to justify putting
  the direct probe into the normal app startup path.

The same review exposed a reset-budget gap:

- The first repeated MQTT failure set the NVM hard-reset marker and called
  `microcontroller.reset()`.
- The next app pass hit the same MQTT failure.
- The marker suppressed the next hard reset at the fast three-failure
  threshold.
- Because the suppression path reset the failure counters, the 60-second
  repeated-failure budget never fired. The app could loop indefinitely until a
  manual interrupt.

`v0.26.142.6` changes this:

- `MQTT_STARTUP_CONDITIONING_ENABLED` is now `False`.
- The startup-conditioning helper remains in the code for controlled testing,
  but it no longer runs on normal app startup.
- The hard-reset NVM marker now suppresses only the immediate fast repeat reset.
- If MQTT remains failed long enough to exhaust the 60-second budget, the app is
  allowed to reset again even when the marker is still present.
- The suppression log now uses `hard_reboot_deferred` to distinguish a temporary
  marker delay from a permanently suppressed reset.

Expected behavior after this patch:

- Cold start should return to the normal `.142.4` shape: no
  `startup_conditioning` serial lines before app boot.
- If repeated MQTT connect failures continue after a `microcontroller.reset()`,
  the app should defer the immediate reset but still reset when the 60-second
  failure budget is exhausted.

## 2026-05-22 Soft-Reload Conditioning Gate

The `.142.6` cold-start rollback leaves soft-reload warm starts expected to
fail. That makes the `microcontroller.reset()` recovery cleaner, but it also
leaves no useful app-side soft-reload path.

`v0.26.142.7` scopes startup conditioning to soft reloads only:

- `MQTT_STARTUP_CONDITIONING_ENABLED` remains `False`, so cold/power starts do
  not run direct MiniMQTT conditioning.
- New `MQTT_STARTUP_CONDITIONING_SOFT_RELOAD_ENABLED = True` lets the app run
  the conditioning pass when the current CircuitPython run reason contains
  `reload`.
- The same pass also runs when the app consumed its soft-reload cleanup marker,
  which is set before controlled or unprepared soft reload cleanup.
- `microcontroller.reset()` recovery should still skip startup conditioning,
  because it should come back as a startup/reset run rather than a VM reload.

Expected test shape:

- Cold/power or `microcontroller.reset()` start: no
  `mqtt startup_conditioning ...` lines.
- `supervisor.reload()` / Ctrl-D style soft reload: expect
  `mqtt startup_conditioning phase=start`, then the direct connect/disconnect
  markers before normal app MQTT connect.

Follow-up testing showed the soft-reload conditioning path still failed the app
warm start. The important observation is that a runtime soft reboot is not a
recovery path on this board; it becomes a delayed hard reset after MQTT fails
again.

`v0.26.142.8` changes recovery escalation policy:

- Recovery reasons selected by the app runtime now hard reset directly:
  `mqtt_recovery_timeout`, `mqtt_memory_allocation_failures`,
  `mqtt_repeated_connect_failures`, `wifi_after_ready_failure`,
  `wifi_recovery_timeout`, `ap_idle_timeout`, and `sensor_not_found`.
- This avoids the soft-reboot-then-hard-reset detour after a long recovery
  window has already expired.
- Explicit app/web/OTA soft reload callbacks are left unchanged for now; they
  are separate operational flows from fault recovery.

`v0.26.142.9` closes the remaining app-owned soft-reload hole for MQTT
profiles:

- App-requested soft restarts under `sensorius`, `weewx`, and `homeassistant`
  now promote to `microcontroller.reset()`.
- This covers web restart callbacks when available, `fwupdate_prepare`, and the
  OTA applied-pending-boot callback.
- REPL `Ctrl-D` still uses `supervisor.reload()` because it is a manual
  CircuitPython path rather than app recovery or app restart.
- The manual `primer.py` and `startup_test.py` warm-start harnesses were
  removed from the app side after this policy change. They are no longer
  expected deployment or REPL tools.

## 2026-05-22 Accepted Merge Position

The accepted warm-start strategy for `v0.26.142.9` is to avoid soft reload as an
app recovery path for MQTT profiles. Runtime recovery, web restart callbacks
when available, `fwupdate_prepare`, and OTA applied-pending-boot callbacks
promote to `microcontroller.reset()` under MQTT profiles. REPL `Ctrl-D` remains
a manual CircuitPython path.

Broker-visible validation is accepted for the merge redo goal:

- `aqi-wfcp7p` produced `/data` at the configured cadence.
- `aqi-wfcp7p` produced `/status/heartbeat` and `/availability` at the status
  cadence.
- Switch-capable behavior was validated through `S1-wfcp7p` config
  `set` / `ack` / `result`, switch `event`, switch `state`, and
  `aqi-wfcp7p/meta/patch`.

This closes the earlier investigation item that treated app warm-start soft
reload as a candidate recovery mechanism.
