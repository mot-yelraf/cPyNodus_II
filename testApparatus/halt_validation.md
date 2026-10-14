# Halt hardware validation

Status: not run. Run separately on Pico2 W / CircuitPython 9.2.8 and
XIAO ESP32-S3 Sense / CircuitPython 10.2.1. The operator deploys files; agents
must not write to `/Volumes/CIRCUITPY`.

1. Capture serial output and confirm the branch firmware version/profile.
2. Require broker-visible retained device `meta.capabilities.halt = true`.
   Set two populated relays to different states. Subscribe at the broker to
   `nodus/<device_id>/config/#`, heartbeat, and channel availability.
3. Send a non-retained halt command as documented in `docs/mqtt.md`. Require
   broker-visible correlated acknowledgement and successful halted result,
   plus offline topics. Record lost deliveries rather than assuming success
   from serial or local publish counts.
4. Confirm serial `halt phase=halted filesystem_sync=ok relays=held`. Measure
   that relay output levels remain unchanged. Leave power applied longer than
   the recovery timeout (at least 16 minutes); require no sensor/automation
   changes, heartbeat publications, reconnect, reload, or hard reset.
5. After confirmation, remove power and inspect saved TOML and firmware files
   on the next edit-mode boot. Check the preceding scalar change was fully
   committed, with no incomplete `.tmp`. Power cycle into runtime and confirm
   normal operation; halt must not persist or replay.
6. Repeat after a config write, calibration save, and switch persistence.
   Confirm each complete file transaction precedes halt. Also send a mutation
   behind halt in the same incoming batch; it must not take effect.
7. In NodusWeb (station and AP), submit **Halt until power cycle**. Require the
   response attempt before listener shutdown, serial completion, held relay
   levels, and no recovery. Inspect button layout manually or with paced
   terminal Playwright when needed. No browser rendering has been verified yet.
8. After halt is accepted, interrupt broker connectivity. The device must halt
   locally despite failed reply delivery. Observe elapsed shutdown time; the
   send-attempt budget retains existing per-operation socket timeouts.
9. On both ROFS and RWFS, confirm no application writes follow filesystem sync.
   Host edit-mode writes require a separate host eject. A sync failure must
   report failure and must never be interpreted as safe-to-unplug confirmation.

This routine tests the shutdown mechanism. It does not establish the cause of
past filesystem loss or guarantee immunity to power/flash failures.
