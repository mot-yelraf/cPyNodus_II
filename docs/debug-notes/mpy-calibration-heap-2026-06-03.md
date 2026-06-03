# MPY Calibration Heap Validation - 2026-06-03

## Context

This note records a targeted hardware validation on `co2-ykdvea` using compiled
`.mpy` firmware modules. The goal was narrow: determine whether reducing import
and bytecode load pressure gives the Nodus enough heap to complete a
Sensorius-driven `calibration/set` command.

This was not evaluated as a general deployment strategy. The operator loaded
compiled `.mpy` artifacts for `cpynodus_ii/app.py`, `cpynodus_ii/core/*.py`,
and `cpynodus_ii/features/*.py` onto the device and removed the corresponding
`.py` modules for the run.

## Build Artifacts Used

Artifacts were generated under `build/firmware/` with the local CircuitPython
9.2.8 `mpy-cross` compiler:

- `build/firmware/cpynodus_ii/app.mpy`
- `build/firmware/cpynodus_ii/core/*.mpy`
- `build/firmware/cpynodus_ii/features/*.mpy`

The compiled files use the CircuitPython `.mpy` header format and target MPY
v6.3, matching CircuitPython 9.2.8.

## Startup and Memory Observation

Compared with previous `.py` module startups on the same class of firmware:

- startup to NTP sync dropped from roughly 25 seconds to roughly 14 seconds;
- post-MQTT-connect free memory increased by about 5 kB to roughly 99 kB;
- periodic memory reports increased from roughly 90 kB free after GC to roughly
  96 kB free after GC.

Representative soak log before calibration:

```text
2026-06-03 12:48:12 memory phase=periodic_gc before=free_mem=91152 mem_alloc=321040 after=free_mem=96800 mem_alloc=315392 queues pub=0 sub=0 rx=0
2026-06-03 12:53:12 memory phase=periodic_gc before=free_mem=92704 mem_alloc=319488 after=free_mem=96800 mem_alloc=315392 queues pub=0 sub=0 rx=0
2026-06-03 12:58:12 memory phase=periodic_gc before=free_mem=84464 mem_alloc=327728 after=free_mem=96752 mem_alloc=315440 queues pub=0 sub=0 rx=0
2026-06-03 13:03:12 memory phase=periodic_gc before=free_mem=82864 mem_alloc=329328 after=free_mem=96752 mem_alloc=315440 queues pub=0 sub=0 rx=0
```

The soak showed stable health reports, MQTT connected, NTP synced, and empty
publish/subscribe/rx queues before the calibration attempt.

## Calibration Result

Sensorius then sent calibration updates to `co2-ykdvea`. The device reported
two successful volatile calibration applications:

```text
2026-06-03 13:06:08 mqtt command type=calibration phase=published topic=nodus/co2-ykdvea/calibration/set requested=offset_fast:applied published=3 persistence_mode=volatile errors=none
2026-06-03 13:06:11 mqtt command type=calibration phase=published topic=nodus/co2-ykdvea/calibration/set requested=offset_fast:applied published=3 persistence_mode=volatile errors=none
```

Sensorius displayed:

```text
Updated 2 device calibration value(s) for co2-ykdvea.
```

Post-command memory recovered after GC, though the pre-GC report showed the
expected transient command-path pressure. The after-GC free memory also settled
lower than before calibration: from roughly 96.7 kB before the command to
roughly 90.8 kB after the command.

```text
2026-06-03 13:08:12 memory phase=periodic_gc before=free_mem=47760 mem_alloc=364432 after=free_mem=90848 mem_alloc=321344 queues pub=0 sub=0 rx=0
2026-06-03 13:13:12 memory phase=periodic_gc before=free_mem=47568 mem_alloc=364624 after=free_mem=90848 mem_alloc=321344 queues pub=0 sub=0 rx=0
2026-06-03 13:18:12 memory phase=periodic_gc before=free_mem=55680 mem_alloc=356512 after=free_mem=90816 mem_alloc=321376 queues pub=0 sub=0 rx=0
```

The lower post-calibration memory level persisted through the soak before the
manual soft reboot, reaching roughly 90.3 kB after GC just before the restart:

```text
2026-06-03 15:28:12 memory phase=periodic_gc before=free_mem=40896 mem_alloc=371296 after=free_mem=90352 mem_alloc=321840 queues pub=0 sub=0 rx=0
```

## Warm Start Observation

After the calibration run, the operator interrupted `co2-ykdvea` with `Ctrl-C`
and then restarted with `Ctrl-D`. The known warm-start issue did not occur on
that attempt.

The serial log shows the interrupt and restart path:

```text
2026-06-03 15:31:44 runtime shutdown_prepare reason=finally marker=set
2026-06-03 15:31:44 mqtt disconnect phase=error errors=Socket not managed
2026-06-03 15:31:44 runtime services action=stop sensor=1 switch=1
2026-06-03 15:31:44 runtime network action=teardown result=1 cycle_radio=0
KeyboardInterrupt:
2026-06-03 15:31:47 runtime warm_start_cleanup phase=start
2026-06-03 15:31:48 runtime warm_start_cleanup phase=done disconnect=0 stop_station=1 stop_ap=1 cycle_radio=0 start_station=1
2026-06-03 15:31:54 mqtt connect_context attempt=1 recovery_phase=mqtt recovery_elapsed_s=0 wifi_ready=1 network_phase=ready ipv4=10.0.0.236 socket_source=direct free_mem=94688 mem_alloc=316224 queues pub=0 sub=0 rx=0 last_reason=none
2026-06-03 15:31:54 mqtt connect phase=connected broker=10.0.0.248 elapsed_s=0.1
2026-06-03 15:31:54 memory phase=post_mqtt_connect free_mem=98272 mem_alloc=312640
2026-06-03 15:31:59 mqtt sync source=main_loop phase=synced published=0 subscribed=4 op=subscribe topic=nodus/co2-ykdvea/logs/get retain=0 bytes=-1 pending=4 elapsed_ms=27 errors=none
2026-06-03 15:32:01 mqtt sync source=main_loop phase=synced published=0 subscribed=2 op=subscribe topic=nodus/S2-ykdvea/config/set retain=0 bytes=-1 pending=2 elapsed_ms=23 errors=none
2026-06-03 15:32:03 ntp phase=synced server=us.pool.ntp.org rtc=2026-06-03T15:32:03 errors=none
```

After the soft reboot, free memory returned to the higher compiled-module
baseline, roughly 96.3 kB, compared with roughly 90.3 kB before the reboot.
The serial startup log reported `post_mqtt_connect free_mem=98272`; the
post-start MQTT-visible data confirmed the device had resumed normal service.

The attached broker capture also shows MQTT-visible recovery after the restart:

```text
2026-06-03T15:31:45-0600 nodus/co2-ykdvea/status/heartbeat {"schema":"nodus-heartbeat/v1","status":"offline","timestamp":1780500704,"device_id":"co2-ykdvea"}
2026-06-03T15:31:55-0600 nodus/co2-ykdvea/meta {... "version":"v0.26.154.2", ...}
2026-06-03T15:31:56-0600 nodus/co2-ykdvea/status/heartbeat {"schema":"nodus-heartbeat/v1","status":"online","timestamp":1780500714,"device_id":"co2-ykdvea"}
2026-06-03T15:31:57-0600 nodus/co2-ykdvea/availability {"schema":"nodus-availability/v1","sensor_id":"co2-ykdvea","status":"online","timestamp":1780500714}
2026-06-03T15:31:58-0600 nodus/co2-ykdvea/meta/switch {...}
2026-06-03T15:32:00-0600 nodus/co2-ykdvea/data {...}
```

This was only tested once, like the calibration/set validation above, so it
should be treated as a noteworthy observation rather than a confirmed behavior
change.

## Interpretation

The `.mpy` build appears to provide enough extra heap headroom for the
Sensorius `calibration/set` command path to parse, apply runtime offsets, and
publish the expected command responses on `co2-ykdvea`.

Important limits of this finding:

- `persistence_mode=volatile` means this validates runtime application and MQTT
  response publication, not TOML persistence across reload.
- The post-command after-GC free memory settled around 90.8 kB instead of the
  pre-command 96.7 kB baseline, so additional soak after repeated commands is
  still useful.
- The soft reboot returned free memory to the higher compiled-module baseline,
  so the lower post-calibration level did not persist across restart in this
  single run.
- The clean `Ctrl-C` / `Ctrl-D` warm start was a single run only; repeat tests
  are needed before treating it as a warm-start mitigation.
- For formal MQTT validation, broker-visible `calibration/ack`,
  `calibration/result`, and `meta/patch` output should still be captured.

The practical conclusion for the current calibration investigation is that the
remaining command failure was plausibly heap-pressure related, and compiled
`.mpy` modules are a viable mitigation for testing Sensorius calibration
updates on constrained Pico2 W devices.
