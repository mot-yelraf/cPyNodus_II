# Nodus integration with WeeWX

This guide provisions a Nodus sensor to publish to MQTT, integrates its data
into a standard Raspberry Pi OS WeeWX package installation, and installs the
optional Nodus report. The automated path can install WeeWX when it is
absent. Both paths require an operational MQTT broker.

The examples use these names:

- Nodus sensor: `aht-va41ka`
- Nodus web address: `http://aht-va41ka.local:8000`
- MQTT and WeeWX host: `<host>`
- MQTT base topic: `nodus`

Replace them for another installation.

The SSH/SCP examples below use `<user>@<host>`. Replace both placeholders with
the Raspberry Pi login and a resolvable hostname or SSH configuration alias.

## Data path

```text
Nodus sensor
  -> nodus/<sensor-id>/data and retained /meta on the MQTT broker
  -> MQTTSubscribe service or driver
  -> WeeWX loop/archive records
  -> /var/lib/weewx/nodus.sdb for an automated Nodus instance
  -> Nodus Cheetah report + retained-meta identity extension
  -> /var/www/html/weewx/nodus/index.html
```

The `weewx` profile does not publish a separate WeeWX payload. It publishes the
normal `nodus-sensor-data/v1` JSON payload. The host-side MQTTSubscribe mapping
selects values from that payload and assigns WeeWX observation names and units.

## Standard Raspberry Pi package paths

This guide assumes WeeWX was installed from the official Debian package on
Raspberry Pi OS. The standard locations are:

| Purpose | Path |
| --- | --- |
| Main configuration | `/etc/weewx/weewx.conf` |
| User extensions | `/etc/weewx/bin/user/` |
| Skins | `/etc/weewx/skins/` |
| Default SQLite archive | `/var/lib/weewx/weewx.sdb` |
| Automated Nodus archive | `/var/lib/weewx/nodus.sdb` |
| Generated reports | `/var/www/html/weewx/` |
| systemd service | `weewx.service` |

Confirm the local installation before changing it:

```bash
weectl --version
sudo systemctl status weewx
sudo grep -nE 'SKIN_ROOT|HTML_ROOT|SQLITE_ROOT|database_name' \
  /etc/weewx/weewx.conf
```

The official WeeWX Debian quick start covers the package installation and
these standard paths:
<https://www.weewx.com/docs/latest/quickstarts/debian/>.

## Automated installation on the WeeWX host

This is the recommended path when this repository has been cloned directly on
the Debian or Raspberry Pi OS system that will run WeeWX:

```bash
cd /path/to/cPyNodus_II
./integrations/weewx/install_nodus_weewx.sh
```

Do not run the script on the CircuitPython device or on a workstation that
does not host WeeWX. It is specifically for a Debian-family, systemd-based
WeeWX 5 installation using the standard package paths listed above.

The installer:

1. Checks for `weectl` and `/etc/weewx/weewx.conf`.
2. If WeeWX is absent, asks before adding the official WeeWX apt repository
   and installing the package. Answering no exits without changing the host.
3. Examines the configured `[Station]` identity for useful defaults.
4. Preserves the primary WeeWX configuration and creates an independent Nodus
   instance at `/etc/weewx/nodus.conf`.
5. Installs `python3-paho-mqtt` and MQTTSubscribe 3.1.1 only when missing.
6. Installs the Nodus archive schema, unit registration, retained-MQTT identity
   extension, and Nodus skin from the clone.
7. Validates MQTTSubscribe before enabling and starting the selected service.

The resulting layout is deliberately predictable: `/etc/weewx/nodus.conf`,
`weewx@nodus.service`, `/var/lib/weewx/nodus.sdb`, and
`/var/www/html/weewx/nodus`. The installer does not modify or stop the primary
`/etc/weewx/weewx.conf` or `weewx.service`. This follows WeeWX's templated
`weewx@.service` model for multiple instances.

The installer prompts for the station coordinates, altitude, MQTT broker,
Nodus `device_id`, sensor family, subscriber credentials, and TLS settings. It
backs up an existing Nodus configuration with a timestamped `.bak` suffix
before replacing it. If dependency installation, validation, or service
startup fails, the installer restores the prior Nodus configuration and
restarts the prior Nodus service when it was active. Broker passwords are
written only to the root/weewx-readable configuration and are not printed in
the installation summary.

The automated canonical mapping currently covers AHT/AVPD/APVPD environmental
metrics, CO2, AQI, and soil. APVPD plant-side metrics and lux-only observations
require a manual schema/skin extension; the installer does not silently map
them to unrelated WeeWX observations.

Before making changes, config detection can be checked independently:

```bash
./integrations/weewx/install_nodus_weewx.sh \
  --inspect-config /etc/weewx/weewx.conf
```

Use `--dry-run` to complete the prompts and review the installation plan
without changing the system:

```bash
./integrations/weewx/install_nodus_weewx.sh --dry-run
```

After installation, inspect the Nodus service and its first records:

```bash
sudo systemctl status weewx@nodus --no-pager -l
sudo journalctl -u weewx@nodus --since '10 minutes ago' --no-pager -l
sqlite3 /var/lib/weewx/nodus.sdb \
  "select datetime(max(dateTime),'unixepoch','localtime') from archive;"
```

The remaining numbered sections document device provisioning, broker
verification, and the manual integration path. Do not also perform the manual
host-side configuration after the automated installer has completed.

## 1. Provision the Nodus

Provision Wi-Fi, MQTT, time, and the profile in `settings.toml`. The essential
settings are:

```toml
[Network]
SSID = "<WIFI_SSID>"
PASSWORD = "<NODUS_OBFUSCATED_WIFI_PASSWORD>"
HOSTNAME = "aht-va41ka"
HTTPPORT = 8000

[Profile]
ACTIVE_PROFILE = "weewx"

[MQTT]
BROKER = "<mqtt-host>"
BROKER_IP = "<OPTIONAL_FIXED_BROKER_IP>"
PORT = 1883
USE_TLS = false
BASE_TOPIC = "nodus"
USERNAME = "<NODUS_PUBLISHER_USERNAME_OR_EMPTY>"
PASSWORD = "<NODUS_OBFUSCATED_MQTT_PASSWORD_OR_EMPTY>"

[Time]
TZ = "America/Denver"
TZ_OFFSET = -21600
TZ_NAME = "MDT"
NTP_SERVER = "us.pool.ntp.org"
```

Use the normal Nodus deployment procedure. Do not edit or copy files while the
device is in its normal read-only runtime mode. If configuration changes are
made while the app filesystem is writable, restore the RWFS jumper before the
normal test boot.

The startup log must identify the requested profile:

```text
cPyNodus_II boot version=<version> profile=weewx ap_mode=False
```

The Nodus Info page should also show `Profile: weewx`.

## 2. Verify broker-visible MQTT data

Serial messages saying that a publish succeeded are not sufficient. Subscribe
at the broker and confirm the actual messages:

```bash
mosquitto_sub \
  -h <mqtt-host> \
  -p 1883 \
  -t 'nodus/aht-va41ka/#' \
  -v
```

At minimum, expect:

```text
nodus/aht-va41ka/meta ...
nodus/aht-va41ka/availability ...
nodus/aht-va41ka/status/heartbeat ...
nodus/aht-va41ka/data {"schema":"nodus-sensor-data/v1",...}
```

The `data` message must contain the sensor's current `values`. An AHT normally
publishes temperature, relative humidity, absolute humidity, dew point, dew
point deficit, ambient VPD, and DewVPD risk. It does not publish barometric
pressure.

If the broker requires authentication, add `-u '<subscriber>'` and use `-P`
only in a protected shell environment. Prefer a dedicated read-only subscriber
account for WeeWX.

## 3. Choose MQTTSubscribe service or driver mode

Use exactly one MQTTSubscribe mode:

| Mode | Use it when | WeeWX station driver |
| --- | --- | --- |
| Service | WeeWX already receives loop packets from another station or simulator and Nodus augments them | Keep the existing driver |
| Driver | MQTT/Nodus is the station's source of loop packets | Set the station driver to MQTTSubscribe |

The Nodus-generated integration guide targets **service mode**. Do not enable
both the MQTTSubscribe service and driver for the same topic; that can create
multiple subscribers and duplicate ingestion.

## 4. Install MQTTSubscribe 3.1.1

For a standard WeeWX 5 package installation, run on the Raspberry Pi:

```bash
sudo weectl extension install \
  https://github.com/weewx-mqtt/subscribe/archive/refs/tags/v3.1.1.zip
```

MQTTSubscribe requires the Paho MQTT Python client. Verify the import:

```bash
python3 -c 'import paho.mqtt.client; print("paho-mqtt available")'
```

If it is missing on Raspberry Pi OS, install the distribution package:

```bash
sudo apt update
sudo apt install python3-paho-mqtt
```

Confirm the installed configuration utility. The extension also installs a
lowercase runtime module; the uppercase file is the command-line utility used
below:

```bash
test -f /etc/weewx/bin/user/MQTTSubscribe.py && \
  echo 'MQTTSubscribe configuration utility installed'
```

MQTTSubscribe's maintained repository documents package-install paths,
service/driver selection, configuration validation, and simulation:
<https://github.com/weewx-mqtt/subscribe>.

## 5. Choose automated or manual host installation

The automated installer is the supported stage-one path. cPyNodus_II keeps
normal MQTT profiles headless, so it intentionally does not expose the
`/weewx-config` device endpoint. The host integration consumes the existing
MQTT contract and does not require a firmware payload change.

For a manual installation, use the canonical single-environment mapping below
and transfer the bundled schema, units, identity, automation, and skin files.
Do not also perform the manual installation after the automated installer has
completed.

## 6. Back up and stop WeeWX

Stop WeeWX before changing the archive schema. The official database guide
also requires a backup before `weectl database` changes:
<https://weewx.com/docs/latest/custom/database/>.

```bash
ssh <user>@<host>
```

Then on the Raspberry Pi:

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
sudo systemctl stop weewx
sudo cp -a /etc/weewx/weewx.conf \
  "/etc/weewx/weewx.conf.${STAMP}.bak"
sudo cp -a /var/lib/weewx/weewx.sdb \
  "/var/lib/weewx/weewx.sdb.${STAMP}.bak"
```

Verify that WeeWX stopped:

```bash
sudo systemctl is-active weewx
ps -ef | grep -E '[w]eewxd|[w]eewx'
```

`is-active` should report `inactive`, and the process command should not show a
running `weewxd`.

## 7. Apply the manual integration

Apply these components in order:

1. Merge one MQTTSubscribe driver or service section and the exact device
   `/data` topic into the selected WeeWX configuration.
2. Use a dedicated broker subscriber account. Use empty credentials only when
   the broker permits anonymous access.
3. Install `nodus_schema.py` and `nodus_units.py` from
   `integrations/weewx/bin/user/`, then use `user.nodus_schema.schema` for the
   Nodus archive and load `user.nodus_units.NodusUnits` as a prep service.
4. Install the retained identity and optional automation modules from the same
   directory.
5. Install and enable the bundled `Nodus` skin.

Check existing columns before adding them:

```bash
sqlite3 /var/lib/weewx/weewx.sdb 'pragma table_info(archive);'
```

If the Nodus is the only station data source and driver mode is required,
configure `[MQTTSubscribeDriver]` using the mapping below, set its
driver to `user.mqttsubscribe`, and make it the `[Station]` `station_type`.
Disable `[MQTTSubscribeService]`. Follow the upstream driver instructions for
the installed MQTTSubscribe release rather than enabling both modes.

### Observation names used by Nodus

The supplied Nodus schema and skin are a single-environment dashboard and
expect these canonical observation names:

| Nodus value | Nodus observation | Availability |
| --- | --- | --- |
| `Temperature` | `inTemp` | Environmental sensors |
| `Rel-Humidity` | `inHumidity` | Environmental sensors |
| `Dew Point` | `dewpoint` | Environmental sensors |
| `Ambient VPD` | `vpd` | Environmental sensors |
| `Humidity` | `absoluteHumidity` | Environmental sensors |
| `Dew Point Deficit` | `dewpointDepression` | Environmental sensors |
| `DewVPD Risk` | `dewVpdRisk` | Environmental sensors |
| `Baro-Pressure` | `pressure` | Only pressure-capable sensors |

For `aht-va41ka`, the canonical topic portion is:

```ini
[[[nodus/aht-va41ka/data]]]
    subscribe = true
    ignore = true

    [[[[values_Temperature]]]]
        ignore = false
        name = inTemp
        conversion_type = float

    [[[[values_Rel-Humidity]]]]
        ignore = false
        name = inHumidity
        conversion_type = float

    [[[[values_Dew Point]]]]
        ignore = false
        name = dewpoint
        conversion_type = float

    [[[[values_Ambient VPD]]]]
        ignore = false
        name = vpd
        conversion_type = float

    [[[[values_Humidity]]]]
        ignore = false
        name = absoluteHumidity
        conversion_type = float

    [[[[values_Dew Point Deficit]]]]
        ignore = false
        name = dewpointDepression
        conversion_type = float

    [[[[values_DewVPD Risk]]]]
        ignore = false
        name = dewVpdRisk
        conversion_type = float
```

Place this stanza below the existing MQTTSubscribe `[[topics]]` and
`[[[message]]]` sections. A pressure-capable Nodus can additionally map
`values_Baro-Pressure` to `pressure`.

For a dedicated single environmental Nodus report, use these canonical names
in the MQTTSubscribe topic mapping and report labels. Standard WeeWX schemas
already contain `inTemp`, `inHumidity`, `dewpoint`, and `pressure`; add only
missing custom columns.

For multiple Nodus devices, use separate WeeWX instances/reports or adapt the
schema and skin with device-specific observation names. Do not map several
devices to the same canonical fields.

Nodus checks the current raw value before rendering each card. Therefore,
an AHT report omits Station Pressure instead of displaying `N/A`.

## 8. Transfer and enable the Nodus skin

The complete skin bundle is in:

```text
integrations/weewx/Nodus/
  index.html.tmpl
  skin.conf
  style.css
integrations/weewx/bin/user/
  nodus_identity.py
  nodus_automation.py
  nodus_units.py
  nodus_schema.py
```

From the development host, create a staging directory on the Raspberry Pi and
copy the skin and its identity extension:

```bash
cd /path/to/cPyNodus_II

ssh <user>@<host> \
  'mkdir -p /tmp/Nodus'

scp integrations/weewx/Nodus/index.html.tmpl \
    integrations/weewx/Nodus/skin.conf \
    integrations/weewx/Nodus/style.css \
    integrations/weewx/bin/user/nodus_identity.py \
    integrations/weewx/bin/user/nodus_automation.py \
    integrations/weewx/bin/user/nodus_units.py \
    integrations/weewx/bin/user/nodus_schema.py \
    <user>@<host>:/tmp/Nodus/
```

Install the staged files on the Raspberry Pi:

```bash
ssh <user>@<host> \
  'sudo install -d -m 0755 /etc/weewx/skins/Nodus /etc/weewx/bin/user &&
   sudo install -m 0644 /tmp/Nodus/index.html.tmpl /etc/weewx/skins/Nodus/ &&
   sudo install -m 0644 /tmp/Nodus/skin.conf /etc/weewx/skins/Nodus/ &&
   sudo install -m 0644 /tmp/Nodus/style.css /etc/weewx/skins/Nodus/ &&
   sudo install -m 0644 /tmp/Nodus/nodus_identity.py /etc/weewx/bin/user/ &&
   sudo install -m 0644 /tmp/Nodus/nodus_automation.py /etc/weewx/bin/user/ &&
   sudo install -m 0644 /tmp/Nodus/nodus_units.py /etc/weewx/bin/user/ &&
   sudo install -m 0644 /tmp/Nodus/nodus_schema.py /etc/weewx/bin/user/'
```

If the Raspberry Pi already has a customized Nodus `style.css`, back it
up and omit the final `style.css` install command unless the repository default
style is wanted.

`style.css` is a `copy_once` skin asset. When updating an already-generated
Nodus report, also replace its published copy so the new layout takes
effect immediately:

```bash
ssh <user>@<host> \
  'sudo install -o weewx -g weewx -m 0664 \
   /tmp/Nodus/style.css /var/www/html/weewx/nodus/style.css'
```

Add the report under `[StdReport]` in `/etc/weewx/weewx.conf`:

```ini
[StdReport]
    [[Nodus]]
        skin = Nodus
        HTML_ROOT = /var/www/html/weewx/nodus
        enable = true
```

Merge this subsection into the existing `[StdReport]`; do not create a second
top-level `[StdReport]` section.

### Renaming an existing historical skin installation

Earlier repository versions used a different report and skin name. Upgrade an
existing installation by installing this bundle in `/etc/weewx/skins/Nodus/`,
renaming the report subsection to `[[Nodus]]`, and setting `skin = Nodus`.
Report commands must likewise use `weectl report run Nodus`. The published
HTML root remains `/var/www/html/weewx/nodus`, so bookmarks and web-server
configuration do not change.

If the earlier skin had a customized `style.css`, preserve a backup and copy
those customizations into `/etc/weewx/skins/Nodus/style.css` after installing
the new bundle. The earlier skin directory is no longer referenced and can be
archived after the renamed report generates successfully.

No device ID or firmware version is entered manually. The
`user.nodus_identity.NodusIdentity` Cheetah extension finds the single exact
`nodus/<device_id>/data` topic in the active MQTTSubscribe driver or service
configuration, subscribes to the corresponding retained `/meta` topic, and
uses its `device_id` and `version`. It reuses the MQTTSubscribe broker, port,
credentials, and TLS settings. A successful result is cached under
`/var/lib/weewx/`, so a brief broker outage does not erase the header identity.
The next report generation reads newly retained metadata after a firmware
update; no `weewx.conf` version update is required.

The same metadata selects the header description:

| Sensor family | Description |
| --- | --- |
| AVPD, AHT, and APVPD variants | Indoor environment + VPD + disease-risk indicators. |
| CO2 | Outdoor environment + CO2 + VPD + disease-risk indicators. |
| AQI | Indoor environment + AQI + VPD + disease-risk indicators. |
| Soil | Soil Moisture + Temperature |

Nodus uses the device-ID family first and sensor hardware as a fallback.
An unrecognized sensor displays `Sensor conditions` rather than an incorrect
family description.

Nodus represents one environment. If the MQTTSubscribe section contains
more than one exact Nodus `/data` topic, select the report's device once by
adding this report subsection:

```ini
        [[[NodusIdentity]]]
            meta_topic = nodus/aht-va41ka/meta
```

This selection is only needed to disambiguate multiple devices. Firmware
versions are still obtained automatically.

The skin:

- arranges available cards alphabetically;
- hides observations unavailable for the current device;
- formats VPD as `#.###`;
- reloads the browser every 60 seconds;
- identifies the source Nodus by `device_id` and firmware version;
- selects a sensor-family description automatically from retained metadata;
- adds a compact 24-hour graph to each available metric card using WeeWX's
  native ImageGenerator; every archived point is plotted, and the archive
  timestamp in the image URL prevents a browser from reusing an older PNG;
- uses the existing WeeWX almanac for sunrise, sunset, and moon phase.

## Switch automations

The optional `user.nodus_automation.NodusAutomation` WeeWX data service can
control Nodus switch channels from gathered WeeWX observations. Automation
logic runs on the WeeWX host, not in the generated HTML and not on the
CircuitPython device.

Each rule combines these requirements with logical AND:

- every configured metric condition is active;
- the current local day is enabled; and
- the current local time is inside the rule's active window.

Each metric condition has separate ON and OFF thresholds. This hysteresis,
together with minimum ON/OFF dwell times, prevents relay chatter around a
single threshold. The service also stops trusting an observation after its
configured freshness interval.

The automated installer registers the service but leaves it disabled. Set
`enabled = true` and configure rules such as the following. For a manual skin
installation, also add `user.nodus_automation.NodusAutomation` after
MQTTSubscribe in `[Engine] / [[Services]] / data_services`.

```ini
[NodusAutomation]
    enabled = true
    status_file = /var/lib/weewx/nodus_automation.json
    stale_after = 180
    command_timeout = 15

    # Optional writable MQTT account. When omitted, the MQTTSubscribe
    # connection settings and credentials are reused.
    username = weewx-automation
    password = <AUTOMATION_MQTT_PASSWORD>

    [[rules]]
        [[[greenhouse_fan]]]
            enabled = true
            channel_id = S1-ykdvea
            condition_1 = inTemp, above, 27.0, 25.0
            condition_2 = inHumidity, above, 70.0, 65.0
            start_time = 08:00
            end_time = 20:00
            days = mon, tue, wed, thu, fri, sat, sun
            outside_window = off
            stale_action = off
            minimum_on_seconds = 300
            minimum_off_seconds = 300
            retry_seconds = 60
```

The four values in each numbered condition are:

```text
WeeWX observation, above|below, ON threshold, OFF threshold
```

For an `above` condition, the OFF threshold must be lower than the ON
threshold. For a `below` condition, it must be higher. Number conditions
consecutively as `condition_1`, `condition_2`, and so on. Thresholds use the
units present in the WeeWX loop packet when it reaches the automation service;
the automated Nodus instance maps MQTT input as `METRICWX`.

`start_time` and `end_time` use the WeeWX host's local `HH:MM` time. Equal
values mean all day. An overnight range such as `20:00` to `06:00` is
supported. `days` uses three-letter lowercase names. The day check applies to
the current local day, including the after-midnight portion of an overnight
window.

`outside_window` and `stale_action` accept:

- `off`: request OFF after the normal dwell check;
- `hold`: leave the current switch state unchanged.

Only one enabled rule may target a channel. `retry_seconds` delays another
attempt after a publish, rejection, failed result, or confirmation timeout so
a broker or device fault cannot produce a rapid command loop.

The service derives `nodus/<device_id>/meta/switch` from the single exact
MQTTSubscribe `/data` topic. With multiple `/data` topics, set an exact
`meta_topic` in `[NodusAutomation]`. It discovers the channel index and all
command/response topics from retained switch metadata; do not enter a GPIO pin
or construct `SWITCH_N_LAST_STATE` manually.

Commands use the canonical non-retained JSON `config/set` envelope. A command
is complete only after the service receives its correlated `config/ack`, a
successful `config/result`, and the requested retained channel state. Timeout,
rejection, and apply errors are recorded in the status file and shown by the
Nodus Switch Automations cards after the next report generation.

For production, use a dedicated broker account whose ACL can:

- subscribe to the selected device's `meta/switch` topic;
- subscribe to the selected channels' `state`, `config/ack`, and
  `config/result` topics; and
- publish only to those channels' `config/set` topics.

After enabling or editing rules, restart WeeWX and inspect the service log:

```bash
sudo systemctl restart weewx@nodus
sudo journalctl -u weewx@nodus --since '10 minutes ago' --no-pager -l
sudo -u weewx test -r /var/lib/weewx/nodus_automation.json
```

Use `weewx.service` instead for an installation that uses the main instance.
Before connecting real equipment, validate thresholds and time windows with a
non-critical relay and broker-visible captures of `config/set`, `config/ack`,
`config/result`, and `state`.

## 9. Validate and start WeeWX

The commands below use the automated install targets. For a manual integration
merged into the primary instance, substitute `/etc/weewx/weewx.conf` and
`weewx.service`.

Select the installed MQTTSubscribe configuration utility:

```bash
MQTTSUB=/etc/weewx/bin/user/MQTTSubscribe.py
printf '%s\n' "$MQTTSUB"
```

Validate service-mode configuration:

```bash
sudo -u weewx env PYTHONPATH=/usr/share/weewx \
  python3 "$MQTTSUB" configure service \
  --validate --conf /etc/weewx/nodus.conf
```

Optionally run the MQTTSubscribe simulator while the WeeWX service is stopped:

```bash
sudo -u weewx env PYTHONPATH=/usr/share/weewx \
  python3 "$MQTTSUB" simulate service \
  --conf /etc/weewx/nodus.conf
```

Start WeeWX and inspect the complete log:

```bash
sudo systemctl start weewx@nodus
sudo systemctl status weewx@nodus --no-pager -l
sudo journalctl -u weewx@nodus --since '5 minutes ago' --no-pager -l
```

Run the Nodus report immediately from a directory readable by the `weewx`
service account. This avoids a `PermissionError` when the invoking user's home
directory is not traversable by `weewx`:

```bash
cd /tmp
sudo -u weewx weectl report run Nodus \
  --config=/etc/weewx/nodus.conf
```

The normal report cycle will also regenerate it after new archive records.

## 10. Verify archived observations and HTML

For the canonical single-environment mapping, inspect recent records:

```bash
sqlite3 /var/lib/weewx/weewx.sdb \
  "select datetime(dateTime,'unixepoch','localtime'),
          inTemp, inHumidity, dewpoint, pressure,
          vpd, absoluteHumidity, dewpointDepression, dewVpdRisk
   from archive
   order by dateTime desc
   limit 5;"
```

An AHT should have values for its environmental fields and no pressure value.

Check report generation:

```bash
stat /var/www/html/weewx/nodus/index.html
grep -n -A2 'asof-label' /var/www/html/weewx/nodus/index.html
ls -l /var/www/html/weewx/nodus/micro_*.png
```

Open:

```text
http://<host>/weewx/nodus/
```

The page reloads every 60 seconds. The displayed `As of` time advances only
when WeeWX has generated a report from a newer archive record.

## 11. Troubleshooting

### Header shows unknown device and firmware

Verify that the identity extension is installed and that the retained metadata
is available to the same broker account used by MQTTSubscribe:

```bash
test -r /etc/weewx/bin/user/nodus_identity.py
mosquitto_sub -h <mqtt-host> \
  -t 'nodus/aht-va41ka/meta' -C 1 -v
cd /tmp
sudo -u weewx weectl report run Nodus \
  --config=/etc/weewx/nodus.conf
sudo journalctl -u weewx@nodus --since '10 minutes ago' --no-pager -l | \
  grep -i 'Nodus identity'
ls -l /var/lib/weewx/nodus_identity_*.json
grep -n -A4 'device-identity' \
  /var/www/html/weewx/nodus/index.html
```

If MQTTSubscribe has no exact `/data` topic or has several, set the
report-specific `NodusIdentity.meta_topic` shown above. The `unknown` values in
the skin are only a failure-safe fallback; ordinary installations should not
edit them.

### Nodus publishes, but WeeWX does not update

Verify each boundary independently:

```bash
mosquitto_sub -h <mqtt-host> \
  -t 'nodus/aht-va41ka/data' -v

sudo journalctl -u weewx@nodus --since '30 minutes ago' --no-pager -l

sqlite3 /var/lib/weewx/weewx.sdb \
  "select datetime(max(dateTime),'unixepoch','localtime') from archive;"
```

If MQTT is current but the archive is not, check the configured topic,
flattening delimiter, field names, MQTTSubscribe mode, broker credentials, and
validation output.

### WeeWX reports `invalid units`

Run MQTTSubscribe's configuration validation using the exact installed
version. Unit names are MQTTSubscribe/WeeWX identifiers, not display labels,
and acceptance depends on the installed release and observation registration.
Ensure the bundled `NodusUnits` prep service loads before custom
observations are ingested. If validation identifies a specific per-field unit
as invalid, remove that `units` line only after the observation has been
registered with the correct WeeWX unit group; MQTTSubscribe can then use the
registered group.

### The web page is stale

Compare the newest archive timestamp with the generated HTML modification time:

```bash
sqlite3 /var/lib/weewx/weewx.sdb \
  "select datetime(max(dateTime),'unixepoch','localtime') from archive;"
stat /var/www/html/weewx/nodus/index.html
```

If the database is current but HTML is old, run the report manually and inspect
the journal for Cheetah errors.

### Manual report fails with permission denied for a home directory

Run it from `/tmp`:

```bash
cd /tmp
sudo -u weewx weectl report run Nodus \
  --config=/etc/weewx/nodus.conf
```

### A metric card is missing

Nodus deliberately hides observations whose current raw value is absent.
Confirm that the mapped archive column has a value in the newest record. A
missing Station Pressure card is expected for an AHT sensor.

## Adding another Nodus

Provision and verify the new device first. In service mode, merge its additional
topic under the existing MQTTSubscribe `[[topics]]` section and preserve all
existing services.

Use device-specific observations for multiple devices. A canonical field such
as `inTemp` can represent only one selected environmental sensor in a single
WeeWX record and Nodus report.
