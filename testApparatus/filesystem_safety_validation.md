# Filesystem safety hardware validation

Status: operator reports firmware deployed and verified on 2026-10-15; the
intermittent filesystem-loss failure remains under observation. Board, runtime,
profile, filesystem mode, duration, and results for each fault-injection step
below were not supplied; this is not a completed power-loss validation matrix.

Run on spare Pico2 W / CircuitPython 9.2.8 and XIAO ESP32-S3 Sense /
CircuitPython 10.2.1 devices. The operator owns deployment and mounted
CIRCUITPY writes. Start with complete firmware/configuration backups; deliberate
power cuts can damage the filesystem under test.

1. Capture serial, board identity, firmware version/profile, RW guard position,
   volume label, file hashes, and free space. Confirm USB storage is hidden in
   RWFS and visible after a hard boot into host-edit mode.
2. Exercise settings, calibration, display, location, switch label/state, and
   onboarding updates. For MQTT, require broker-visible correlated ack/result
   and refreshed metadata. Reboot and confirm saved values and unchanged topic
   and relay behavior. Use `calibration_test.py` and `rw_test_toml.py` as
   existing isolated checks where applicable. Record command latency, free heap,
   and any pystack error; compare against the prior firmware on the same board.
3. On a spare image, have the operator arrange missing and empty live TOML files
   with valid retained backups. Boot writable and verify the original identities,
   credentials, sensor selection, and switch configuration return before factory
   detection. In ROFS, confirm retained settings can load without filesystem writes.
   Remove all three forms (live, `.bak`, `.tmp`) for deliberate config deletion.
4. Repeat interrupted writes at different times during scalar config and
   onboarding updates. After each cut, inspect the entire volume and compare
   firmware hashes. Accept a complete old or new config; reject defaults replacing
   an available saved backup. Confirm cleared onboarding state never returns.
5. Generate enough isolated recovery/reboot events to trigger log trimming.
   Interrupt trimming; inspect the live log and retained backup on restart.
   Confirm log reads/writes stay bounded and recovery remains functional.
6. Perform a small signed MPY OTA. Record extra digest/sync time and heap.
   Cut power during staging, backup copy, live replacement, rollback, and first
   boot. Require complete originals or validated replacements; a truncated backup
   must never replace a healthy live file. Repeat a cut during rollback and verify
   another rollback attempt can still use its retained journal and backup.
7. Repeat configuration and OTA writes with nearly full storage. Failed writes
   must close handles and preserve recoverable live/backup files. Check sync errors
   remain visible and required radio/socket recovery still uses its intended reset.
8. Test normal network-loss recovery, startup AP fallback, persisted switch state,
   and halt using `halt_validation.md`. Require broker captures for MQTT behavior.
   Use paced sequential HTTP requests for onboarding/web checks.
9. If the drive becomes unnamed, disappears unexpectedly, or contains only
   `boot_out.txt`, preserve that file, serial safe-mode/reset output, USB/host disk
   diagnostics, and a disk image before repair, erasure, or reflash. Record power
   source/cable, recent outage, board/runtime version, and RW/RO mode.

Host fault injection verifies file-operation ordering and closure, not FAT-sector
atomicity, flash endurance, brownout behavior, or the cause of past filesystem loss.
