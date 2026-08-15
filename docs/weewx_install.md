# WeeWX Nodus installation: simple step-by-step runbook

Use this runbook to install the Nodus WeeWX integration on a Debian or
Raspberry Pi OS host, or to replace the Nodus currently used by WeeWX.

This is the short operator procedure. [`weewx.md`](weewx.md) is the detailed
reference for schemas, services, MQTT permissions, automations, and manual
configurations.

## The safe workflow

The safe installation order is always:

1. Configure the intended Nodus with `ACTIVE_PROFILE = "weewx"`.
2. Disable automatic provisioning on the WeeWX host.
3. Remove the old installed WeeWX Nodus, if there is one.
4. Bootstrap the host for the intended sensor family without naming a device.
5. Confirm that the intended Nodus appears as **Discovered**.
6. Explicitly select that exact device under **Install Device**.
7. Verify MQTT, the WeeWX service, the database, and the dashboard.

Leave automatic provisioning disabled when the broker contains more than one
discovered device. Explicit selection is clearer and safer.

## Example used below

The commands below use the installation completed on `sensorius-hub-1`:

| Setting | Example value |
| --- | --- |
| WeeWX host | `sensorius-hub-1` |
| Intended Nodus | `avpd-1jm5s1` |
| Sensor family | `avpd` |
| MQTT broker | `localhost` on the WeeWX host |
| MQTT port | `1883` |
| MQTT base topic | `nodus` |
| Station altitude | `1781, meter` |

Replace `avpd-1jm5s1`, `avpd`, and the station values when installing another
device. Do not blindly copy the example device ID.

Supported automated families are:

- `aht`
- `avpd`
- `apvpd`
- `apvpd_aht`
- `co2`
- `aqi`
- `soil`

## Step 1: configure the intended Nodus

The intended device must use the `weewx` profile and the same MQTT broker as
the WeeWX host. For the example device, the essential settings are:

```toml
[Network]
HOSTNAME = "avpd-1jm5s1"
HTTPPORT = 8000

[Profile]
ACTIVE_PROFILE = "weewx"

[MQTT]
BROKER = "sensorius-hub-1.local"
BROKER_IP = "10.0.0.241"
PORT = 1883
BASE_TOPIC = "nodus"
USE_TLS = false
```

Also configure the correct Wi-Fi and MQTT credentials. Restart the Nodus after
saving. Its serial log must eventually show:

```text
profile=weewx
mqtt connect phase=connected
```

If replacing an existing WeeWX Nodus, stop or power off the old device.

## Step 2: open the project on the WeeWX host

Run all remaining commands on the Debian or Raspberry Pi OS WeeWX host:

```bash
cd ~/Projects/cPyNodus_II
```

Confirm that the installer contains the discovery-first mode:

```bash
./setup_nodus_weewx_host.sh --help
```

The output must include:

```text
--discovery-only
--family FAMILY
```

If those options are missing, update the project copy before continuing.

## Step 3: determine whether this is fresh or a replacement

Check whether the manager is already installed:

```bash
curl -sS http://127.0.0.1:8768/api/system |
  python3 -m json.tool
```

- If the command cannot connect, this is a fresh host. Continue with Step 5.
- If it returns JSON, continue with Step 4.

On a fresh host, also check WeeWX:

```bash
weectl --version
```

If `weectl` is missing, the Step 5 dry run will only report that WeeWX would be
installed. Continue to Step 6, answer **Yes** when asked to install WeeWX, and
then complete the normal station and discovery-only prompts. The installer uses
the official WeeWX Debian package and continues after package installation.

## Step 4: prepare an existing installation for replacement

### 4.1 Disable automatic provisioning

Use the General Settings page:

```text
http://<weewx-host>:8768/system/#system-settings
```

Open **WeeWX Preferences**, set **Automatic provisioning** to **No**, and
click **Save**.

Alternatively, use:

```bash
curl -sS \
  -X POST \
  -H 'Content-Type: application/json' \
  --data '{
    "title":"Nodus Automation Instrumentorum",
    "online_timeout_seconds":150,
    "auto_provision":false
  }' \
  http://127.0.0.1:8768/api/system |
  python3 -m json.tool
```

Verify that the response from `/api/system` now contains:

```json
"auto_provision": false
```

### 4.2 Remove only the old installed WeeWX device

Open:

```text
http://<weewx-host>:8768/system/#remove-device
```

Select only the device carrying the **Installed** badge. Check the confirmation
box, then click **Remove Selected**.

Important:

- Removal deletes the selected WeeWX database and generated dashboard.
- If that history must be preserved, stop here and back up
  `/var/lib/weewx/nodus.sdb` before removing the device.
- Removal clears retained MQTT topics belonging to the selected device.
- Never select an unrelated Sensorius device merely because it appears as
  **Discovered**.

Verify the old operational installation is gone:

```bash
sudo test ! -e /etc/weewx/nodus.conf &&
  echo "operational config absent"

sudo test ! -e /var/lib/weewx/nodus.sdb &&
  echo "old database absent"

sudo test ! -e /var/lib/weewx/nodus_installed.json &&
  echo "installed-device record absent"

sudo systemctl is-active weewx@nodus || true
```

Expected final service state:

```text
inactive
```

## Step 5: dry-run discovery-only bootstrap

Use the family of the intended device. For `avpd-1jm5s1`:

```bash
./setup_nodus_weewx_host.sh \
  --discovery-only \
  --family avpd \
  --dry-run
```

Answer the prompts as follows:

| Prompt | What to enter |
| --- | --- |
| Station description | Accept the existing value if correct |
| Latitude | Accept the existing value if correct |
| Longitude | Accept the existing value if correct |
| Altitude with unit | A value such as `1781, meter` |
| MQTT broker | `localhost` when the broker runs on this host |
| MQTT port | `1883`, unless deliberately changed |
| MQTT base topic | `nodus`, unless deliberately changed |
| MQTT subscriber username | Broker subscriber username, or blank |
| MQTT subscriber password | Broker subscriber password, or blank |
| MQTT TLS | `no`, unless TLS is configured |
| Apply this installation? | `y` |

The altitude must contain both a number and a WeeWX unit. These are valid:

```text
1781, meter
5843, foot
```

This is invalid:

```text
1781
```

The expected plan is:

```text
mode:       discovery-only
family:     avpd
installed:  none (awaiting discovery)
```

The expected final line is:

```text
DRY-RUN: configuration rendered successfully; no changes applied.
```

## Step 6: apply discovery-only bootstrap

Run the same command without `--dry-run`:

```bash
./setup_nodus_weewx_host.sh \
  --discovery-only \
  --family avpd
```

Use the same answers and enter `y` at the final prompt.

Expected final output:

```text
Nodus WeeWX discovery bootstrap complete.
Managed template family: avpd
No operational device is installed.
```

At this point, `weewx@nodus` must still be inactive. That is expected.

## Step 7: verify the bootstrap state

```bash
sudo python3 -c '
import json
config = json.load(open("/etc/weewx/nodus-discovery.json"))
print("template family:", config.get("template_family"))
print("altitude:", config.get("altitude"))
'

sudo test ! -e /etc/weewx/nodus.conf &&
  echo "operational config absent"

sudo test ! -e /var/lib/weewx/nodus_installed.json &&
  echo "installed-device record absent"

sudo systemctl is-active weewx@nodus || true
```

For the example, expect:

```text
template family: avpd
altitude: 1781, meter
operational config absent
installed-device record absent
inactive
```

## Step 8: confirm MQTT and discovery

Power on or restart the intended Nodus. Wait about 30 seconds.

Confirm one broker-visible data message:

```bash
mosquitto_sub \
  -h 127.0.0.1 \
  -t nodus/avpd-1jm5s1/data \
  -C 1 \
  -W 30
```

This must return a JSON sensor payload. A serial log alone is not sufficient
proof that the broker received the data.

Check the manager:

```bash
curl -sS http://127.0.0.1:8768/api/system |
  python3 -m json.tool
```

Find the intended device and verify:

```json
"device_id": "avpd-1jm5s1",
"installed": false,
"discovered": true
```

Ignore unrelated devices carrying only a **Discovered** badge.

## Step 9: explicitly install the intended device

Keep automatic provisioning set to **No**.

Open:

```text
http://<weewx-host>:8768/system/#install-device
```

Then:

1. Select exactly `avpd-1jm5s1`.
2. Confirm that unrelated devices are not selected.
3. Click **Install Selected**.

The success message must be:

```text
Installed avpd-1jm5s1 from discovery.
```

The equivalent command-line action is:

```bash
curl -sS \
  -X POST \
  -H 'Content-Type: application/json' \
  --data '{"device_id":"avpd-1jm5s1"}' \
  http://127.0.0.1:8768/api/install |
  python3 -m json.tool
```

Do not enable automatic provisioning merely to install a selected device.

## Step 10: verify the running installation

### 10.1 Confirm the selected device

```bash
curl -sS http://127.0.0.1:8768/api/system |
  python3 -m json.tool
```

The intended device must show:

```json
"device_id": "avpd-1jm5s1",
"installed": true,
"discovered": true
```

### 10.2 Confirm the service

```bash
sudo systemctl status weewx@nodus --no-pager -l
```

Expected:

```text
Active: active (running)
```

### 10.3 Confirm the generated configuration

```bash
sudo grep -nE \
  'altitude =|nodus/[^/]+/data' \
  /etc/weewx/nodus.conf
```

For the example, expect:

```text
altitude = 1781, meter
[[[nodus/avpd-1jm5s1/data]]]
```

### 10.4 Wait for the first archive record

WeeWX archives on five-minute boundaries. Wait up to six minutes, then run:

```bash
sudo sqlite3 -header -column \
  /var/lib/weewx/nodus.sdb \
  "select datetime(dateTime, 'unixepoch', 'localtime') as archive_time,
          inTemp, inHumidity, pressure, vpd
   from archive
   order by dateTime desc
   limit 1;"
```

At least one row must be returned. Observation columns vary by sensor family.

### 10.5 Open the dashboard

```text
http://<weewx-host>/weewx/nodus/
```

The page must show the intended device ID and current metric cards.

## Step 11: final checklist

- [ ] Intended Nodus uses `ACTIVE_PROFILE = "weewx"`.
- [ ] Broker-visible `nodus/<device-id>/data` JSON was captured.
- [ ] Automatic provisioning is disabled.
- [ ] Intended device is both **Installed** and **Discovered**.
- [ ] Unrelated devices are **Discovered** only.
- [ ] `weewx@nodus` is active.
- [ ] `/etc/weewx/nodus.conf` contains the intended data topic.
- [ ] Altitude includes `meter` or `foot`.
- [ ] The database contains an archive record.
- [ ] The dashboard opens and shows the intended device.

## Common problems

### Discovery-only refuses to run

It deliberately refuses when an operational Nodus is still installed, when
`/etc/weewx/nodus.conf` still exists, or when automatic provisioning is on.
Complete Step 4, then retry.

### More than one matching device is discovered

Leave automatic provisioning off. Use **Install Device** and explicitly select
the intended device. The manager will not automatically choose between
multiple matching-family discoveries.

### An unrelated Sensorius device appears as Discovered

Leave it alone. Do not remove it through the WeeWX manager because confirmed
removal clears its retained MQTT topics. A Discovered-only entry does not make
that device the operational WeeWX station.

### A wrong unrelated device was installed

First disable automatic provisioning. Do not use `/api/remove` if the wrong
device belongs to Sensorius, because `/api/remove` clears retained topics.

Use the local-only manager helper to remove the mistaken WeeWX files without
touching MQTT retained topics or the discovery registry. Replace the example
wrong ID before running:

```bash
sudo python3 -c '
import json, sys
sys.path.insert(0, "/etc/weewx/bin/user")
import nodus_manager_helper as helper
settings = json.load(open("/etc/weewx/nodus-discovery.json"))
print(json.dumps(helper._remove(settings, "avpd-zbcalz"), indent=2))
'
```

Then repeat Steps 5 through 10 and explicitly select the intended device.

### WeeWX fails with `list index out of range` in `StationInfo`

Check the altitude:

```bash
sudo grep -n 'altitude =' /etc/weewx/nodus.conf
```

The value probably lacks a unit. Rerun discovery-only bootstrap and enter a
value such as `1781, meter`.

### Removal shows a JSON decode traceback

Clearing a retained MQTT topic publishes an empty retained payload. An older
running MQTTSubscribe instance may log a JSON decode traceback while it is
being stopped. If removal reports zero retained-topic failures and the service
stops successfully, that historical traceback does not indicate a continuing
fault.

### `systemctl status` shows old failure lines

`systemctl status` includes recent historical log lines. Use the `Active:` line
to determine the current state, or run:

```bash
sudo systemctl is-active weewx@nodus
```

### The database is empty immediately after installation

That is normal until the next five-minute archive boundary. Wait up to six
minutes and check again.

### The dashboard returns 404 immediately after installation

The first report is generated after the first archive record. Wait for the
archive cycle, then reload the page.
