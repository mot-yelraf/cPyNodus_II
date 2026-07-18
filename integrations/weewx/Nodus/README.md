# Nodus WeeWX skin files

This directory is the complete `Nodus` WeeWX skin bundle. It arranges
metric cards alphabetically, omits cards whose current observation is
unavailable, and reloads the generated report in the browser every 60 seconds.
The centered `Nodus AI` title and system-settings gear are followed by the latest
data timestamp and Skyfield-backed Sun Position and Moon Phase cards. The
cards use the station coordinates, pinned Astral 3.2 and Skyfield 1.54, and a
locally cached DE421 ephemeris; report generation does not fetch astronomy
data from the network. Clicking the position card opens a full-width 29-day
Sun/Moon position and lunar-phase graph; clicking the expanded graph closes
it. Device identity, firmware, sensor
setup gear, and sensor description are centered above the metric cards.
The template versions its stylesheet and dashboard-script URLs so an upgraded
installation cannot combine new report markup with stale browser assets.
The dashboard and both setup surfaces use the shared Nodus `N` SVG favicon.
It shows every switch discovered from retained Nodus metadata, including its
label, current state, and recent events. Automated switch cells are green
regardless of their current ON/OFF state. Neutral manual cells can be clicked
to toggle the switch, with a five-second guard and an optional persisted
one-shot countdown. The switch column shows the label and channel ID. The event
area is five lines high and scrolls through up to twenty retained entries.
Event lines use `<timestamp> <rule name> : <state>`. When the optional Nodus automation
service is enabled, it also shows each
configured switch rule's channel state, current decision, last confirmed
action, and error status.

The generated metrics dashboard and its setup page share the visible
`/weewx/nodus/` URL tree. Sensor and switch setup gears open `setup/#sensor`
and `setup/#switch`; the page uses Sensorius-style sensor and switch sidebars
with a Dashboard return button. Sensor views cover Location, Device
Calibration, and device/network information. Switch views cover location and
channel labels, saved automations and their editor, and switch/network
information. There is no System Calibration view. The switch-status service
supplies its unauthenticated LAN API on TCP port 8767. The UI supports
locations, switch labels, change-only calibration writes, and bounded host-side
automations with AND/OR metric, timer, time/day, Astral, and switch-state
conditions plus multiple switch actions. See `docs/weewx.md` for its limits,
LAN exposure, and MQTT ACL requirements.
Automation-card titles show `<automation name> : Enabled|Disabled`; disabled
rules are display-only and do not control a channel.

The system gear opens the persistent port-8768 manager. Its two menus are
System Settings and Remove Device. It stores host settings in
`/var/lib/weewx/nodus_system.toml`, displays explicit Installed/Discovered
badges and live online/offline state, clears exact retained device/switch
topics during confirmed removal, and reprovisions a returning device.

The installer stores reusable operator answers in
`integrations/weewx/<device_id>.toml`. These mode-`0600` profiles are ignored
by Git because they can contain an MQTT password.

For a fresh installation, copy `index.html.tmpl`, `skin.conf`, `style.css`,
`dashboard.js`, and `nodus-favicon.svg`
into the host's `Nodus` skin directory. When updating a customized
installation, keep its existing `style.css` unless the supplied default style
is wanted.

On a package-installed WeeWX host the destination is commonly:

```text
/etc/weewx/skins/Nodus/
```

After copying the files, restart WeeWX or run the `Nodus` report manually
from a directory readable by the `weewx` service account.

See `docs/weewx.md` for complete Nodus provisioning, MQTTSubscribe, schema,
report, transfer, validation, and troubleshooting instructions.
