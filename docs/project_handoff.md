# cPyNodus_II Project Handoff

This document captures the working direction and decisions established while
bootstrapping `cPyNodus_II`.

## Project intent

`cPyNodus_II` is a fresh CircuitPython 9.2.8 codebase for the Raspberry Pi Pico
2 W. It is meant to replace the current `cPyNodus` incrementally, not by
wholesale copying the old implementation.

The main motivation is runtime stability, especially around MQTT transport under
switch-command load.

## Background and field observations

### Existing projects

- `~/Projects/cPyNodus`
- `~/Projects/cPySwitch`

### Observed behavior

- Sensor-only `cPyNodus` devices can publish for days without obvious failure.
- Once switch toggling starts on `cPyNodus`, the MQTT stack has been observed to
  degrade over time, followed by network collapse and reboot.
- `cPySwitch`, built as a smaller MQTT switch-only implementation with the same
  MQTT contract, has shown significantly better behavior:
  - hours of stable operation
  - steady heap behavior
  - no observed MQTT network stack collapses during the test window

### Working conclusion

The likely problem is not generic MQTT publishing. The weak area is the heavier
command/toggle path and the amount of work retained or triggered during switch
activity.

## Architectural decision

Use `cPySwitch` as the architectural seed for `cPyNodus_II`, and use
`cPyNodus` as the behavioral specification.

Do **not** port `cPyNodus` wholesale into the new project.

Instead:

1. Inventory the existing `cPyNodus` behavior.
2. Write characterization tests for that behavior.
3. Rebuild behavior in small slices on top of a simpler runtime.
4. Validate each slice with host tests and on-device soak testing.

## Design rules for future Codex work

These rules should guide future VS Code Codex sessions working in
`cPyNodus_II`.

- Do not bulk-copy the old `cPyNodus` codebase into this repo.
- Treat `cPyNodus` as a specification to preserve, not as implementation to
  inherit blindly.
- Keep MQTT transport minimal and transport-scoped.
- Do not place calibration, config-apply, metadata patching, or other heavy
  control-plane work directly into the transport path unless there is measured
  device evidence that it is safe.
- Prefer explicit state machines and narrow modules over implicit shared global
  behavior.
- Add one feature slice at a time.
- Before implementing a slice, write or update characterization tests.
- Favor host-testable modules and deterministic behavior.
- Any MQTT hot-path change should include a memory/allocation review and an
  on-device soak plan.

## Planned implementation slices

1. Boot, settings, profile selection, and startup planning
2. Wi-Fi and MQTT connect/reconnect lifecycle
3. Sensor-only steady-state publish
4. Switch-only command handling
5. Combined sensor + switch runtime
6. Runtime config apply and metadata patching
7. Calibration flows
8. AP/onboarding and local web flows

## Current scaffold state

The following scaffold decisions are already in place:

- project root: `~/Projects/cPyNodus_II`
- package layout under `cpynodus_ii/`
- host-testable startup planning and minimal transport facade
- deploy script adapted from the existing `cPyNodus` deploy workflow
- real `boot.py` behavior ported from `cPyNodus`

## Boot.py decision

`boot.py` in `cPyNodus_II` should match the proven `cPyNodus` behavior for
filesystem/USB mode handling.

### Required semantics

- `GP14` low:
  - app filesystem writable
  - USB mass storage disabled
  - REPL enabled
- `GP14` high:
  - app filesystem read-only
  - USB mass storage exposed
  - REPL enabled

### Additional retained behavior

- store desired ROFS/RWFS intent in `microcontroller.nvm[0]`
- clear the cold-boot bounce marker in `microcontroller.nvm[1]` on true power
  events
- fail safe toward edit/recovery mode if guard pin setup fails

## Deploy script decision

The old deploy workflow was intentionally reused instead of inventing a new
one.

### Source

- adapted from `~/Projects/cPyNodus/scripts/deploy_nodus.sh`

### Reason

The existing script already had the desired properties:

- `rsync` based
- CIRCUITPY path safety checks
- local and remote SSH targets
- dry-run support
- deprecated target pruning

### cPyNodus_II-specific adaptation

`cPyNodus_II` uses a package layout, so the deploy script must understand the
`cpynodus_ii/` directory instead of assuming a flat root-file firmware layout.

### Current deploy expectations

The `runtime` deploy mode should sync:

- `boot.py`
- `code.py`
- `cpynodus_ii/`
- root `*.def` files
- `lib/` if present

## Coverage guidance

“100% coverage” should mean behavioral coverage first, not only line coverage.

Coverage goals should include:

- MQTT topic and payload contract coverage
- profile coverage:
  - `nodusweb`
  - `sensorius`
  - `homeassistant`
  - `weewx`
- device-mode coverage:
  - sensor-only
  - switch-only
  - sensor + switch
- persistence coverage for:
  - `settings.toml`
  - `switch.toml`
  - sensor-specific TOML
  - calibration writes
- failure coverage:
  - broker unavailable
  - DNS failure
  - Wi-Fi failure
  - publish timeout
  - degraded socket behavior
  - low-memory protective behavior
- on-device soak coverage under repeated switch toggling plus sensor publish

## Recommended next steps

1. Replace scaffold settings with TOML-backed settings loading.
2. Write characterization tests for existing `cPyNodus` profile and settings
   behavior.
3. Characterize current switch MQTT contract and publish sequence from the
   existing system.
4. Implement slice 1 and slice 2 before adding any calibration or web behavior.
5. Keep the first real on-device validation focused on:
   - boot behavior in ROFS/RWFS modes
   - sensor-only publish stability
   - switch-only command stability

## Operator notes for future sessions

When working on this repo, future Codex sessions should assume:

- stability is more important than feature velocity
- smaller runtime surfaces are preferred
- `cPySwitch` is the reference for simplification style
- `cPyNodus` is the reference for required behavior and MQTT contract coverage
- code should be added only when the required behavior is identified and pinned
  by tests or documentation
