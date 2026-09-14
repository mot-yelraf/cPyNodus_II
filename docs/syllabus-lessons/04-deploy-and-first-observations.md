# Lesson 4: Deploy Nodus and collect the first observations

[Previous](03-circuitpython-and-repl.md) · [Index](README.md) · [Next: Startup](05-startup-and-configuration.md)

## What you will learn

Build target-specific firmware, deploy it to CIRCUITPY, change filesystem modes,
identify the node, find its network address from the REPL, and confirm BME280
readings and S1 control. Allow 90–120 minutes after Lessons
1–3. Read [configuration](../configuration.md) and the [user guide](../user_guide.md).

Flashing the CircuitPython UF2 in Lesson 3 installs the interpreter. Running
[deploy_nodus.sh](../../scripts/deploy_nodus.sh) copies the Nodus application,
entrypoints, templates, and drivers onto its filesystem. It does not install
or replace the CircuitPython interpreter. All device copying below is performed
by the student/operator, not an automated agent.

## 1. Build the Pico2 W application

In the host terminal, activate the course environment and set the compiler and
bundle paths from Lesson 1. From the repository root:

```sh
bash scripts/nodus_mpy.sh --target pico2w \
  --compiler "$MPY_CROSS_PICO2W" \
  --lib-source "$NODUS_LIB_SOURCE"
cat build/firmware/pico2w/BUILD_INFO
```

Confirm the metadata identifies `pico2w`, CircuitPython `9.2.8`, the matching
compiler, and the intended library source. The build creates
`build/firmware/pico2w/cpynodus_ii/` with `.mpy` modules and stages drivers in
`build/firmware/pico2w/lib/`. Keep the complete set, not only the BME280 driver.

If the compiler or a library entry is missing, stop and correct the course
inputs. `pip install` of a sensor package on the laptop does not populate the
board's driver directory. Explicitly build again after runtime source edits.
The deploy script can automatically rebuild stale artifacts, but that rebuild
uses its default library lookup; it does not forward your earlier custom
`--lib-source`. Building explicitly immediately before deployment avoids this
local-path surprise. A dry run that says it would rebuild has not validated or
produced those missing compiled files.

## 2. Select host-edit mode and the real mount

Finish any writes and power off. **Open the GP14-to-GND jumper**, leave S1's
GP5 resistor connected, and reconnect USB without BOOTSEL. On a fresh board the
Nodus mode policy takes effect only after its `boot.py` has been copied and a
full boot occurs. On a previously deployed board this selects ROFS: the host
can write the visible CIRCUITPY drive and the application cannot.

Use the actual mount found in Lesson 3. Set one of these in the host terminal:

```sh
# macOS
NODUS_MOUNT="/Volumes/CIRCUITPY"
```

```sh
# Ubuntu/Debian desktop example; verify your actual path first
NODUS_MOUNT="/media/$USER/CIRCUITPY"
```

Some Linux desktops use `/run/media/$USER/CIRCUITPY`. Verify with the file
manager or `lsblk -o NAME,LABEL,MOUNTPOINTS`. Confirm the mounted board identity:

```sh
cat "$NODUS_MOUNT/boot_out.txt"
```

Do not create a plain folder at a guessed mount path. The script's name guard
checks for `CIRCUITPY` in the target path; it is not proof that a board is mounted.
Do not use `--force` to bypass an incorrect path. Back up live TOML files and
needed logs from an existing board before deployment.

## 3. Preview and deploy

Keep the application stopped at the serial REPL during copying. On a stock
first installation, file writes may trigger CircuitPython autoreload; use
`import supervisor` then `supervisor.runtime.autoreload = False` in the REPL
before copying. Nodus itself disables runtime autoreload after installation.

Run the preview from the host terminal and inspect the destination and contents:

```sh
bash scripts/deploy_nodus.sh --target "$NODUS_MOUNT" \
  --content pico2w-mpy --dry-run
```

Once the preview and build are correct, the operator performs the copy:

```sh
bash scripts/deploy_nodus.sh --target "$NODUS_MOUNT" \
  --content pico2w-mpy
sync
```

This mode copies root startup `.py` files, board templates, compiled package
modules, and staged libraries; it removes matching package `.py` files that
would shadow `.mpy` imports. It preserves active settings/sensor/switch TOML
files. Postmortem logs are removed by default; add `--keep-reboot-log` when
those records must survive the deployment.

The `runtime` content option is for source deployment and does not manage
libraries. The `full` default has a broader scope. Use the explicit
`pico2w-mpy` mode for the course. There is no need for `--delete` here.

## 4. Enter RWFS for first bootstrap

Wait for copying to finish, safely eject CIRCUITPY in the file manager, unplug
power, **close the GP14-to-GND jumper**, and reconnect. This full boot runs
Nodus `boot.py`: the drive is hidden, the firmware filesystem is writable, and
serial remains available. Reconnect `run_screen` using the discovered console
endpoint; two CDC endpoints may now appear.

Watch the firmware version, `RWFS`, profile, sensor, and switch startup events.
On a fresh writable deployment with correct wiring, bootstrap should create:

- `settings.toml` from the shared template;
- `sensor_i2c.toml` for the BME280 as logical `DEVICE = "avpd"`;
- `switch.toml` for enabled S1, including generated channel identity.

Existing live files are the source of truth on later boots. If an old board
has stale configuration, inspect/update it deliberately; changing a template
or redeploying will not automatically reset it. ROFS skips write-based factory
bootstrap, so a first boot in ROFS is not a valid onboarding success test.

## 5. Understand DEVICE, serial number, and device ID

These names answer different questions: what kind of device is this, which
instance is it, and how do we address it? The precise TOML keys matter. The
serial-number concept is stored as `SERIAL_NUM` for the sensor and
`DEVICE_SERIAL_NUM` for the switch, not `SERIAL_NUMBER`. Device identity uses
`SENSOR_ID` and `SWITCH_DEVICE_ID`; `DEVICD_ID` is a misspelling, not a setting.
`DEVICE_ID` in a command example is a placeholder for the effective device ID,
not a generic TOML key to add to `settings.toml`.

The following is an **illustrative fresh-bootstrap example** with suffix
`k7m2q9`. Read your own generated values rather than copying these identities:

| Meaning | File, section, and key | Example | How it is used |
| --- | --- | --- | --- |
| Sensor type | `sensor_i2c.toml`, `[Sensor] DEVICE` | `avpd` | Selects the logical sensor behavior; BME280 is the physical hardware family |
| Sensor serial number | `sensor_i2c.toml`, `[Sensor] SERIAL_NUM` | `k7m2q9` | Persisted six-character suffix seeded during factory bootstrap |
| Sensor identity | `sensor_i2c.toml`, `[Sensor] SENSOR_ID` | `avpd-k7m2q9` | Identifies this sensor in data and MQTT topics |
| Switch type | `switch.toml`, `[Switch] DEVICE` | `switch` | Identifies the switch device class |
| Switch serial number | `switch.toml`, `[Switch] DEVICE_SERIAL_NUM` | `k7m2q9` | Normally shares the node's bootstrap suffix |
| Switch device identity | `switch.toml`, `[Switch] SWITCH_DEVICE_ID` | `switch-k7m2q9` | Identifies the node's switch component |
| S1 identity | `switch.toml`, `[Switch] SWITCH_1_CHANNEL_ID` | `S1-k7m2q9` | Addresses the individual output channel |
| Network hostname | `settings.toml`, `[Network] HOSTNAME` | `avpd-k7m2q9` | Network name; NodusWeb advertises it through mDNS when available |
| Setup AP name | `settings.toml`, `[Network] AP_SSID` | `Nodus-k7m2q9` | Wi-Fi network name used during AP provisioning |

The serial suffix is generated by Nodus; it is not the USB `/dev/` suffix,
BME280 I2C address, MAC address, or immutable chip serial. Fresh bootstrap shares
it across the sensor, switch, and AP identities and normally seeds HOSTNAME
from SENSOR_ID for this combined fixture. Existing configured identities and
custom hostnames can differ. See identity seeding in
[settings.py](../../cpynodus_ii/core/settings.py).

For device-wide metadata and commands, the runtime chooses the first nonempty
value in this order: sensor ID, switch device ID, then network hostname. Thus
this BME280/S1 example's effective `device_id` is `avpd-k7m2q9`, while a command
to its individual switch channel uses `S1-k7m2q9`. This selection is visible in
[payloads.py](../../cpynodus_ii/features/payloads.py) and the
[Sensorius contract](../sensorius_contract.md). A friendly label such as “Fan”
is not a channel ID. Preserve generated IDs during ordinary deployment; copying
another node's live identity files can cause topic/name collisions.

`DEVICE_IP` is also a placeholder, this time for the **current IPv4 address**.
It is not an identity key to save in TOML. DHCP can assign a different IP after
reconnection even though the serial number, device ID, and hostname remain the
same. Do not confuse it with `MQTT.BROKER_IP`, which addresses the broker.

## 6. Find DEVICE_IP and HOSTNAME through the REPL

Connect `run_screen` as in Lesson 3. You can inspect the generated hostname
and identities before provisioning. On a fresh device still in AP mode, the
station IP is not assigned yet; use the AP address for setup, then repeat the
station-IP check after the Wi-Fi join in section 7. With the safe S1 fixture
Off, press Ctrl-C and enter the `>>>` REPL.
These commands inspect the radio without initiating a new connection:

```python
import wifi
print("Station connected:", wifi.radio.connected)
print("DEVICE_IP:", wifi.radio.ipv4_address)
print("Radio hostname:", wifi.radio.hostname)
print("AP IPv4:", wifi.radio.ipv4_address_ap)
```

The station address is the value to substitute for `DEVICE_IP` in the normal
NodusWeb URL. `ipv4_address_ap` refers to the separate AP interface. An unset
station address (`None`) is not a usable URL; the device may be in AP mode,
not yet connected, or the interruption may have changed network state. Do not
manually call `wifi.radio.connect()` to create a second startup path. Resume
Nodus and inspect its join log before retrying. These properties are documented
in the [CircuitPython 9.2 Wi-Fi API](https://docs.circuitpython.org/en/9.2.x/shared-bindings/wifi/index.html).

Nodus attempts to set the radio hostname from the configured HOSTNAME, but
that assignment is best-effort. The persisted `[Network] HOSTNAME` is the value
to check if the radio reports a default or unexpected name. Read selected fields
without printing Wi-Fi/MQTT passwords: press Ctrl-E, paste this helper and its
calls, then Ctrl-D to execute the pasted block.

```python
def show_fields(path, section, keys):
    """Print selected assignment lines from a generated Nodus TOML section."""
    active = False
    with open(path, "r") as config_file:
        for line in config_file:
            text = line.strip()
            if text.startswith("["):
                active = text == "[{}]".format(section)
            elif active and "=" in text:
                key = text.split("=", 1)[0].strip()
                if key in keys:
                    print(text)


show_fields("/settings.toml", "Network", ("HOSTNAME", "HTTPPORT", "AP_SSID"))
show_fields("/sensor_i2c.toml", "Sensor", ("DEVICE", "SERIAL_NUM", "SENSOR_ID"))
show_fields(
    "/switch.toml", "Switch",
    ("DEVICE", "DEVICE_SERIAL_NUM", "SWITCH_DEVICE_ID", "SWITCH_1_CHANNEL_ID"),
)
```

This small reader displays assignment lines in the generated files; it is not
a general TOML parser and does not initialize services or alter configuration.
It works through the REPL in ROFS or RWFS, including when RWFS hides the drive
from the laptop. If a file is missing, return to the bootstrap checks rather
than inventing IDs. Do not use `os.getenv("HOSTNAME")` to read this nested
`[Network]` field; use the actual file section shown above.

For illustration, a station IP of `192.168.1.42`, HOSTNAME of `avpd-k7m2q9`, and
HTTPPORT of `8000` produce these two addresses:

```text
http://192.168.1.42:8000/setup
http://avpd-k7m2q9.local:8000/setup
```

Replace all three values with yours. Add `.local` when constructing the mDNS
URL; it is not part of the generated HOSTNAME. Name resolution depends on mDNS
support and the local network. A hostname that fails to resolve does not prove
the numeric IP is unreachable. AP provisioning still uses the AP address,
normally `http://192.168.4.1:8000/setup`, while connected to the Nodus AP.

**Resume before trying HTTP:** Ctrl-C interrupted the application and its web
service. At the normal `>>>` prompt, press Ctrl-D to reload Nodus and wait for
the web service to be ready in its current mode: AP for initial provisioning,
or station if already configured. Confirm the reported IP again after station
join; it can change on reconnection. A power cycle is needed only if you also changed the GP14 mode
selector. Record the IP and hostname alongside the sensor and switch IDs in
your lab notebook, marking the station IP as pending if not yet provisioned.
Continue with section 7, then collect the live observations in section 8.

## 7. Provision the local profile

With RWFS active and no valid station credentials, join the generated
`Nodus-<suffix>` access point; the factory default password is `password`.
Existing devices may have another configured AP name/password. Visit
`http://192.168.4.1:8000/setup` and follow the user guide to save the classroom
Wi-Fi credentials with `Profile.ACTIVE_PROFILE = "nodusweb"`.

Let the save/reboot finish and return the laptop to the classroom network.
Repeat the station-IP and hostname checks from section 6 if needed, then resume
Nodus before opening its web UI. Use the station IP reported in serial, or
`http://HOSTNAME.local:8000/setup` where mDNS works. Substitute the configured
HTTP port if different. Keep GP14 grounded for saved configuration and S1 state.
If the UI reports persistence failure, verify RWFS before retrying the save.

## 8. Observe BME280 and S1

1. Open the NodusWeb status page. Record Temperature (°C), Rel-Humidity (%),
   Baro-Pressure (hPa), sensor ID, and sample timestamp. The humidity reading
   helps confirm this is the required BME280 rather than a BMP280.
2. Make one bounded API request from the host, substituting the real IP:

   ```sh
   curl -sS --max-time 15 http://DEVICE_IP:8000/current-data
   ```

3. Wait at least the normal 60-second NodusWeb sampling interval and request
   again. Correlate responses with serial. Requesting cached data does not force
   a fresh sensor read, and equal measured values can still be a new sample.
4. With no automation owning S1 and only the safe classroom fixture attached,
   toggle S1 On on the status page, observe its physical output, wait at least
   five seconds, then set Off. The default generated label can be “Fan”; identify
   the channel by S1 and its generated ID, not solely its display label.
5. To inspect saved files, finish activity, power down, open GP14, and boot into
   ROFS. Read the generated configs on CIRCUITPY. Confirm GP1/GP0 and address
   `118`, plus `SWITCH_1_ENABLE_PIN = "GP5"`, `SWITCH_1_PIN = "GP28"`, and a
   populated S1 channel ID. Restore RWFS with another full power cycle afterward.

## Acceptance, troubleshooting, and reflection

Submit build metadata, deploy result, ROFS/RWFS observations, redacted boot
excerpt, your identity table, REPL-discovered IP/hostname and resulting URL,
two sensor responses, S1 On/Off evidence, and final safe output state.
Pass when the correct runtime is installed, Nodus detects BME280/S1, persistence
is observed in RWFS, and the normal profile is `nodusweb`.

| Symptom | First checks |
| --- | --- |
| No CIRCUITPY drive after deployment | GP14 low intentionally hides it; open and fully power-cycle for host edits |
| No generated configs or failed Save | Confirm application RWFS, not just host-write access |
| BME280 detected as aqi | Address is likely `0x77`; inspect selector and any previously generated config |
| Missing S1 | GP5 enable resistor, live switch.toml, channel pins and identity |
| MPY import error | Target/compiler/runtime match and complete staged dependencies |
| No station IP in the REPL | AP versus station mode, join progress, and application interruption; resume and recheck serial |
| HOSTNAME.local does not resolve | Confirm persisted HOSTNAME, mDNS support, same LAN, and current numeric station IP |
| Silent screen session | Console versus data CDC, changed device path, or another program holding the port |

Explain why copying application files does not flash the interpreter, why the
RW jumper must change before a full boot, and why a successful web response
alone does not prove fresh sensor data or persisted switch state.

Explain how DEVICE, serial number, effective device ID, S1 channel ID, HOSTNAME,
and DEVICE_IP differ. Which values normally stay stable when DHCP changes the
node's address, and which ID would you use to address S1?
