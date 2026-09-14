# Lesson 13: Extension project

[Previous](12-signed-ota-updates.md) · [Lesson index](README.md)

## What you will learn

Turn a small requirement into a reviewable change with tests, documentation,
and bounded hardware validation. Allow 6–10 hours over two or more sessions.

## Preparation

Complete Lessons 1–7 and 11; add Lessons 8–10 or 12 for the chosen area. Read
[extending](../extending.md), [contributing](../CONTRIBUTING.md), and the current
contract docs for affected behavior. Agree on available hardware and profile
with the instructor before selecting a project. Core hardware work uses the
Pico2 W/BME280/S1 fixture; the XIAO port described in the index is extra credit.

## Choose a bounded project

| Project | Starting points | Required evidence |
| --- | --- | --- |
| Add a derived sensor metric | `enrich_metrics`, payload builders, metric tests | Known inputs, units, missing-input behavior, payload observation |
| Add a supported I2C sensor path | Sensor service, adapter, settings, board templates, driver staging | Fake-driver tests, hardware detection/read, dependency and heap record |
| Add a host-side observation tool | Existing log retrieval and analysis scripts | Fixture tests, bounded input handling, documented output |

Locate these in [derived_metrics.py](../../cpynodus_ii/features/derived_metrics.py),
[sensor_service.py](../../cpynodus_ii/features/sensor_service.py),
[sensor_adapter.py](../../cpynodus_ii/hardware/sensor_adapter.py),
[settings.py](../../cpynodus_ii/core/settings.py),
[payloads.py](../../cpynodus_ii/features/payloads.py), and
[nodus_log_analyze.py](../../scripts/nodus_log_analyze.py).
A new broker, transport stack, or recovery strategy is too broad for the default
project; propose a separately reviewed advanced investigation if that is your aim.

## Design worksheet

Complete this before editing runtime code:

```text
Problem and user-visible outcome:
Chosen profile and board:
Current behavior and source symbols:
Proposed behavior and files:
Inputs, units, output names, and missing-input behavior:
Public config/topic compatibility:
Expected allocation lifetime and cleanup ownership:
Host acceptance cases:
Hardware/broker validation and stop conditions:
Restore procedure:
```

Run `git status --short` and preserve unrelated work. For startup, network,
MQTT, recovery, OTA, persistence, or switch changes, document the current
contract and obtain operator approval for the proposed runtime behavior before
editing. Read-only investigation and host exercise design can proceed first.

## Implementation lab

1. Start from one existing adjacent test and write a new assertion for the
   required observable outcome. Demonstrate a failing case before implementation.
2. Add the smallest implementation using existing patterns. Firmware code uses
   CircuitPython-compatible APIs; host-only tools may use CPython libraries.
   Avoid new tasks, retained history, or feature-owned sockets unless the design
   explicitly needs and justifies them.
3. For a metric, test normal inputs, missing inputs, and a boundary. For a sensor,
   test driver absence, read failure, and cleanup as well as successful reads.
4. Update relevant board templates and configuration models only when new keys
   are required. Update the reference docs and externally visible metric/topic
   contract deliberately. Preserve existing names and units.
5. Run focused tests, then the general host suite and diff checks. For runtime
   behavior changes, bump `__version__` once using the repository's year/day and
   daily-counter rule. Documentation or host-tool-only changes do not need a
   firmware version bump.
6. Have the operator deploy for the agreed hardware validation. Record board,
   CircuitPython/firmware versions, profile, duration, and actual observations.
   Capture the broker for MQTT claims. Restore the fixture after testing.

## Assessment and reference approach

Submit the worksheet, patch, tests, updated docs, and evidence report. Assess
correctness and boundary behavior (40%), tests/evidence (30%), constrained-board
fit (20%), and clarity/reproducibility (10%). Instructors may accept a host-only
project when its claim is host-only; unperformed hardware validation must remain
an explicit limitation rather than a passing hardware claim.

A good metric solution is a small computation in the existing enrichment path,
with stable units and no result when required inputs are absent. Its tests cover
calculation and payload inclusion. A sensor solution also covers config loading,
driver staging, bus binding, startup/read errors, and physical validation.
There is no single reference patch because hardware and chosen requirements
vary; these observable criteria define the reference approach.

What did you choose not to include to keep the change measurable? Which remaining
assumption presents the greatest risk on a constrained CircuitPython board?
