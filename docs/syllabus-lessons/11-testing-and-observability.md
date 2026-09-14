# Lesson 11: Testing and observability

[Previous](10-memory-and-cooperative-io.md) · [Index](README.md) · [Next](12-signed-ota-updates.md)

## What you will learn

Design assertions around observable behavior, choose useful doubles, and
separate test success from operational evidence. Allow 120 minutes.

## Preparation and references

Use a host checkout and pytest. Read [contributing](../CONTRIBUTING.md) and
[the test plan](../nodus_test_plan.md). Inspect
[test_feature_services.py](../../tests/test_feature_services.py),
[test_mqtt_client_adapter.py](../../tests/test_mqtt_client_adapter.py),
[reboot_log.py](../../cpynodus_ii/core/reboot_log.py), and
[recovery_log.py](../../cpynodus_ii/core/recovery_log.py).
No live profile or board mutation is required.

## Walkthrough

A double replaces a boundary you cannot or should not use in a host test: a
sensor property, socket, clock, or filesystem root. Useful tests distinguish
outcomes that matter to a caller. Counting internal helper calls alone may
miss incorrect data or cleanup. A fake send can establish adapter accounting;
it cannot establish broker receipt or native socket health.

Nodus console events use date/time when the clock is valid and seconds before
clock synchronization. Numeric MQTT payload timestamps are a different format.
Clock synchronization can change the console time basis. Record this boundary
when aligning traces; do not subtract unlike timestamp formats. Interpreter
traceback continuation lines retain their ordinary diagnostic layout.

## Lab

1. Choose a feature test and an adapter test. For each, write: input, replaced
   boundary, externally meaningful assertion, and a defect it would miss.
2. Use the sensor failure exercise from Lesson 6 as a starter. Extend its test
   to assert that a failed read cannot expose a previous successful value as a
   fresh successful snapshot. Keep runtime source unchanged.
3. Demonstrate that the assertion matters: temporarily change the expected
   outcome in your exercise so it fails, capture the failure, then restore it
   and capture the passing result. Do not weaken the assertion to hide a defect.
4. Create an evidence matrix with these claims: calibrated value is correct;
   physical sensor communicates; MQTT packet reached the broker; a config edit
   survived a board reboot; a closed HTTP client releases native resources.
   Assign host, device, broker, and persistence evidence as appropriate.
5. Run the relevant focused tests and then `pytest tests` in the prepared course
   environment. Record exact commands and results, including any dependency or
   environment failures; a collection failure is not a firmware regression.

## Acceptance and troubleshooting

Submit the test diff, deliberate failure and restored pass, the evidence matrix,
and a redacted sample event annotation. Use a real capture only if available;
otherwise label an invented line illustrative. Include firmware revision and
hardware conditions with any actual device logs. Never include passwords,
private signing keys, or unredacted retained metadata in a class report.

If a test uses real time, look for injected monotonic values before adding
sleeps. If it touches a path, use a temporary host directory, never a device
mount. Read `testApparatus` routines before running them: they are hardware
support programs, not an automatic replacement for the host test suite.

## Reference answer and reflection

Host doubles can establish metric arithmetic and adapter decisions. Physical
communication needs the sensor/board; MQTT receipt needs a broker capture;
durable writes need persisted contents and a reload/reboot observation; native
socket release needs paced device requests correlated with serial evidence.
The same claim may need several kinds of evidence.

What is the narrowest test that detects your chosen failure? Which information
would another student need to reproduce your result without asking you questions?
