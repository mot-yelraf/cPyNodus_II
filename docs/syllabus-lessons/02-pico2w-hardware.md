# Lesson 2: Pico2 W, BME280, S1, and the RW pin

[Previous](01-development-toolchain.md) · [Index](README.md) · [Next: CircuitPython and REPL](03-circuitpython-and-repl.md)

## What you will learn

Wire the course fixture, distinguish physical pins from GPIO names, and explain
ROFS/RWFS filesystem ownership. Allow 60–90 minutes with a wiring inspection.
Use the Pico 2 W only; the XIAO implementation is an extra-credit port.

Read the repository [pinout](../pinout.md),
[board profile](../../cpynodus_ii/core/board_profile.py),
[boot.py](../../boot.py), and [configuration contract](../configuration.md).
The table below covers the I2C course fixture, not the RS485 HAT wiring.

## Parts and power

Use a Pico 2 W with soldered headers, a USB data cable, breadboard, jumpers,
a 3.3 V-compatible BME280 breakout configurable to address `0x76`, a 1 kΩ
resistor for S1 enable, and an instructor-approved S1 output fixture. A red LED
and 1 kΩ series resistor provide a simple first output; an active-high,
3.3 V logic-compatible relay module can demonstrate switching afterward.
A BMP280 lacks humidity and is not the BME280 required for the lessons.

Unplug USB and any other supply before rewiring. Orient the board using its
printed labels and the [official Pico documentation](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html).
Physical pin 1 is GP0; physical pin 36 is 3V3(OUT). Use 3V3(OUT) for this
sensor, not VBUS or VSYS. All connections use a common ground.

## 1. Connect the BME280 on I2C bus 0

| Pico signal | Physical pin | BME280 connection |
| --- | --- | --- |
| 3V3(OUT) | 36 | Breakout's documented 3.3 V supply input, often VIN/VCC |
| GND | 3 | GND |
| GP0 | 1 | SDA, sometimes labeled SDI |
| GP1 | 2 | SCL, sometimes labeled SCK |
| GND | 3 or the same ground rail | SDO/address-select when required for `0x76` |

Select I2C mode according to the breakout's documentation. Its SDA/SCL pullups
must go to 3.3 V. Check whether they are already fitted before adding more.
For the Adafruit BME280 breakout, SDO-to-GND or the closed ADDR jumper changes
the default `0x77` address to `0x76`; see its
[pinout guide](https://learn.adafruit.com/adafruit-bme280-humidity-barometric-pressure-temperature-sensor-breakout/pinouts).
Other breakouts may need CS held high for I2C; follow that board's schematic.

This address is significant to Nodus: its current factory probe maps `0x76` to
logical device `avpd` and `0x77` to `aqi` (the BME680 path). The classroom BME280
must therefore scan at `0x76` before first bootstrap. Do not assume that any
responding address proves the correct chip was identified. Lesson 3 includes
an I2C scan; `0x76` is decimal `118` in the generated configuration.

## 2. Connect S1 enable and output

| Purpose | Pico physical pin | Connection |
| --- | --- | --- |
| S1 enable, GP5 | 7 | Through a 1 kΩ resistor to GND, pin 8 |
| S1 control, GP28 | 34 | LED series resistor or compatible relay module IN |
| S1 output ground | 33 (AGND) | LED cathode or fixture ground; shared board ground |

The enable connection declares that S1 hardware exists at boot. It is distinct
from the GP28 signal that turns the output on and off. Leave S2 unconfigured.

**Use the resistor on GP5, not a permanent direct short.** Settings probes this
pin as an input with a pull-up, but the current switch adapter later configures
it as an output and the service drives it high. A 1 kΩ path pulls the boot
input low and limits current to about 3.3 mA when driven high. This is based on
[switch_adapter.py](../../cpynodus_ii/hardware/switch_adapter.py) and
[switch_service.py](../../cpynodus_ii/features/switch_service.py); it explains
how to implement the pinout's “grounded at boot” requirement on a breadboard.

For the LED fixture, connect GP28 → 1 kΩ resistor → LED anode, then LED cathode
→ ground. On means GP28 high. For a relay fixture, connect GP28 to its IN and
common ground, and power the module from its specified supply. The module must
include a driver and accept 3.3 V high as On; a bare relay coil must not connect
to a GPIO. Do not assume a 5 V or active-low module is compatible. Start with
unloaded contacts; any later contact-side demonstration uses an instructor's
low-voltage source and load. Mains wiring is outside this course.

## 3. Install the RW mode selector

Connect a removable jumper or SPST switch between **GP14, physical pin 19**, and
**GND, physical pin 18**. Open is host-edit mode; closed is application-write
mode. GP14 has an internal pull-up in Nodus `boot.py`; do not wire it to 5 V.
Leave the factory-reset pin GP17 unconnected for normal lessons.

ROFS means read-only filesystem **from the application's perspective**. RWFS
means read/write filesystem **from the application's perspective**:

| GP14 when Nodus boot.py runs | Mode | Host CIRCUITPY drive | Firmware writes | USB serial/REPL |
| --- | --- | --- | --- | --- |
| Open/high | ROFS: host editing/deployment | Visible and host-writable | Read-only | Available |
| Connected to GND/low | RWFS: normal writable runtime | Hidden | Allowed | Available |

Firmware writes include factory-created TOML files, saved onboarding settings,
switch state, writable recovery logs, and OTA staging. ROFS can run an already
configured device, but it is unsuitable for first-boot creation or a lab that
needs durable settings. A live state change is not proof that it was saved.

This policy belongs to **Nodus boot.py**. A freshly installed CircuitPython UF2
without that file does not yet use GP14 to select these modes. Once Nodus is
installed, the hidden drive in RWFS is expected, not a failed USB connection.
The policy avoids simultaneous host and firmware writes to the same filesystem.

## 4. Change modes correctly

Finish writes, safely eject the visible drive, unplug power, change the jumper,
and reconnect USB. Use a full power cycle so `boot.py` reads GP14 again.
Ctrl-D reloads application code but does not rerun `boot.py` to reconfigure USB.
Changing the jumper while the program is running does not remount the filesystem.

For deployment: GP14 open → full boot → visible drive → copy firmware.
For first bootstrap/onboarding: finish/eject → unplug → GP14 grounded → full boot
→ watch serial while firmware creates/saves its configuration.
For later host edits: finish runtime work → unplug → GP14 open → full boot.
Keep S1's enable resistor connected in both modes.

After deploying Nodus in Lesson 4, you can also inspect the application mount
from the serial REPL using the controls taught in Lesson 3:

```python
import storage
print(storage.getmount("/").readonly)
```

Expect `True` in ROFS and `False` in RWFS. This inspects the mount without
writing a test file. Entering the REPL interrupts the application; restore
normal operation afterward. Do not use `storage.remount` to bypass the selector
or enable simultaneous host/application writes.

## Acceptance and debugging

Submit an inspected wiring table/photo and your predicted drive visibility in
each mode. Identify GP0/GP1, GP5/GP28, and GP14 without confusing GPIO numbers
with physical positions. Verify power/ground and resistor values before USB power.

If S1 is absent after deployment, check enable sensing and generated
`switch.toml`; later boots still require that live file. If CIRCUITPY disappears
only after grounding GP14, reconnect the serial console instead of reflashing.
Explain why ROFS can allow laptop edits while denying firmware persistence.
