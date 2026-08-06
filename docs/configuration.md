# Configuration

## WeeWX host system settings

The host-side WeeWX integration keeps its system UI preferences in
`/var/lib/weewx/nodus_system.toml`. This is not a CircuitPython
`settings.toml`, sensor file, or switch file. It contains the Nodus system
title, online timeout, and automatic-provisioning gate. Broker, station,
database, service, and dashboard details remain sourced from the root-managed
WeeWX manager configuration and are displayed read-only in the system pane.
The System Settings view groups these values into WeeWX Preferences, Station &
MQTT, and WeeWX Runtime & Files sections. The host installer supports
`--discovery-only --family FAMILY`, which stages a family-bounded managed
template without selecting a device. With automatic provisioning disabled, an
operator can then select the intended retained-metadata device from System
Settings > Install Device. Automatic provisioning proceeds only when exactly
one discovered device matches the managed family.

Live configuration is stored as TOML files at the project root. Defaults are
provided as `.def` templates under `boards/`.

## Files

- `settings.toml`: network, profile, MQTT, Home Assistant, and time
- `sensor_i2c.toml`: I2C sensors
- `sensor_soil.toml`: UART/Modbus soil sensor, including optional CH1/CH2 RS485 soil channels
- `switch.toml`: switch/relay configuration

For `apvpd`, `sensor_i2c.toml` carries two BME280 definitions:

- `[I2Cbus]`: ambient sensor
- `[I2Cbus.Plant]`: plant sensor

Both may use the default BME280 address `118` (`0x76`) because they live on separate buses.

For `apvpd_aht`, `sensor_i2c.toml` uses the same two-bus layout for an ambient
and plant AHT10/AHT20 pair:

- `[I2Cbus]`: ambient sensor
- `[I2Cbus.Plant]`: plant sensor

Both may use the default AHTx0 address `56` (`0x38`) because they live on
separate buses.

Supported I2C `DEVICE` values:

- `aht`: AHT10/AHT20 temperature and relative humidity
- `apvpd_aht`: dual AHT10/AHT20 ambient and plant temperature/RH
- `apvpd`: dual BME280 ambient and plant temperature/RH/pressure
- `aqi`: BME680 air quality
- `avpd`: BME280 ambient temperature/RH/pressure
- `co2`: SCD30/SCD4x CO2
- `lux`: VEML7700 light

The `DEVICE` value remains the logical Nodus sensor ID used in topics and
payloads. MQTT retained `meta` and `/itaot-meta` also report the concrete
hardware family as `sensor.hardware` when known, for example `BME280`,
`BME680`, `VEML7700`, `AHTx0`, `SCD30`, or `SCD4x`. Soil sensors report the
configured `SOIL_VARIANT`, for example `canonical`, `soil_2in1`, `soil_4in1`,
or `soil_7in1`.

For single I2C sensors, `[I2Cbus]` is the preferred runtime bus. If the
configured bus cannot be opened, or the driver reports no sensor at the
configured address, Nodus tries the other board-profile I2C bus with the same
address for that boot. The fallback is volatile and does not rewrite
`sensor_i2c.toml`; update `I2C_BUS`, `I2C_SCL`, and `I2C_SDA` when the wiring
is intentionally moved. Dual `apvpd` and `apvpd_aht` devices keep explicit
`[I2Cbus]` and `[I2Cbus.Plant]` bus assignments and do not use this fallback.

## Templates

Copy the matching deployed templates and edit the live root files:

- `boards/settings.toml.def` -> `settings.toml`
- `boards/pico2w/templates/sensor_i2c.toml.def` -> `sensor_i2c.toml`
- `boards/pico2w/templates/sensor_soil.toml.def` -> `sensor_soil.toml`
- `boards/pico2w/templates/switch.toml.def` -> `switch.toml`

For XIAO ESP32-S3, use the matching `boards/xesp32s3/templates/` files.

On a clean factory deploy, Nodus uses the deployed `*.toml.def` files as
first-boot templates. `settings.toml.def` is shared under `boards/`; sensor and
switch templates live under `boards/<target>/templates/`, currently `pico2w`
and `xesp32s3`. Nodus prefers the detected board's templates, then the shared
`boards/` template, then any legacy root template left from older deployments.
It creates `settings.toml` and then creates only the live sensor and switch TOML
files needed for the detected hardware.

## Switch bootstrap behavior

- `switch.toml` plus at least one populated and grounded `SWITCH_N_ENABLE_PIN`
  is the normal-runtime switch gate.
- On a true factory bootstrap, Nodus detects grounded switch enable pins and creates/populates `switch.toml`.
- On later boots, if `switch.toml` is absent, grounded enable pins alone do not
  make the device switch-enabled.
- If `switch.toml` exists but no channel has a populated and grounded
  `SWITCH_N_ENABLE_PIN`, the runtime treats the device as not switch-enabled.

## Factory reset behavior

- Pico2 W reserves `GP17` as the factory-reset input.
- Hold the board's configured reset pin LOW continuously for 5 seconds during
  boot to force `ACTIVE_PROFILE = "nodusweb"` in `settings.toml`, then reboot.
- This reset currently affects only `[Profile].ACTIVE_PROFILE` in
  `settings.toml`. Sensor and switch TOML files are not deleted or regenerated
  by reset-pin hold alone.
- The XIAO ESP32-S3 profile does not assign a factory-reset pin by default.
- The input uses an internal pull-up, so the reset condition is an external
  pull to ground.

## Profiles

The active runtime profile is configured in `settings.toml` under `[Profile]`:

- `ACTIVE_PROFILE = "sensorius"`
  - MQTT-enabled Sensorius profile used by the AP bootstrap and MQTT onboarding flow
  - broker settings come from the shared `[MQTT]` section
  - NTP sync enabled after network bring-up
  - normal-mode webserver disabled by design
  - AP mode remains the primary factory/bootstrap path for provisioning credentials

- `ACTIVE_PROFILE = "nodusweb"`
  - default local-only behavior
  - MQTT disabled
  - NTP sync enabled after network bring-up
  - local switch automation editor/evaluator available when a physical switch
    channel is enabled; Astral rules are not supported
- `ACTIVE_PROFILE = "weewx"`
  - networked MQTT behavior using the shared `[MQTT]` connection settings
  - MQTT enabled
  - normal-mode webserver disabled by design
  - host installation and MQTTSubscribe mapping are documented in
    [WeeWX](./weewx.md)
  - NTP sync enabled after network bring-up
- `ACTIVE_PROFILE = "homeassistant"`
  - networked MQTT behavior using the shared `[MQTT]` connection settings
  - MQTT enabled
  - Home Assistant integration settings come from `[HomeAssistant]`
  - normal-mode webserver disabled by design
  - provision in `nodusweb`/AP mode first, then reboot into `homeassistant`
  - NTP sync enabled after network bring-up

## MQTT section

The shared MQTT connection is configured in `[MQTT]`:

- `BROKER`
- `BROKER_IP`
- `PORT`
- `USE_TLS`
- `BASE_TOPIC`
- `USERNAME`
- `PASSWORD`

`BROKER` is the canonical broker hostname from Sensorius. MQTT connections use
IP literals only. During startup and MQTT recovery rebuilds, Nodus attempts to
resolve `BROKER`; on success it stores the first unique resolved address as
`BROKER_IP` for the current boot. Broker refresh does not open extra TCP
verification sockets before the normal MQTT path. On RWFS, after MQTT connects
successfully, Nodus makes a best-effort scalar TOML update for the runtime
`BROKER_IP` value. Persistent `BROKER_IP` can also come from provisioning,
onboarding, explicit config writes, or a prior successful broker refresh. If
resolution fails, Nodus keeps any existing `BROKER_IP` value as the MiniMQTT
target.

On the Pico 2 W CircuitPython runtime, `socketpool.getaddrinfo()` may expose
only one resolved address for a hostname, even when the broker host advertises
multiple interfaces over mDNS. Nodus uses the resolver-provided address as the
source of truth for `BROKER_IP`; it does not maintain an alternate broker IP
target. Existing deployed `BROKER_IP_ALT` keys are ignored by current runtime
code.

The same settings rewrite obfuscates any plaintext passwords that were manually
entered in `settings.toml`.

The Nodus device's current station IPv4 address is not stored in
`settings.toml`. It is assigned at runtime by the network stack and published
in retained MQTT `meta` as `network.ipv4addr` when available. Treat that value
as volatile; it can change after DHCP lease changes, reconnects, or network
changes.

## Device hostname and mDNS

`Network.HOSTNAME` is the Nodus device hostname. In station mode, Nodus can
start a small mDNS server so the device answers at `<Network.HOSTNAME>.local`
on networks that pass multicast DNS. This is for operator access, diagnostics,
and HTTP discovery; it is not used as the MQTT broker connection target.

When `ACTIVE_PROFILE = "nodusweb"`, Nodus also advertises `_http._tcp` on
`Network.HTTPPORT` immediately after Wi-Fi is ready so Bonjour/mDNS browsers can
discover the web UI. Temporary OTA HTTP mode also starts mDNS for the transfer
window. MQTT profiles do not start mDNS in normal runtime; they stay headless
and keep MQTT startup/recovery focused on broker IP literals. Use
`ping <hostname>.local` or `curl http://<hostname>.local:8000` only when an
HTTP runtime is active, such as `nodusweb` or temporary OTA mode.

## Home Assistant section

Home Assistant integration is configured in `[HomeAssistant]` and is active
only when `ACTIVE_PROFILE = "homeassistant"`.

Current runtime behavior:

- `DISCOVERY_PREFIX` controls the Home Assistant discovery topic prefix.
- `PUBLISH_DISCOVERY_RETAIN` controls whether discovery config messages are
  retained.
- Sensor and switch state topics still use the shared `[MQTT].BASE_TOPIC`.
- `BASE_TOPIC`, `PUBLISH_STATE_RETAIN`, and `PUBLISH_LEGACY_SENSOR_TOPIC` are
  loaded and persisted for compatibility, but the current publish helpers do
  not use them to change topic layout or state retain behavior.

## Host MQTT Log Retrieval Tool

The host-side `scripts/nodus_getlogs.py` tool can request bounded Nodus runtime
logs over MQTT and save the completed file locally. Copy
`scripts/nodus_getlogs.toml.def` to `scripts/nodus_getlogs.toml` and set:

- `[mqtt]`: `broker`, `port`, `base_topic`, optional TLS, username, and password
- `[transfer]`: `storage_dir`, default `filenames`, `chunk_size`, and `timeout_s`

The tool writes successful transfers under `storage_dir/<device_id>/`.
Use `scripts/nodus_log_analyze.py` to summarize transferred `_recovery.log`
and `_reboot.log` files by event type, recovery reason, topic, broker, phase
transition, and reboot reason.

## Local setup UI

The built-in `nodusweb` UI uses independent pages so the initial `/` response
contains only status and switch controls. Setup, calibration, switch settings,
automation editing, and device information are generated only when their
navigation link is requested. The UI is available in normal mode only for
`ACTIVE_PROFILE = "nodusweb"`; AP recovery continues to expose the setup
surface, while MQTT profiles remain headless.

The pages use a responsive, centered NodusWeb sidebar and card layout inspired
by the host-side Nodus interfaces. On narrow screens the sidebar becomes a
compact horizontal navigation row. This styling does not add sensor history,
graphs, statistics, external assets, or a single-page application; each
surface keeps its independent constrained-memory response.

Every NodusWeb page embeds the same blue/green Nodus `N` SVG favicon used by
the WeeWX web surfaces, without adding a favicon route or HTTP request.

- `Status`: the last successfully gathered sensor sample, its RTC timestamp,
  and a switch card with state controls and local automation ownership. A
  waiting or failed read keeps the prior sample; no history buffer is allocated.
- `Setup`: network, time, profile/MQTT, sensor location, and display settings
- `Switch Settings`: two-column switch controls with the immediate state toggle
  before a shortened editable label field
- `Automations`: NodusWeb-local rules for enabled switch devices
- `Calibration`: device-appropriate calibration fields
- `Nodus Info`: current network, firmware, sensor, and switch identity
  plus the number of times the current boot entered Wi-Fi recovery

Wi-Fi and MQTT passwords use password inputs. The Pico2 W UI does not allocate sensor history and does not expose
history graphs, min/average/max statistics, stored-data summaries, or data
export.

HTML rendering collects garbage first and admits each page only with at least
10 KB free heap. A request below that floor returns `503 Service Unavailable`
with a retry message.

Each page builds only the view data it renders, and HTML is encoded before the
response object is returned so the sender does not retain both a full HTML
string and a second encoded copy. A timed-out response triggers immediate
garbage collection; two consecutive response timeouts restart the HTTP
listener while retaining the active station connection and socket pool. A bad
client descriptor is discarded with receive/send stage context in the serial
log, while an unscoped bad descriptor restarts that listener without resetting
the station connection.
Successfully completed responses also trigger collection before the next
request so closed Pico2 W client sockets are released promptly.
The response body uses both `setblocking(False)` and `settimeout(0)` on the
accepted-client socket; Pico2 W response writes require both controls to avoid
retaining the server's positive native timeout. Final close is also
nonblocking. Would-block results are retried only within the bounded response
deadlines, and any send failure immediately closes that client so it cannot
hold the cooperative runtime or leak into the following request.
After constructing a configuration page body, the renderer collects temporary
formatting objects before allocating the full document. A second collection
immediately before that allocation limits fragmentation from shared wrapper
construction.
Setup profile and display fragment lists are released and collected before the
next larger allocation. A guarded `MemoryError` logs only its failing stage and
free heap.
Page-specific Status, Config, and Automation renderer modules are released when
navigating to another page family.
Unexpected page-renderer failures return HTTP 500 and leave other web routes
available. Heap-guard and render-allocation failures continue to return the
temporary HTTP 503 response.

The JSON `/config` route accepts a broader supported update set than the
rendered page:

- live-safe: `Sensor.LOCATION`, `Switch.SWITCH_LOCATION`, switch labels,
  display metrics/styles, `[Calibration.System]`, `[Calibration.Device]`,
  `[NPK]`, `Time.*`, and switch last state
- restart-required: `Network.SSID`, `Network.PASSWORD`, `Network.HOSTNAME`,
  `Network.HTTPPORT`, `Network.AP_CHANNEL`, `[MQTT]`, `[Profile]`, and
  `[HomeAssistant]`

Runtime MQTT `Display.*` writes use a low-stack handler for
`Display.METRIC_1` through `Display.METRIC_6` and `Display.Style.METRIC_1`
through `Display.Style.METRIC_6`. Nodus applies the live display setting and
publishes successful `config/result` plus `meta/patch` before attempting TOML
persistence. If persistence fails from memory or Python-stack pressure, the
live change remains active until reboot and the serial command log reports
`persistence_mode=volatile`.

Runtime MQTT `Sensor.LOCATION` and `Switch.SWITCH_LOCATION` writes use the same
live-first pattern. Nodus updates the runtime location and publishes successful
`config/result` plus `meta/patch` before best-effort TOML persistence. If
persistence fails from memory or Python-stack pressure, the location remains
active until reboot and the serial command log reports
`persistence_mode=volatile`.

Runtime MQTT `Switch.SWITCH_1_LABEL` and `Switch.SWITCH_2_LABEL` writes also
use the live-first pattern. Nodus updates the runtime switch label and
publishes successful `config/result` plus `meta/patch` before best-effort
`switch.toml` persistence. If persistence fails from memory or Python-stack
pressure, the label remains active until reboot and the serial command log
reports `persistence_mode=volatile`.

`AP_SSID` and `AP_PASSWORD` are read from `[Network]` for AP mode but are not
currently accepted by the web config classifier.

Calibration offsets in `[Calibration.System]` and `[Calibration.Device]` are additive corrections.
For example, a `CO2_OFFSET = -400.0` reduces the live measured `CO2` value by `400 ppm` before publish.
`Calibration.Device.ALTITUDE_METERS` is device setup calibration, not an additive metric offset.
When set to a non-zero meters value, BME280 and BME680 devices use it to
normalize the published `Baro-Pressure` from station pressure to sea-level
pressure. BME680 also seeds driver sea-level pressure at startup, and
SCD30/SCD4x drivers use the value for CO2 altitude compensation at driver
startup.

Published `Baro-Pressure` and `Plant Baro-Pressure` values use one decimal
place, providing consistent `0.1 hPa` resolution for BME280 and BME680 devices.

Soil sensor `[Calibration.Device]` keys are:

- `SOIL_TEMP_CAL_VAL`
- `SOIL_MOIST_CAL_VAL`
- `SOIL_PH_CAL_VAL`
- `SOIL_EC_CAL_VAL`

`SOIL_MOIST_CAL_VAL` is the canonical soil moisture additive offset. Older
deployed files that still contain `SOIL_TEMP_MOIST_VAL` are read as a legacy
alias, but new templates and MQTT calibration patches use
`SOIL_MOIST_CAL_VAL`.

Corner case:

- The normal-mode setup UI is available only when `ACTIVE_PROFILE = "nodusweb"`.
- Devices for MQTT-enabled profiles should be provisioned through AP/nodusweb mode before being switched into the target profile.

Manual switch overrides use the live runtime switch controller and persist the
resulting `SWITCH_#_LAST_STATE`, so the next boot starts from the last
successfully applied manual state. The browser and backend enforce a
per-channel five-second guard. An enabled local rule owns its target channel
and blocks manual changes until disabled. Rules are stored transactionally in
`automations.toml`; see [automations.md](automations.md).

## Time settings

Time is configured in the `[Time]` section of `settings.toml`:

- `TZ` (e.g., `America/Denver`)
- `TZ_OFFSET` (seconds, or hours if abs <= 14)
- `TZ_NAME` (e.g., `MST`)
- `NTP_SERVER` (optional hostname or IP; defaults to `us.pool.ntp.org` when blank)
- `NTP_SERVER_IP` (optional IP fallback used when hostname/DNS sync fails; default `132.163.96.6`)

Sensorius AP bootstrap may provide these same supported keys in top-level
`/itaot-init.time`. Nodus persists them to `[Time]` when present; missing Time
keys do not block onboarding.

`NTP_SERVER` may be left blank to use the default US pool host. If
`NTP_SERVER_IP` is set, Nodus tries the hostname first and then the IP fallback
when DNS or the hostname-based sync fails. NTP sync is deferred until hostname
resolution is available when no fallback succeeds; this avoids early boot
failures when DNS/mDNS is not ready yet. Nodus attempts startup sync up to
three times (60s spacing), then pauses NTP for one hour. After the cooldown it
performs one final three-attempt window, then disables further NTP attempts for
that configured server. Startup NTP is attempted after normal network/socket
artifacts are ready and is not held behind MQTT startup publish/subscription
queues. MQTT availability and heartbeat status do not require NTP; unsynced
devices continue publishing online state with best-effort timestamps.

Accepted runtime `config/set` writes for supported `Time.*` keys also request a
fresh NTP sync after the config acknowledgements and MQTT publish queue drain.
This lets Sensorius daylight/standard-time `TZ_OFFSET` and `TZ_NAME` changes
move the Nodus RTC without a reboot.

Runtime MQTT `Time.*` writes use a low-stack handler. Nodus applies the live
time setting and publishes successful `config/result` plus `meta/patch` before
attempting TOML persistence. If persistence fails from memory or Python-stack
pressure, the live change remains active until reboot and the serial command
log reports `persistence_mode=volatile`.

## Soil RS485 channels

`sensor_soil.toml` supports one or two soil sensors on a dual-channel RS485 hat.

- `[Modbus]` remains supported for existing single-sensor configurations.
- `[Modbus.CH1]` configures CH1, normally `GP0` TX and `GP1` RX.
- `[Modbus.CH2]` configures CH2, normally `GP4` TX and `GP5` RX.
- Leave a channel's `UART_TX` and `UART_RX` blank to disable that channel.
- Factory bootstrap probes both CH1 and CH2 and writes whichever soil channels respond.

`[SoilSensorRegisters]` remains the primary source for soil metric registers.
For 4-in-1 and 7-in-1 profiles, the runtime also has a narrow fallback for an
observed alternate manufacturer layout: if the configured pH register is not a
plausible soil pH value and register `0x0007` is plausible, Nodus reports pH
from `0x0007` and EC from `0x000C` when the configured EC value is empty or
zero. N/P/K registers are not guessed; set `N_REG`, `P_REG`, and `K_REG`
explicitly when a sensor's nutrient registers are confirmed.

Each channel uses the same keys:

```toml
UART_TX = "GP0"
UART_RX = "GP1"
MODBUS_ADDR = 1
MODBUS_BAUD = 9600
MODBUS_TIMEOUT_S = 0.30
SOIL_VARIANT = "canonical"
```

When exactly one soil channel is active, payload metric names stay unchanged,
for example `Soil Moisture` and `Soil Temp_C`. When both channels are active,
Nodus prefixes metric names with the channel, for example `CH1 Soil Moisture`
and `CH2 Soil Moisture`, so Home Assistant and Sensorius receive distinct
values.

## Soil deficit thresholds

`sensor_soil.toml` includes a `[SoilDeficit]` section that controls how `Soil Moisture Deficit` is calculated:

- `SPD_WET_THRESHOLD_PCT`
- `SPD_DRY_THRESHOLD_PCT`

`Soil Moisture Deficit` is a normalized dryness percentage derived from corrected `Soil Moisture`, not an independent raw sensor register.

It represents the current soil water shortfall within the configured wet-to-dry operating band, not the soil's inherent water-holding capacity.

```text
Soil Moisture Deficit = 100 * ((wet_threshold - corrected_soil_moisture) / (wet_threshold - dry_threshold))
```

Behavior:

- moisture at or above the wet threshold maps to `0%`
- moisture at or below the dry threshold maps to `100%`
- values in between are scaled linearly
- higher values indicate drier soil and greater irrigation need
- the output is clamped to `0-100%`
- if `SPD_WET_THRESHOLD_PCT <= SPD_DRY_THRESHOLD_PCT`, `Soil Moisture Deficit` is not reported

Defaults in `sensor_soil.toml.def`:

- `SPD_WET_THRESHOLD_PCT = 38.0`
- `SPD_DRY_THRESHOLD_PCT = 18.0`

## Soil stress settings

`sensor_soil.toml` also includes a `[SoilStress]` section that controls how `Soil Stress Index` is calculated:

- `SSI_TEMP_LOW_CRIT_C`
- `SSI_TEMP_LOW_OK_C`
- `SSI_TEMP_HIGH_OK_C`
- `SSI_TEMP_HIGH_CRIT_C`
- `SSI_MOISTURE_WEIGHT_PCT`
- `SSI_TEMP_WEIGHT_PCT`

`Soil Stress Index` is a normalized soil concern percentage derived from:

- `Soil Moisture Deficit`
- corrected `Soil Temp_C`

Default temperature bands:

- overall range: `15°C` to `30°C`
- too low: below `18°C`
- OK: `18°C` to `24°C`
- too high: above `24°C`

Default critical edges:

- `SSI_TEMP_LOW_CRIT_C = 15.0`
- `SSI_TEMP_HIGH_CRIT_C = 30.0`

At the critical edges and beyond, the temperature contribution is `100%`. Inside the OK band, the temperature contribution is `0%`. Values in between scale linearly.

Default weights:

- `SSI_MOISTURE_WEIGHT_PCT = 70.0`
- `SSI_TEMP_WEIGHT_PCT = 30.0`

Formula:

```text
soil_temp_stress = temperature-based stress from 0 to 100
Soil Stress Index = ((Soil Moisture Deficit * moisture_weight) + (soil_temp_stress * temp_weight)) / (moisture_weight + temp_weight)
```

If the temperature band is invalid or the total weight is zero, `Soil Stress Index` is not reported.

## Soil NPK targets

`sensor_soil.toml` includes an `[NPK]` section that controls how `Soil Fertility Index` is calculated:

- `N_TARGET`
- `P_TARGET`
- `K_TARGET`

`Soil Fertility Index` is a normalized `0-100%` NPK sufficiency score for 7-in-1 soil sensors. It is derived from reported `Soil Nitrogen`, `Soil Phosphorus`, and `Soil Potassium`.

Formula:

```text
n_score = clamp(Soil Nitrogen / N_TARGET, 0.0, 1.0)
p_score = clamp(Soil Phosphorus / P_TARGET, 0.0, 1.0)
k_score = clamp(Soil Potassium / K_TARGET, 0.0, 1.0)
Soil Fertility Index = 100 * ((0.5 * min_score) + (0.5 * avg_score))
```

The lowest nutrient score is weighted with the average nutrient score so one deficient nutrient lowers the final index. The output is rounded and clamped to `0-100%`.

Defaults in `sensor_soil.toml.def` are in `mg/kg`, matching the 7-in-1 sensor's reported nutrient metrics. They are sensor-scale defaults based on healthy living-soil readings, not lab sufficiency thresholds:

- `N_TARGET = 20.0`
- `P_TARGET = 30.0`
- `K_TARGET = 70.0`

`Soil Fertility Index` is only reported when the active soil channel has `SOIL_VARIANT = "soil_7in1"`. If any N/P/K value is missing or any target is `0` or lower, `Soil Fertility Index` is not reported.

## Tips

- Keep SSID/password correct to avoid AP fallback.
- AP fallback network settings are in `[Network]`: `AP_SSID` and `AP_PASSWORD`.
- If MQTT is disabled, the device can still run as a local sensor.
