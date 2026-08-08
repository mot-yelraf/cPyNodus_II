# cPyNodus_II

`cPyNodus_II` is CircuitPython firmware for the Nodus family of small Wi-Fi
microcontroller devices. Verified targets are Raspberry Pi Pico2 W (`pico2w`)
on CircuitPython `9.2.8` and Seeed Studio XIAO ESP32-S3 Sense (`xesp32s3`) on
CircuitPython `10.2.1`. Nodus devices support sensor-only, switch-only, and
combined sensor+switch configurations. They can be provisioned through AP
bootstrap, the local `nodusweb` UI/API, or the Sensorius Add Device flow. After
provisioning, MQTT-enabled profiles publish telemetry and subscribe to
control/config topics through a Sensorius, WeeWX, or Home Assistant MQTT broker.

Sensorius Automatio Instrumentorum, or Sensorius, is the companion system that monitors and manages deployed Nodus devices. Nodus can also publish Home Assistant MQTT discovery/config topics directly when `ACTIVE_PROFILE = "homeassistant"`.

## Who this is for

- Makers and students who want a real-world IoT firmware example.
- Contributors who want a small, readable CircuitPython codebase.
- Anyone building Nodus sensor or relay nodes on supported CircuitPython boards.
- Project Status: Pre-1.0. Interfaces and internal architecture may change.
- Security Note: Wi-Fi and MQTT passwords are obfuscated on write, not encrypted. Do not deploy in security-sensitive environments.

## Key features
Nodus is a headless IoT node with the following core responsibilities:

- Bring up Wi‑Fi in normal mode when valid credentials exist.
- Fall back to AP mode when credentials are missing or invalid.
- Provide AP bootstrap routes and a minimal `nodusweb` local UI/API.
- Support MQTT publishing, config, calibration, log retrieval, switch control, and OTA prepare.
- Process MQTT config, calibration, and switch mutations through shallow-stack
  handlers with one scalar update per command.
- Publish Sensorius metadata and optional Home Assistant MQTT discovery.
- Auto-detect supported sensors and factory-enabled switches on first boot.
- Maintain sensor data collection and publish loops.
- Provide recovery hooks for network, MQTT, sensor, and AP idle failures.
- Maintain bounded reboot and recovery logs when the filesystem is writable.
- Constrained-memory friendly web server and routes.
- Over-the-air update support using MQTT prepare plus chunked HTTP transfer.
  See [OTA](./ota.md).


## System Architecture
### Nodus using Sensorius MQTT Broker
```
                     +------------------------+        +------------------+
                     |      Sensorius Hub     |<------>|  Home Assistant  |
                     |  (FastAPI + MQTT + DB) |        |     (Optional)   |
                     +------------------------+        +------------------+
                         ^            ^          
                         |            |
                 +-------+            +-------+
                 |                            |
                 v                            v
         +---------------+            +----------------+
         | Nodus Sensor  |            | Nodus Switch   |
         |  (e.g. CO2)   |            |  IoT Relay     |
         +---------------+            +----------------+
                |                              |
        MQTT pub/sub                      MQTT pub/sub 
```
### Nodus using Home Assistant MQTT Broker
```
                     +------------------+
                     | Home Assistant   |
                     |     (HA)         |
                     +------------------+
                         ^           ^          
                         |           |
                 +-------+           +--------+
                 |                            |
                 v                            v
         +---------------+            +----------------+
         | Nodus Sensor  |            | Nodus Switch   |
         |  (e.g. CO2)   |            |  IoT Relay     |
         +---------------+            +----------------+
                |                              |
        MQTT pub/sub                      MQTT pub/sub 
```

## Hardware

- Raspberry Pi Pico2 W (`pico2w`, verified)
- Seeed Studio XIAO ESP32-S3 Sense (`xesp32s3`, verified)
- Optional I2C sensors
- Optional UART/Modbus soil sensor
- Optional relay switches

Firmware requirements:
- Pico2 W / `pico2w`: CircuitPython 9.2.8 (verified)
- XIAO ESP32-S3 Sense / `xesp32s3`: CircuitPython 10.2.1 (verified)

For factory XIAO setup, see
[XIAO ESP32-S3 CircuitPython Bring-Up](./xiao_esp32s3_circuitpython_bringup.md).

See `docs/pinout.md` for the Nodus wiring pinout.

## Quick start (device)

1. Install the matching CircuitPython build on the target board.
2. Copy this repo to the device filesystem (CIRCUITPY).
3. Use `docs/pinout.md` as guidance to connect sensors, switches, the RW enable pin, and factory reset input.
4. Reboot the device and allow about a minute for it to self-configure. On a
   clean deploy, Nodus creates `settings.toml` and the detected live sensor and
   switch TOML files from the deployed `boards/` templates.
5. Edit the relevant files for your Nodus:
   - `settings.toml` (configure Wi-Fi, profile, MQTT, Home Assistant, and time)
   - `sensor_i2c.toml`
   - `sensor_soil.toml`
   - `switch.toml` if the device includes switches

## Deployment script

Use `scripts/deploy_nodus.sh` to copy firmware files to a `CIRCUITPY` drive.

Examples:

- Direct local mount:
  - `scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY`
- Raspberry Pi host with a connected Nodus (direct to drive):
  - `scripts/deploy_nodus.sh --target pi@raspberrypi:/media/pi/CIRCUITPY`
- Raspberry Pi staging folder:
  - `scripts/deploy_nodus.sh --target pi@raspberrypi:/home/pi/cPyNodus_II-release --mode staging`
- Preview without writing:
  - `scripts/deploy_nodus.sh --target pi@raspberrypi:/media/pi/CIRCUITPY --dry-run`
- Sync runtime files only:
  - `scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content runtime`
- Sync Pico2 W runtime files with compiled firmware modules and 9.x libraries:
  - `scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content pico2w-mpy`
- Sync XIAO ESP32-S3 runtime files with compiled firmware modules and 10.x libraries:
  - `scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content xesp32s3-mpy`
- Build compiled package artifacts first:
  - `scripts/nodus_mpy.sh --target pico2w`
  - `scripts/nodus_mpy.sh --target xesp32s3`

Notes:

- The script excludes development files (`tests/`, `docs/`, `.git/`, caches, etc.).
- `--content` options are `full` (default), `runtime`, `pico2w-mpy`, or
  `xesp32s3-mpy`. `mpy` remains a compatibility alias for `pico2w-mpy`.
- `runtime` syncs `boot.py`, `safemode.py`, `code.py`, `dataclasses.py`, board TOML
  templates, and `cpynodus_ii/`. It does not manage CircuitPython libraries.
- Target MPY deploys require a complete, current
  `build/firmware/<target>/cpynodus_ii/` tree plus staged
  `build/firmware/<target>/lib/` dependencies and
  `build/firmware/<target>/BUILD_INFO` metadata. The metadata records the
  requested target, CircuitPython version, MPY ABI, compiler path/version, and
  staged library source. Deploy validates it before syncing so `pico2w-mpy`
  and `xesp32s3-mpy` cannot accidentally deploy the wrong firmware image.
- `scripts/nodus_mpy.sh` requires a compiler that reports the matching
  CircuitPython version and emits `mpy v6.3`: Pico2 W uses CircuitPython
  `9.2.8`, and XIAO ESP32-S3 uses CircuitPython `10.2.1`. Set
  `MPY_CROSS_PICO2W` or `MPY_CROSS_XESP32S3`, or pass `--compiler`, when the
  compiler is outside the standard `~/Projects/mcu_libs` layout.
- Target MPY deploys sync root startup files, board TOML templates, compiled
  `.mpy` files, and the target library tree. Before copying
  the compiled package, deploy removes matching `cpynodus_ii/*.py` files from
  the target so CircuitPython imports the `.mpy` modules.
- MPY deploy can be used on a factory CircuitPython board with no existing Nodus
  firmware. It installs the compiled Nodus package plus the root startup files,
  `boards/` TOML templates, and library dependencies needed for first boot.
- Active target TOML files such as `settings.toml`, `sensor_i2c.toml`,
  `sensor_soil.toml`, and `switch.toml` are not part of `runtime` or MPY
  deploy content and remain intact.
- On macOS, deploy sets `COPYFILE_DISABLE=1` and `COPY_EXTENDED_ATTRIBUTES_DISABLE=1` to prevent `._*` sidecar files on CIRCUITPY.
- In `drive` mode, the target path must contain `CIRCUITPY` (override with `--force`).
- `--mode` options are `auto` (default), `drive`, or `staging`.
- Use `--delete` if you want files removed from target when no longer present in this repo.
- `--delete` is supported only with `--content full`.
- Use `--prune-deprecated` to remove only the target paths listed in `scripts/deprecated_target_files.txt`.
- Deploy removes target `_reboot.log` and `_recovery.log` by default so old postmortem records do not survive firmware updates; use `--keep-reboot-log` to preserve them.

## OTA package tags

After devices are deployed and confirmed on the hardware-verified OTA baseline,
use the repo tag `OTA-Verified---baseline` as the starting point for tag-range
OTA packages. Future OTA releases should be tagged on committed firmware
history, then packaged with:

```text
python scripts/nodus_ota.py package \
  --from OTA-Verified---baseline \
  --to <release-tag> \
  --target <pico2w-or-xesp32s3> \
  --compiled-root build/firmware/<target> \
  --out build/ota/cpynodusii_<from-version>_to_<to-version>_<target> \
  --signing-key /secure/path/cpynodusii-ota-private.pem
```

See [OTA](./ota.md) for the full tag workflow, manifest expectations, and
public-key provisioning, signed MQTT-prepare, and HTTP-transfer push command.

## Boot Flow

1. `safemode.py` runs only after CircuitPython enters safe mode. It permits two
   automatic resets for core hard faults, while leaving other safe-mode causes
   stopped for operator inspection.
2. `boot.py` configures USB/FS access based on a guard pin during normal boots.
3. `code.py` disables runtime autoreload, loads `cpynodus_ii.app`, and records fatal tracebacks when possible.
4. Network logic chooses AP mode or normal mode:
   - **AP mode**: starts an AP SSID named `Nodus_Setup` password is `password` (default channel `6`, configurable with `Network.AP_CHANNEL`).
   After connecting to the AP, use `POST /itaot-init` for Sensorius bootstrap or browse to `http://192.168.4.1:8000/setup` for the lightweight local setup page.
   - **Normal mode**: connects to Wi‑Fi and starts profile-specific runtime
     services. `nodusweb` publishes mDNS immediately; MQTT profiles do not
     start mDNS.
5. The runtime starts its asynchronous sensor, MQTT, recovery, and memory-management loops.

## AP Mode (Factory / Recovery)

AP mode is used when:

- SSID or password is missing,
- SSID equals `Nodus_Setup`, or
- Wi‑Fi connection fails.

AP mode exposes `/itaot-init`, `/itaot-meta`, `/setup`, `/config`, `/current-data`, `/set-switch-state`, and `/restart` when enough memory is available. Sensorius provisioning uses `/itaot-init`; manual JSON posts to `/config` can persist supported settings and then `/restart` can reboot into normal mode.

## Web UI Memory Guards

Nodus uses intentional low-memory guards around the web UI to protect runtime stability on constrained CircuitPython heaps.

- Every HTML page request runs garbage collection before checking free heap.
- HTML pages require at least 10 KB free after collection.
- Configuration and automation code is imported only when its independent page is requested; it is not embedded in the initial status response.
- Guarded requests return `503 Service Unavailable` with a retry message. This is a protective response, not necessarily a crash or reboot condition.
- HTML responses use nonblocking accepted-client sockets. Failed response
  sends close that client immediately and are collected; two consecutive send
  timeouts restart only the HTTP listener. Bad client descriptors are isolated
  to the affected request; an unscoped bad descriptor restarts the listener.
- Successfully completed responses are also collected before the next request
  so closed Pico2 W client sockets do not remain allocated until periodic GC.
- Accepted client sockets remain nonblocking through final close so an
  abandoned browser request cannot hold the cooperative runtime.
- Manual pacing still matters on weaker devices. Repeated rapid page loads or heavy configuration actions can still push the heap into protection windows.

## Normal Mode

In normal mode the device:

- Connects to Wi‑Fi and configures socket artifacts.
- Publishes `<Network.HOSTNAME>.local` through mDNS for `nodusweb` and
  temporary OTA HTTP mode when supported by CircuitPython. MQTT profiles stay
  headless and do not start mDNS.
- Starts sensor data collection loop.
- Publishes sensor metrics on a fixed interval.
- Serves lightweight status/setup routes only when `ACTIVE_PROFILE = "nodusweb"`.

## Profiles

- `nodusweb` is the default local-only profile.
  - MQTT startup is skipped.
  - Periodic NTP sync is started after normal network bring-up.
  - The device can run without an MQTT broker.
  - In AP mode, bootstrap routes and the local setup route are available.
  - In normal mode, the local web UI/API can manage supported live and restart-required settings directly without Sensorius.
  - Switch devices can execute local sensor, time, timer, and AND/OR rules.
    The evaluator and editor exist only in this profile; Astral is unsupported.
- `sensorius` is the networked profile used by Sensorius for Nodus onboarding, management, monitoring and automation implementation.
  - Periodic NTP sync is started after normal network bring-up.
  - MQTT is started and switch control topics are subscribed when enabled.
  - Broker settings come from `[MQTT]`.
  - Sensor metrics and runtime metadata are published over MQTT.
  - In normal mode, the built-in webserver is intentionally not started in this profile.
- `weewx` is a networked MQTT profile using the shared `[MQTT]` connection settings.
  - MQTT is enabled.
  - In normal mode, the built-in webserver is intentionally not started in this profile.
  - The host-side integration runs one selected Nodus through the independent
    `weewx@nodus.service`, archive, and report. A separate passive watcher
    records retained metadata from WeeWX-profile devices without creating
    additional WeeWX instances. The integration also includes archive schema
    and units, a Nodus report skin, retained device identity, Skyfield-backed
    Sun/Moon position and phase cards, an always-on switch label/state/event
    panel, and optional confirmed switch
    automation. A bounded Sensorius-style LAN host UI configures sensor/switch
    locations, switch labels, device calibration changes, and AND/OR metric,
    timer, time/day, Astral, and switch-state automations, with sensor/switch
    information panes. See
    [WeeWX](./weewx.md).
  - Discovery-first installation can stage a sensor-family template without
    naming a device, then explicitly install the intended discovered device;
    see the [WeeWX installation runbook](./weewx_install.md).
  - Periodic NTP sync is started after normal network bring-up.
- `homeassistant` is a networked MQTT profile using the shared `[MQTT]` connection settings.
  - MQTT is enabled.
  - Home Assistant behavior is configured in `[HomeAssistant]`.
  - Periodic NTP sync is started after normal network bring-up.
  - In normal mode, the built-in webserver is intentionally not started in this profile.
  - Provision the device in `nodusweb`/AP mode first, then reboot into `homeassistant`.
  - Once the device is operating in `homeassistant`, changes are currently expected through `settings.toml` edits or by returning the device to provisioning mode.

## Sensor Auto-Detect

`Settings.bootstrap_factory_defaults()` probes I2C buses and the RS485 soil channels on a clean factory deploy. It creates only the detected live sensor TOML file and seeds display defaults, serial numbers, sensor IDs, switch channel IDs, and hostname. On later boots, existing sensor TOML files are treated as the source of truth; switch channels also require their configured enable pin to be grounded at boot.

## Boot-Time Factory Reset

- Pico2 W reserves `GP17` as the factory-reset input.
- Hold the board's configured reset pin LOW continuously for 5 seconds during
  boot to force `ACTIVE_PROFILE = "nodusweb"` in `settings.toml`, then reboot.
- The profile reset does not currently rewrite the rest of `settings.toml`, and
  it does not delete or recreate `sensor_i2c.toml`, `sensor_soil.toml`, or
  `switch.toml`.
- The XIAO ESP32-S3 profile does not assign a factory-reset pin by default.
- The input uses an internal pull-up, so the reset condition is an external
  pull to ground.

## Supported Sensor Types and Metrics

Nodus currently supports these sensor device types: `aht`, `apvpd_aht`, `apvpd`,
`aqi`, `avpd`, `co2`, `lux`, and `soil`.

These `DEVICE` values remain the logical Nodus sensor IDs used in topics and
payloads. Sensorius-facing metadata also reports the concrete hardware family
as `sensor.hardware` when known: `BME280`, `BME680`, `VEML7700`, `AHTx0`,
`SCD30`, or `SCD4x`. Soil sensors report the configured `SOIL_VARIANT`, for
example `canonical`, `soil_2in1`, `soil_4in1`, or `soil_7in1`.

### `aht` (AHT10/AHT20)

- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)

### `apvpd_aht` (dual AHT10/AHT20: ambient + plant)

- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)
- `Plant Temperature` (`°C`)
- `Plant Temperature_F` (`°F`)
- `Plant Rel-Humidity` (`%`)
- `Plant Humidity` (`g/m³`)
- `Plant VPD` (`kPa`)
- `Plant Dew Point` (`°C`)
- `Plant Dew Point_F` (`°F`)
- `Plant Dew Point Deficit` (`°C`)
- `Plant DewVPD Risk` (`%`)

### `apvpd` (dual BME280: ambient + plant)

- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)
- `Plant Temperature` (`°C`)
- `Plant Temperature_F` (`°F`)
- `Plant Rel-Humidity` (`%`)
- `Plant Humidity` (`g/m³`)
- `Plant VPD` (`kPa`)
- `Plant Baro-Pressure` (`hPa`)
- `Plant Dew Point` (`°C`)
- `Plant Dew Point_F` (`°F`)
- `Plant Dew Point Deficit` (`°C`)
- `Plant DewVPD Risk` (`%`)

### `aqi` (BME680)

- `Air Quality` (`AQI`)
- `Gas` (`Ω`)
- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)
- `Baro-Pressure` (`hPa`)

### `avpd` (single BME280)

- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)
- `Baro-Pressure` (`hPa`)

### `co2` (SCD30/SCD4x family)

- `CO2` (`ppm`)
- `Temperature` (`°C`)
- `Temperature_F` (`°F`)
- `Rel-Humidity` (`%`)
- `Humidity` (`g/m³`)
- `Ambient VPD` (`kPa`)
- `Dew Point` (`°C`)
- `Dew Point_F` (`°F`)
- `Dew Point Deficit` (`°C`)
- `DewVPD Risk` (`%`)

### `lux` (VEML7700)

- `Light Intensity` (`lux`)
- `Auto Light` (`lux`)
- `Estimated PPFD` (`µmol/m²/s`)
- `Visible Light Intensity` (`mol/m²/day`)

## Soil Sensor Metrics

HaliSense-compatible RS485 soil sensors are the best-tested soil devices because their register layout matches the default Nodus register map. Nodus reads the configured register map and omits metrics whose register read fails. For 4-in-1 and 7-in-1 soil profiles, Nodus keeps the configured map as primary but can use the observed alternate `pH@0x0007` and `EC@0x000C` registers when the primary pH register is not plausible and the alternate pH value is valid.

When one soil channel is active, Nodus publishes unprefixed metric names. When both RS485 channels are active, Nodus prefixes each metric with the channel name, for example `CH1 Soil Moisture` and `CH2 Soil pH`.

### `soil` (RS485 Modbus 2-in-1)

- `Soil Moisture` (`%`)
- `Soil Temp_C` (`°C`)
- `Soil Temp_F` (`°F`)
- `Soil Moisture Deficit` (`%`)
- `Soil Stress Index` (`%`)

### `soil` (RS485 Modbus 4-in-1)

- `Soil Moisture` (`%`)
- `Soil Temp_C` (`°C`)
- `Soil Temp_F` (`°F`)
- `Soil pH` (`pH`)
- `Soil EC` (`mS/cm`)
- `Soil Moisture Deficit` (`%`)
- `Soil Stress Index` (`%`)

### `soil` (RS485 Modbus 7-in-1)

- `Soil Moisture` (`%`)
- `Soil Temp_C` (`°C`)
- `Soil Temp_F` (`°F`)
- `Soil pH` (`pH`)
- `Soil EC` (`mS/cm`)
- `Soil Nitrogen` (`mg/kg`)
- `Soil Phosphorus` (`mg/kg`)
- `Soil Potassium` (`mg/kg`)
- `Soil Moisture Deficit` (`%`)
- `Soil Stress Index` (`%`)
- `Soil Fertility Index` (`%`)

## Soil Moisture Deficit

`Soil Moisture Deficit` is a Nodus-derived dryness metric that expresses how much water the soil is currently missing relative to the configured wet and dry thresholds.

It is a measure of current water shortfall, not the soil's inherent ability to retain water.

- `0%` means the soil is at or above the configured wet threshold
- `100%` means the soil is at or below the configured dry threshold
- values between `0%` and `100%` show where the current moisture sits within that wet-to-dry operating band
- higher values mean drier soil and greater watering need

This makes `Soil Moisture Deficit` easier to alert on than raw volumetric moisture alone, because the same percentage scale can be tuned for different media, sensor placements, or crop targets.

Nodus calculates `Soil Moisture Deficit` from the corrected soil moisture value using the thresholds in `sensor_soil.toml`:

```text
Soil Moisture Deficit = 100 * ((wet_threshold - corrected_soil_moisture) / (wet_threshold - dry_threshold))
```

The result is clamped to the range `0-100%`.

Default thresholds:

- `SPD_WET_THRESHOLD_PCT = 38.0`
- `SPD_DRY_THRESHOLD_PCT = 18.0`

Using the defaults:

- `38%` soil moisture or higher reports `Soil Moisture Deficit = 0%`
- `18%` soil moisture or lower reports `Soil Moisture Deficit = 100%`
- `28%` soil moisture reports `Soil Moisture Deficit = 50%`

If the wet threshold is not greater than the dry threshold, `Soil Moisture Deficit` is not reported.

## Soil Stress Index

`Soil Stress Index` is a Nodus-derived soil concern metric that combines `Soil Moisture Deficit` with root-zone temperature stress into a single normalized percentage.

- `0%` means low combined stress
- `100%` means high combined stress
- higher values mean the soil is drier, thermally less favorable, or both

By default, `Soil Stress Index` is a weighted blend of:

- `70%` `Soil Moisture Deficit`
- `30%` soil temperature stress

The temperature component uses these default bands:

- overall range: `15°C` to `30°C`
- too low: below `18°C`
- OK: `18°C` to `24°C`
- too high: above `24°C`

To avoid an abrupt step change, temperature stress ramps linearly and reaches full stress at the critical edges:

- low critical: `15°C`
- high critical: `30°C`

That means:

- `18-24°C` contributes `0%` temperature stress
- `<=15°C` contributes `100%` temperature stress
- `>=30°C` contributes `100%` temperature stress
- values between those points scale linearly

Nodus calculates `Soil Stress Index` as:

```text
soil_temp_stress = temperature-based stress from 0 to 100
Soil Stress Index = ((Soil Moisture Deficit * moisture_weight) + (soil_temp_stress * temp_weight)) / (moisture_weight + temp_weight)
```

With the default weights and temperature bands:

- `Soil Moisture Deficit = 50%` and `Soil Temp_C = 21°C` reports `Soil Stress Index = 35%`
- `Soil Moisture Deficit = 50%` and `Soil Temp_C = 27°C` reports `Soil Stress Index = 50%`
- `Soil Moisture Deficit = 80%` and `Soil Temp_C = 31°C` reports `Soil Stress Index = 86%`

If the temperature band configuration is invalid, `Soil Stress Index` is not reported.

## Soil Fertility Index

`Soil Fertility Index` is a Nodus-derived NPK score for 7-in-1 soil sensors. It combines the reported `Soil Nitrogen`, `Soil Phosphorus`, and `Soil Potassium` values into a single `0-100%` macronutrient sufficiency indicator.

It is a convenience metric for visualization, automation, and trend monitoring. It is not a laboratory-grade soil fertility analysis, and it does not account for pH, EC, organic matter, moisture, microbial activity, micronutrients, soil structure, or temperature.

Nodus normalizes each nutrient against the `[NPK]` targets in `sensor_soil.toml`:

```text
n_score = clamp(Soil Nitrogen / N_TARGET, 0.0, 1.0)
p_score = clamp(Soil Phosphorus / P_TARGET, 0.0, 1.0)
k_score = clamp(Soil Potassium / K_TARGET, 0.0, 1.0)
Soil Fertility Index = 100 * ((0.5 * min_score) + (0.5 * avg_score))
```

The result is rounded and clamped to `0-100%`. `Soil Fertility Index` is only reported when the active soil channel has `SOIL_VARIANT = "soil_7in1"`. If any N/P/K value is missing or any target is `0` or lower, `Soil Fertility Index` is not reported.

Default targets are in `mg/kg`, matching the 7-in-1 sensor's reported nutrient metrics. They are sensor-scale defaults based on healthy living-soil readings, not lab sufficiency thresholds:

- `N_TARGET = 20.0`
- `P_TARGET = 30.0`
- `K_TARGET = 70.0`

## DewVPD Risk Metric

`DewVPD Risk` is a Nodus-specific derived metric intended to summarize two related environmental concerns in one percentage:

- condensation / leaf-wetness risk, represented by how close dew point is to the measured air or leaf temperature
- plant-environment stress risk, represented by VPD being too low or too high

The metric is reported as a percentage:

- lower values mean a safer environment
- higher values mean a greater chance of undesirable microbial / mold-promoting conditions and/or VPD-driven plant stress

For dual-sensor plant devices, `Plant DewVPD Risk` uses the same algorithm but is calculated from the plant-side temperature and RH.

### Inputs

The calculation uses:

- dew point
- dewpoint deficit
- VPD

Definitions:

- `dewpoint deficit = temperature - dew_point`
- low dewpoint deficit means dew point is close to the measured surface/air temperature
- when dew point approaches the measured temperature, condensation risk rises sharply

### Design Intent

This metric is intentionally not just a mold metric and not just a VPD metric.

It is designed to:

- strongly penalize dew point being close to temperature
- penalize very low VPD because air with little drying force encourages condensation persistence
- treat a moderate VPD band as the safest range
- penalize very high VPD because it represents increasing plant stress even if condensation risk is lower

### Current Risk Model

The current implementation combines two normalized terms:

- `dew_risk`: derived from dewpoint deficit
- `vpd_risk`: derived from a two-sided VPD risk curve

Combined score:

```text
dewvpd_risk = 100 * (dew_weight * dew_risk + (1 - dew_weight) * vpd_risk)
```

Current defaults:

- `dew_weight = 0.65`
- dewpoint deficit risk band: `0.5°C` to `4.0°C`
- VPD low-risk target band: `0.8` to `1.2 kPa`
- low-VPD high-risk threshold: `<= 0.4 kPa`
- high-VPD high-risk threshold: `>= 2.0 kPa`

Interpretation of the two parts:

- Dew component:
  `0.5°C` or less depression is maximum dew risk.
  `4.0°C` or greater depression is zero dew-driven risk.
- VPD component:
  `0.8 - 1.2 kPa` contributes no VPD penalty.
  Risk rises as VPD drops below `0.8`.
  Risk also rises as VPD climbs above `1.2`.

### VPD Interpretation Used By The Metric

The current VPD-side assumptions are:

- `0.0 - 0.4`: no drying force
- `0.4 - 0.8`: low drying force
- `0.8 - 1.2`: moderate / preferred operating band
- `1.2 - 1.6`: high drying force
- `1.6 - 2.0`: very high drying force / increasing stress
- `> 2.0`: extreme drying force / maximum high-side stress penalty

### Practical Reading Guidance

- High risk with low VPD usually means the room is humid enough that drying force is weak and condensation is more likely to persist.
- High risk with high VPD usually means dew point may be acceptable, but the environment is becoming stressful to plants.
- Low risk generally requires both:
  a healthy dewpoint deficit and VPD near the target band.

### Example Comparisons

At the same dewpoint deficit:

- `VPD = 1.0 kPa` produces the lowest VPD-side penalty
- `VPD = 0.4 kPa` produces a strong low-drying-force penalty
- `VPD = 1.6 kPa` produces a moderate high-stress penalty

So two rooms with the same dew point spacing can still receive different `DewVPD Risk` values if one is too wet and the other is too dry.

## Switch Channels (S1/S2)

Nodus supports up to two switch channels. Pico2 W defaults are
`SWITCH_1_ENABLE_PIN=GP5`, `SWITCH_1_PIN=GP28`,
`SWITCH_2_ENABLE_PIN=GP10`, and `SWITCH_2_PIN=GP21`. XIAO ESP32-S3 defaults
are `SWITCH_1_ENABLE_PIN=D0`, `SWITCH_1_PIN=D1`,
`SWITCH_2_ENABLE_PIN=D2`, and `SWITCH_2_PIN=D3`.

`switch.toml` and at least one populated, grounded `SWITCH_N_ENABLE_PIN` are
the primary gate for switch-enabled operation on normal boots.

- During factory bootstrap, when no live TOML files exist yet, Nodus probes the switch enable pins and creates/populates `switch.toml` from the detected board's `switch.toml.def` for grounded channels.
- On later normal boots, Nodus expects `switch.toml` to already exist for switch-enabled devices.
- If `switch.toml` exists but a channel has no `SWITCH_N_ENABLE_PIN`, or its
  configured enable pin is not grounded, that channel is ignored. If no
  channels pass that check, Nodus treats the device as not switch-enabled.
- If only one switch is installed, only that channel is enabled/populated in `switch.toml`.

Under MQTT profiles, automations are implemented by Sensorius, WeeWX, or Home
Assistant and commands use each channel's MQTT `config/set` topic. Under
`nodusweb`, switch devices can instead execute local Sensorius-compatible rules
from `automations.toml`, excluding Astral conditions. Local rules use the latest
normal NodusWeb sensor sample and direct switch service without MQTT. See
[NodusWeb Switch Automations](automations.md). Every successful state change
persists `SWITCH_#_LAST_STATE` when the filesystem is writable.

## Recovery & Resilience

- **Wi-Fi outage policy**: when station Wi‑Fi drops, Nodus pauses MQTT reconnect attempts and spends up to 15 minutes retrying SSID reassociation before soft rebooting.
- **MQTT outage policy**: when Wi-Fi is still up but MQTT is unhealthy, Nodus
  keeps recovery active until startup publishes and subscriptions drain; MQTT
  CONNECT alone is not considered recovered. Slow startup publishes, socket
  progress failures, CONNECT/SUBACK timeouts, and related allocation failures
  share one pre-operational recovery episode. Recovery escalates through MQTT
  rebuild, station/radio reset, up to two NVM-counted warm reloads, and then
  hard reset. Only a fully operational generation or true power-on clears the
  warm-attempt counter.
- **AP recovery policy**: if startup cannot join the configured station network, Nodus falls back into AP recovery mode and soft reboots again after 10 minutes of idle AP uptime.
- **Restart policy**: `nodusweb` can use soft reload for ordinary runtime restarts; MQTT profiles and persistent recovery faults use hard reset paths when needed to clear board radio/socket state, especially on Pico2 W.
- **Recovery diagnostics**: when the filesystem is writable, formal recovery phase changes and recovery actions are appended to `/_recovery.log` with timestamp, firmware version, and device ID headers. The file is capped at 10 KB for USB-powered postmortems.

## Web Server

- Lightweight `adafruit_httpserver` based server.
- Request reads and response writes are bounded so incomplete HTTP clients and
  HTTPS/TLS probes cannot stall the cooperative runtime loop.
- AP mode routes are intentionally minimal to reduce memory pressure.
- Normal-mode routes are exposed only in `nodusweb`.
- Route set: `/`, `/current-data`, `/setup`, `/calibration`, `/info`, `/config`, `/set-switch-state`, and `/restart` when enabled. NodusWeb switch devices also expose `/switch-setup`, `/automations-ui`, and the automation JSON API. `/itaot-init` and `/itaot-meta` are AP-bootstrap routes.
- In station mode, mDNS publishes `<Network.HOSTNAME>.local` only for
  `nodusweb` and temporary OTA HTTP mode. MQTT profiles remain headless and do
  not start mDNS.

## Known Constraints / Notes

- Nodusweb-profile NTP sync is gated on DNS readiness. If DNS ping fails, NTP is skipped to avoid long blocking failures (NTP timeouts can stall coroutines).
- AP mode uses the configured fallback SSID/password for provisioning and defaults to Wi-Fi channel 6 unless `Network.AP_CHANNEL` overrides it.
- The system assumes a constrained heap; many routes and handlers are intentionally minimal to avoid memory fragmentation.

## MQTT + Home Assistant

- MQTT broker can be Sensorius or Home Assistant.
- TLS is enabled when configured or when broker port is 8883.
- Home Assistant discovery is published under `[HomeAssistant].DISCOVERY_PREFIX` when `ACTIVE_PROFILE = "homeassistant"`.
- Shared MQTT topics use `[MQTT].BASE_TOPIC`, defaulting to `nodus`.
- Switch control is handled via channel `config/set` topics; events and state are published to `/event` and `/state`.
- MQTT connects to broker IP literals only. Startup and MQTT recovery rebuilds
  resolve configured `MQTT.BROKER` when possible, use the first unique resolved
  address as the runtime MQTT target, and do not open extra broker verification
  sockets. The normal MQTT preflight/connect path remains the reachability
  check. On RWFS, after MQTT connects successfully, Nodus makes a best-effort
  scalar TOML update for `MQTT.BROKER_IP`. If resolution fails, Nodus keeps any
  existing `BROKER_IP` target.
- Device mDNS does not change MQTT broker target selection. `MQTT.BROKER`
  remains the canonical broker hostname, while `MQTT.BROKER_IP` is the last
  resolved/proven IP target.
- Pico 2 W CircuitPython hostname resolution may return only one address for a
  multi-interface broker host. Current firmware does not keep a configured
  alternate broker target; stale `MQTT.BROKER_IP_ALT` keys are ignored.
- Retained `nodus/<device_id>/meta` includes the current runtime Nodus station
  IPv4 as `network.ipv4addr` when available. This value is not persisted in
  `settings.toml` and can change after DHCP lease changes or reconnects.
- Implemented Home Assistant corner case:
  - when `ACTIVE_PROFILE=homeassistant` or `ACTIVE_PROFILE=weewx`, Nodus skips the normal-mode webserver in both ROFS and RWFS
  - this policy exists because these networked MQTT-only profiles are intended to run without the normal local web UI path
  - AP/nodusweb mode remains the supported provisioning path before switching into either profile

## Project Layout

- `boot.py`: board-specific filesystem/USB guard and startup-mode setup
- `safemode.py`: bounded automatic restart hook for CircuitPython core hard faults
- `code.py`: CircuitPython entrypoint and fatal traceback/reload wrapper
- `cpynodus_ii/app.py`: main async runtime orchestration, recovery, startup, and steady state
- `cpynodus_ii/core/`: board profiles, settings, config models, network, MQTT client adapter, NTP, recovery, reboot logs
- `cpynodus_ii/features/`: sensor/switch services, publish cycles, command intake, web handlers, log transfer, derived metrics
- `cpynodus_ii/hardware/`: CircuitPython hardware adapters for I2C, UART/RS485, and switch GPIO
- `cpynodus_ii/ota/`: MQTT prepare state and temporary HTTP-only OTA mode
- `scripts/`: deploy, OTA package/push, and MQTT log retrieval tools
- `tests/`: host-side pytest characterization and unit coverage

## Documentation

- `docs/user_guide.md`: AP setup, Sensorius onboarding, NodusWeb pages, and
  profile expectations
- `docs/architecture.md`: boot flow and task model
- `docs/configuration.md`: configuration files and keys
- `docs/onboarding.md`: AP provisioning behavior
- `docs/mqtt.md`: topics and Home Assistant notes
- `docs/weewx_install.md`: step-by-step WeeWX installation and replacement
- `docs/weewx.md`: detailed WeeWX integration reference
- `docs/pinout.md`: Nodus board pin mappings
- `docs/extending.md`: adding sensors or switches
- `docs/debug-notes/`: dated investigation logs retained as archival context,
  not the current runtime contract

## Development Notes

- Use the board-specific guard pin (`GP14` on Pico2 W, `D8` on XIAO ESP32-S3)
  to control whether the filesystem is R/W for the app.
- Keep web routes small; heavy handlers can destabilize startup on constrained devices.
- Add Device flow uses `POST /itaot-init`, then MQTT onboarding topics (`nodus/<device_id>/onboard/hello`, `config/set`, `config/ack`, `config/result`) as the authoritative configuration path.
- Nodus TOML files are the source of truth for accepted config. Sensorius should use retained `nodus/<device_id>/meta` as the compact startup/reconnect snapshot, retained `nodus/<device_id>/meta/switch` as the switch control-topic map when switch channels are present, then consume `nodus/<device_id>/meta/patch` for accepted steady-state config deltas.
- Accepted runtime `Time.*` config writes request a fresh NTP sync after MQTT
  command responses and queued publishes drain.
- `GET /itaot-meta` remains available as optional, on-demand UI metadata fallback; it is not required for onboarding success.

## Testing (manual)

- Boot with missing SSID -> AP mode is reachable.
- `POST /itaot-init` in AP mode -> device reboots and joins Wi-Fi.
- Sensor data publishes at the configured interval.
- Switch commands are honored and persisted.
- Temporary Wi-Fi loss suppresses MQTT publishes until Wi-Fi and MQTT both recover.
- Extended Wi-Fi loss eventually soft reboots into AP recovery mode.
- Extended AP recovery uptime soft reboots again after 10 minutes.
