# Retained metadata validation

This change targets CircuitPython 9.2.8 on Pico2 W and 10.2.1 on XIAO ESP32-S3.
Host pytest and MPY compilation are prerequisites, not on-device validation.
The operator owns deployment; do not write mounted CIRCUITPY volumes from the
agent session. See [the contract](./sensorius_contract.md#retained-metaconfig)
for the required Sensorius subscriber migration.

## Host checks

Run `python -m pytest tests` and compile with:

```sh
scripts/nodus_mpy.sh --target pico2w --no-stage-libs
scripts/nodus_mpy.sh --target xesp32s3 --no-stage-libs
```

`tests/test_metadata_snapshot.py` exercises saved zero/false values, missing
fields, legacy soil moisture names, backup-file recovery, ROFS metadata reads,
MQTT persistence followed by reply drain, coalescing, retry after build failure,
reconnect replay, split packet sizes, and saved switch configuration.

The dataclass regression loads the repository's actual CircuitPython shim and
removes class annotations to model the board's field discovery. It first
reproduces `TypeError: too many positional arguments`, then checks skipped,
published, and error refresh results using keyword arguments. It does not
execute CircuitPython or emulate its RF/socket/heap behavior.

## Operator hardware acceptance

Run on each board with the verified CircuitPython version. Use the existing
sensor/switch fixture and keep the serial capture active. Correlate boot
version/profile, free heap, MQTT queue progress, PUBACK, and recovery events
with a broker-side capture. A queued publish or local send success is not proof.

1. Start with saved nonzero altitude (for example 1719.0), zero offsets, false
   retain/override flags, and known saved calibration status/reference fields.
   Subscribe freshly to all three retained topics after startup:

   ```sh
   mosquitto_sub -h <broker> -v -W 30 \
     -t 'nodus/<device-id>/meta' \
     -t 'nodus/<device-id>/meta/config' \
     -t 'nodus/<device-id>/meta/switch'
   ```

   Omit the switch topic for sensor-only devices. Verify complete JSON and
   matching sensor identity/altitude in both snapshots. Verify physical pins
   are separate from channel IDs. Confirm ongoing sensor data and heartbeat
   after the extra startup publish. Check actual packet sizes, especially with
   long location/reference text, rather than relying on fixture sizes.
2. Stop the hub subscriber. Send one authorized scalar calibration/config edit
   with a unique message ID, or use the operator's config flow. Wait for ack,
   result, patch, then retained refresh. Start a *fresh* subscriber: it must
   recover the saved edit without the non-retained patch. Repeat with explicit
   zero altitude, a Time setting, a HomeAssistant setting, and a switch label
   or state. Inspect persisted files through the operator's normal workflow.
3. Repeat a command with the same message ID: no duplicate refresh should be
   requested. Repeat equivalent retained snapshots at the hub and verify no
   redundant shadow writes. Deliver older/partial metadata and verify omitted
   fields do not reset existing shadow values. These last two checks validate
   Sensorius behavior, which firmware host tests cannot establish.
4. Interrupt the connection while refreshed snapshots are queued, then restore
   it. Verify retained replay converges to saved values and queues drain;
   check for PUBACK timeouts, socket errors, reset loops, and heap regressions.
5. In ROFS, verify startup still reads saved calibration/status. A supported
   volatile edit must not advertise changed values as saved configuration.
   Restart and confirm retained metadata reflects the persisted values.
6. In AP/nodusweb, save a local edit, then return to an MQTT profile/restart
   through the operator's workflow. Confirm the next retained startup snapshot
   reflects the saved change. MQTT is intentionally absent in these web modes.
7. Check a combined sensor/switch device and a switch-only device. II has one
   sensor compatibility block; two-child calibration belongs to III and needs
   separate validation there.

Acceptance requires both broker-visible results and serial/heap evidence.
The repository changes and compiled artifacts alone are not a hardware pass.

## 2026-09-20 Pico2 W observation

The operator's `co2-pmoopn` capture on `v0.26.263.1` confirms broker-visible
identity/altitude, physical switch fields, and metadata refresh after the
08:56:06 S2 command. A separate fresh subscription to the advertised
`nodus/co2-pmoopn/meta/config` on broker `10.0.0.241` returned `retain=1` with
calibration offsets/status/reference fields, display arrays, Time, and Home
Assistant settings. Its absence from the pasted capture was not evidence of
missing retained data.

That capture also exposes a refresh defect: S2 published `ON`, but the following
`meta/switch` still said `state=false`. The refresh used boot configuration
instead of the current switch-service snapshot. `v0.26.263.2` passes the live
service to the deferred refresh; regression tests cover both OFF-to-ON and
ON-to-OFF while leaving boot configuration unchanged. The new version still
needs operator deployment and broker replay validation.

The 08:55:14 command preceded completed channel subscriptions at 08:55:22 in
`cu.usbmodem21233201.log`; lack of an acknowledgement for that early command is
consistent with non-retained delivery before subscription. The later command
has broker-visible ack/result/event/state/patch. No subscription-order change
was made.

## Low-depth refresh correction: `v0.26.263.3`

The same Pico2 W run later failed at 08:58:56 allocating a 1,390-byte MQTT
packet for a 1,362-byte metadata payload, with three publishes queued. The
subsequent free-heap reading does not prove contiguous allocation space was
available at failure. The final shutdown traceback is `KeyboardInterrupt`.

The correction bounds runtime refresh allocation rather than changing socket
ownership, startup subscription ordering, or recovery/reboot policy:

- The coordinator marks a dirty-topic mask; the live main loop builds after
  coordinator/command stacks return.
- Only one pending snapshot is built. GC brackets dictionary construction,
  compact JSON serialization, and UTF-8 encoding. The dictionary and JSON
  string are released before the encoded buffer reaches the MQTT packet path.
- The queued buffer is reused for size calculation and packet construction;
  the adapter no longer re-encodes this payload in its deeper send stack.
- The next topic waits until prior sends/subscriptions drain. A fresh edit
  re-dirties relevant topics, including ones already queued. Build failures
  preserve their bit; the adapter retains failed retained sends for retry.
- Metadata serialization is inside the adapter's existing error boundary, with
  collection before metadata serialization and before packet allocation.

Host tests assert single-buffer queueing, deferred construction, byte-buffer
reuse, mid-refresh edits, retry of a failed later topic, and serialization
failure handling. They do not measure CircuitPython fragmentation or establish
that every future allocation will succeed. Repeat the operator switch/config
edit sequence on both boards, observing broker replay and heap across multiple
refreshes. The initial startup batch remains the existing ordered path.
