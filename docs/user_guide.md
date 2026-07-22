# cPyNodus II User Guide

cPyNodus II is firmware for Nodus environmental sensors and relay controllers.
Depending on its selected profile, a Nodus can operate through its own small
local web interface or as a headless MQTT device managed by Sensorius, WeeWX,
or Home Assistant.

This guide begins with a prepared device that can boot normally or enter AP
mode. It does not describe installing CircuitPython, building firmware,
flashing a board, or copying files to the device.

The screenshots show a CO2 Nodus with two switch channels. A sensor-only,
switch-only, or different sensor model will show only the controls and
calibration fields that apply to its hardware.

## AP Mode: Initial Setup and Network Recovery

Nodus enters access-point (AP) mode when:

- its saved Wi-Fi name or password is missing;
- its saved Wi-Fi name is still `Nodus_Setup`; or
- it cannot join the saved Wi-Fi network.

The factory/default AP details are:

| Setting | Default |
| --- | --- |
| Wi-Fi network | `Nodus_Setup` |
| Password | `password` |
| Setup page | `http://192.168.4.1:8000/setup` |

To configure the device manually:

1. Open Wi-Fi settings on a phone, tablet, or computer and join
   `Nodus_Setup` using `password`.
2. Remain connected if the operating system warns that the network has no
   internet access. The Nodus AP is a local setup network.
3. Open `http://192.168.4.1:8000/setup` in a browser. Enter the address
   manually if no captive-portal window appears.
4. Expand **Network** and enter the normal Wi-Fi SSID, password, and desired
   hostname.
5. Expand **Profile and MQTT** and select the intended operating profile. For
   an MQTT profile, also provide the broker settings.
6. Select **Save & Restart**. The setup AP disappears while Nodus restarts and
   tries to join the saved Wi-Fi network.
7. Reconnect the phone or computer to the normal network. A `nodusweb` device
   can then be opened by hostname or IPv4 address.

AP mode is a recovery state, not the normal long-running operating mode. Its
idle timer hard-resets the device after about 10 minutes. If the saved network
still cannot be joined, the setup AP appears again. Because the default AP
password is public, complete provisioning promptly and do not expose the AP
as a permanent network.

## Onboarding with Sensorius

Sensorius can automate AP-mode onboarding through **System Settings > Add
Device**. A typical flow is:

1. Start with the Nodus showing the `Nodus_Setup` AP.
2. Open **Add Device** in Sensorius and select **Add**.
3. Sensorius scans for and joins the setup AP. On macOS it can attempt the
   join automatically; if that is unavailable, follow its prompt to join
   `Nodus_Setup` manually and then return to Add Device.
4. Sensorius reads the Nodus identity and sends Wi-Fi, MQTT broker, hostname,
   and available time settings to the device.
5. Nodus saves the bootstrap values and restarts. Sensorius returns to the
   normal Wi-Fi network and waits for the device.
6. Nodus joins Wi-Fi and MQTT, announces itself to Sensorius, accepts its full
   configuration, and publishes its retained identity metadata.

The Add Device status rows distinguish joining the setup AP, sending the
bootstrap, rejoining normal Wi-Fi, and waiting for the Nodus to connect. Use
**Retry** if the current onboarding session does not finish. A Raspberry Pi
Sensorius host that onboards over Wi-Fi should have a reliable 2.4 GHz path;
an Ethernet-connected hub is usually more dependable when the router combines
2.4 GHz and 5 GHz under one network name.

After successful onboarding the Nodus normally runs in the `sensorius`
profile. That profile is intentionally headless: management, readings,
switches, calibration, and automation are presented by Sensorius rather than
by the device's local web pages.

## Opening NodusWeb

The local interface runs in normal station mode only when the active profile
is `nodusweb`. From a device on the same local network, open:

```text
http://<device-hostname>.local:8000
```

The example device used here is `http://co2-pmoopn.local:8000`. An IPv4
address can be used when `.local` name resolution is unavailable. If the HTTP
port was changed, use that configured port instead of `8000`.

The sidebar contains **Status**, **Setup**, **Calibration**, and **Nodus
Info**. **Switch Settings** and **Automations** also appear when switch
channels are enabled. On narrow screens the sidebar becomes a horizontal,
scrollable navigation row.

NodusWeb is intentionally small enough for a constrained CircuitPython
device. Pages are loaded independently and can occasionally return a temporary
`503 Service Unavailable` response when free memory is low. Wait for the
device to settle before retrying. NodusWeb does not retain sensor history or
provide graphs, 24-hour statistics, or CSV export.

## Status

![NodusWeb Status page with current sensor and switch values](<../assets/screenshots/nodusweb-status.png>)

Status is the main local operating page. It shows:

- the timestamp of the latest successful sensor sample;
- the current values for the configured display metrics; and
- the current state of each enabled switch channel.

The browser refreshes the visible values about every 15 seconds. Sensor
acquisition itself runs on the device's approximately 60-second NodusWeb
cadence, so several browser refreshes can legitimately show the same sample.
If a sensor read is still pending or temporarily fails, the prior successful
sample remains visible.

Select an available **ON** or **OFF** state button to toggle that switch. The
browser and device enforce a five-second guard between manual changes. When an
enabled local automation owns a channel, the state control is disabled and an
ownership label identifies the automation.

## Setup

Setup groups configuration into four independently expandable sections. The
screens below show one section expanded at a time.

### Network

![NodusWeb Setup Network section expanded](<../assets/screenshots/nodusweb-setup-network-expanded.png>)

- **SSID** is the normal Wi-Fi network name.
- **Password** is the Wi-Fi password and remains visually concealed.
- **Hostname** is the local device name used in addresses such as
  `http://co2-pmoopn.local:8000`.

Network changes require a restart. Check all three values before selecting
**Save & Restart**; an incorrect network name or password will send the device
back to AP mode.

### Time

![NodusWeb Setup Time section expanded](<../assets/screenshots/nodusweb-setup-time-expanded.png>)

- **Time Zone** is an IANA name such as `America/Denver`.
- **TZ Offset** is the local offset from UTC in seconds.
- **TZ Name** is the short displayed zone name, such as `MST` or `MDT`.
- **NTP Server** is the hostname used to synchronize the clock.
- **NTP Server IP** is the fallback address used when hostname resolution or
  hostname-based synchronization fails.

Select **Save** for an ordinary live time-setting update. Nodus requests a new
time synchronization after accepted time changes.

### Profile and MQTT

![NodusWeb Setup Profile and MQTT section expanded](<../assets/screenshots/nodusweb-setup-profile-and-mqtt-expanded.png>)

The profile selector offers `nodusweb`, `sensorius`, `weewx`, and
`homeassistant`. MQTT profiles use the shared fields:

- **Broker**: canonical MQTT broker hostname;
- **Username** and **Password**: broker credentials when required;
- **Port**: commonly `1883` without TLS or `8883` with TLS; and
- **Use TLS**: enables an encrypted broker connection.

`nodusweb` does not create an MQTT client, even if broker values remain in the
fields. Profile and MQTT changes take effect after **Save & Restart**. Once an
MQTT profile starts normally, the built-in web server is intentionally absent.

### Sensor Display

![NodusWeb Setup Sensor Display section expanded](<../assets/screenshots/nodusweb-setup-sensor-display-expanded.png>)

- **Location** is the friendly place name reported with the sensor.
- **Metric 1-6** select the preferred Sensorius display metrics.
- **Style 1-6** carry the preferred Sensorius display style for each slot,
  such as `Gauge`, `Graph6hr`, or `Graph24hr`.

These fields describe the six preferred integration display slots. The
NodusWeb Status page uses the configured slots as its compact current-value
list; it does not render the requested gauge or graph styles. Select **Save**
to apply live-safe location and display changes without intentionally
restarting the device.

## Calibration

![NodusWeb Device Calibration page](<../assets/screenshots/nodusweb-calibration.png>)

Calibration fields depend on the detected sensor family. The pictured CO2
device provides:

- **TEMP OFFSET**: additive temperature correction;
- **RH OFFSET**: additive relative-humidity correction;
- **CO2 OFFSET**: additive CO2 correction in ppm; and
- **ALTITUDE METERS**: sensor altitude compensation, not an additive pressure
  offset.

Other sensor families expose only their applicable temperature, humidity,
air-quality, gas, light, PPFD, plant, soil, pH, EC, or altitude values. Apply
small corrections only after comparison with a trusted reference, select
**Save**, and observe subsequent readings. **Restart Device** requests a
device restart and is not required for an ordinary live calibration save.

## Switch Settings

![NodusWeb Switch Settings page](<../assets/screenshots/nodusweb-switch-settings.png>)

Switch Settings shows the switch device's location, editable channel labels,
and a compact manual state toggle for each enabled channel. The toggle appears
before its shorter label field in each two-column channel card.

Use names that identify the actual load, such as Fan, Light, Irrigation Pump,
or Heat Mat. Select **Save** after changing the location or labels. Each state
toggle acts immediately and is separate from saving labels. An enabled
automation owns its target channel; disable that rule before attempting a
manual change.

## Automations

![NodusWeb automation editor with Example expanded](<../assets/screenshots/nodusweb-automations-example-expanded.png>)

Local automations run only in the `nodusweb` profile and use this Nodus's own
sensor sample and switch channels. The editor deliberately exposes compact
JSON rather than a large graphical rule builder:

- **Rule** selects an existing rule or starts a new one.
- **Rule ID** is the stable local identifier.
- **Enabled** controls whether a saved rule runs and owns its target channels.
- **Script JSON** contains the rule name, conditions, and actions.
- **Save** validates and stores the rule.
- **Delete** removes the selected saved rule.
- **Example** shows the compact shape of a time-based switch rule.

Supported conditions include local sensor comparisons with optional
hysteresis, time windows and weekdays, repeating timers, and OR separators.
Conditions within a group are ANDed; an OR separator starts another group.
Each action selects a local switch channel, an absolute On or Off state, a
delay from zero through 60 seconds, and either restoration of the previous
state or no action when the rule becomes false.

The device accepts at most 12 rules, 12 conditions per rule, four actions per
rule, and 1,536 serialized script bytes. Astral conditions and non-local
sensor or switch targets are rejected. Test rules with harmless loads before
allowing them to control pumps, heaters, valves, lights, or other critical
equipment.

## Nodus Info

Nodus Info is read-only. It has two independent expandable sections.

### Network Info

![NodusWeb Network Info section expanded](<../assets/screenshots/nodusweb-nodus-info-network-info-expanded.png>)

Network Info shows the firmware version, active profile, connected SSID,
current station IPv4 address, and the number of Wi-Fi recovery episodes during
the current boot. Use this page to confirm that the expected firmware and
profile actually started before troubleshooting an integration.

### Device Info

![NodusWeb Device Info section expanded](<../assets/screenshots/nodusweb-nodus-info-device-info-expanded.png>)

Device Info shows the logical sensor ID, detected hardware family, interface,
switch device ID, and enabled switch-channel count. The logical sensor ID is
the stable identity used in MQTT topics, while the hardware name identifies
the physical sensor family detected by Nodus.

## What to Expect from Each Profile

### NodusWeb

- Runs without an MQTT broker.
- Serves the local pages described in this guide.
- Supports local sensor/time/timer/AND-OR switch automations.
- Keeps only the latest successful sensor snapshot in memory; it does not
  provide historical graphs or exports.

### Sensorius

- Uses MQTT for readings, identity, configuration, calibration, switch
  control, log retrieval, and firmware-update preparation.
- Is normally provisioned through Sensorius Add Device while Nodus is in AP
  mode.
- Publishes retained device metadata and availability plus ongoing heartbeat,
  sensor, and switch messages for Sensorius to present.
- Does not run the normal NodusWeb server. Use the Sensorius dashboard and
  settings interfaces after onboarding.

### WeeWX

- Publishes the normal Nodus sensor-data payload through MQTT; there is no
  separate WeeWX-only firmware payload.
- Relies on the host-side Nodus WeeWX integration for collection, archive,
  reports, its richer dashboard, limited setup UI, and optional host-side
  switch automations.
- Does not run the normal NodusWeb server. Verify actual broker-visible MQTT
  data before diagnosing the WeeWX collector; serial-side publish messages
  alone are not proof that the broker received data.

### Home Assistant

- Publishes MQTT sensor and switch state plus retained Home Assistant discovery
  messages under the configured discovery prefix.
- Uses the shared MQTT broker settings and Home Assistant options from the
  device configuration.
- Does not run the normal NodusWeb server or local NodusWeb automations.
- Should be provisioned in AP mode or `nodusweb` first, then restarted into
  `homeassistant`. Later changes currently require supported MQTT management,
  direct settings maintenance, or returning the device to provisioning mode.

An unavailable local URL under `sensorius`, `weewx`, or `homeassistant` is
therefore expected behavior, not evidence that the device has failed.

## Operating Tips and Troubleshooting

- Pace page navigation and allow each response to finish. CircuitPython socket
  and heap resources are limited.
- If a page returns `503`, wait and retry once the device has recovered memory.
- If NodusWeb does not open, confirm that the active profile is `nodusweb`, the
  client is on the same network, and the HTTP port is correct.
- Try the IPv4 address from Nodus Info if the `.local` hostname does not
  resolve.
- If a reading looks stale, compare its timestamp with the device's
  approximately 60-second sensor cadence.
- If a switch cannot be changed, check the Status page for local automation
  ownership and wait at least five seconds between manual requests.
- If an MQTT profile appears offline, validate its Wi-Fi and broker settings,
  then confirm the expected topics at the broker.
- If saved network credentials cannot be used, reconnect to `Nodus_Setup` and
  repeat AP-mode provisioning.

## Related Documentation

- [Configuration reference](configuration.md)
- [AP onboarding overview](onboarding.md)
- [NodusWeb automation contract](automations.md)
- [MQTT overview](mqtt.md)
- [Sensorius contract](sensorius_contract.md)
- [WeeWX integration](weewx.md)
- [Architecture and recovery behavior](architecture.md)
- [OTA behavior](ota.md)
