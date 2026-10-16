# Switch synchronization validation

Host tests cannot prove GPIO correctness, CircuitPython stack headroom, broker
delivery, or Sensorius ingest behavior. Run these checks on Pico2 W with
CircuitPython 9.2.8 and XIAO ESP32-S3 with CircuitPython 10.2.1 after operator
deployment. Do not copy files to `/Volumes/CIRCUITPY` from an agent session.

1. Record firmware version, board, filesystem mode, profile, device/channel IDs,
   and initial safe output states. Capture serial and broker-visible device
   `meta/#`, channel `config/#`, `event`, and `state` traffic. Select relay loads
   that can safely change state.
2. In a Sensorius MQTT profile, issue one non-retained JSON channel command with
   a fresh `message_id`. Check correlated ACK/result/event/patch and retained
   state. The result must report `applied=true`, `persistence_mode=persisted`,
   and empty `persistence_errors` on a successful writable filesystem. Inspect
   `/switch.toml`, runtime metadata, physical output, and Sensorius separately.
3. Repeat in read-only mode. Require `applied=true`, `persistence_mode=volatile`,
   and a persistence error; verify the output changes and Sensorius confirms it
   without claiming the device file was saved. Serial `published=1` alone is
   insufficient. Confirm serial logs contain no unhandled stack/memory errors.
4. Use two channels with opposite states. Alternate commands, then change a
   harmless label to request metadata refresh. Verify repeated retained
   `meta/switch` snapshots preserve the live states and independent labels.
   Re-deliver the same command/action reports and confirm Sensorius records a
   transition once; ACK/result alone must not create a displayed transition.
5. Interrupt MQTT, let the existing recovery flow reconnect, and verify current
   retained state/metadata and command subscriptions. No intermediate switch
   events should be synthesized. After a durable change, reboot and inspect
   the saved start state. After a volatile change, expect the saved start state
   to return on reboot.
6. In `nodusweb`, use the existing paced HTTP/manual routines in
   [web_services_validation.md](web_services_validation.md). Check the HTTP
   `success`, `state`, `message_id`, `persistence_mode`, and
   `persistence_errors`, device file, runtime UI, and physical output. Verify
   the five-second guard and automation ownership remain effective. `nodusweb`
   has no MQTT connection, so do not expect live broker reports in this profile.
   A persisted web change becomes visible when the operator later selects an
   MQTT profile and its startup snapshots arrive.

Deferred local event/state/patch publication with a supplied connected MQTT
transport is host-tested. Current production profiles do not run web and MQTT
together; testing that conditional branch on hardware requires an explicitly
approved combined-profile harness, not a change to the normal profile policy.
Record that limitation instead of claiming the web-to-broker path was exercised.

## Pico2 W toggle regression comparison (2026-10-16)

Candidate firmware `v0.26.289.5` removes per-command reconstruction of channel,
switch, and runtime configuration objects. It updates the mutable channel's
last state only after successful output application. The switch-helper import
is now inside the MQTT command's memory/stack guard. Topics, publication count,
persistence ordering, and recovery policy are unchanged by this correction.
Host fault injection covers reconstruction pressure and helper-import failures;
it does not reproduce or explain the native socket failure.

The observed `v0.26.289.4` capture confirmed OFF, then showed an unconfirmed ON
request followed by a slow availability send and native-socket-poison reset.
The initiating cause remains unconfirmed.

Operator validation of `v0.26.289.5` on Pico2 W/CircuitPython 9.2.8, profile
`weewx`, RWFS, confirmed four alternating ON/OFF commands from the Sensorius
web UI between 15:11:15 and 15:16:56 MDT on 2026-10-16. All four command IDs
had broker-visible ACK, successful result with `persistence_mode=persisted`
and no persistence errors, matching event/state/patch, and updated retained
switch metadata. Heartbeat, sensor data, and availability continued. Serial
captured the candidate boot and no subsequent stalls or recovery through
15:14:34; its available tail did not cover the final toggle. The operator
accepted this repeated-toggle behavior as verified; the device continues to
soak. This establishes the observed recovery of toggle behavior, not a proven
root cause or completed long-soak validation.

After operator deployment, compare a recorded known-working artifact with the
candidate on the same Pico2 W/CircuitPython 9.2.8, broker, profile, and writable
configuration. Record artifact provenance as well as version strings. With an
operator-approved safe load, send ten alternating ON/OFF commands, each with a
fresh ID and at least 15 seconds between commands. Wait for each confirmation;
stop on a missing confirmation or reset and record the elapsed time. Capture
device metadata, heartbeat/data, and all channel replies/events/state alongside
serial output. Leave capture running through two subsequent availability cycles
after the final command. Compare command counts, durations, persistence results,
deferred metadata, recovery events, and physical output. Repeat after a soak;
record its duration. Do not infer a fix from host tests or one successful toggle.
