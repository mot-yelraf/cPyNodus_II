# Lesson 3: Install CircuitPython and use the serial REPL

[Previous](02-pico2w-hardware.md) · [Index](README.md) · [Next: Deploy Nodus](04-deploy-and-first-observations.md)

## What you will learn

Install the Pico2 W interpreter, find its serial device, use `run_screen`, and
execute small hardware checks in the REPL. Allow 90 minutes. Complete Lessons
1–2 first. This lesson begins with a fresh classroom board; back up an existing
board before replacing its interpreter or application files.

The student/operator performs the USB copy steps. Automated agents do not
write to `/Volumes/CIRCUITPY` or deploy to the mounted board.

## 1. Flash CircuitPython 9.2.8

1. Open the official [Pico 2 W download page](https://circuitpython.org/board/raspberry_pi_pico2_w/).
   Use **Previous Versions of CircuitPython** to locate the **9.2.8** UF2 for
   `raspberry_pi_pico2_w`. The English filename is
   `adafruit-circuitpython-raspberry_pi_pico2_w-en_US-9.2.8.uf2`.
   The course deliberately uses the repository-verified version even when the
   page offers a newer default. Pico, Pico W, and Pico 2 without W are different
   download targets. This is CircuitPython, not a MicroPython image.
2. With power disconnected and the RW jumper open, hold **BOOTSEL**, connect
   the USB data cable, and release BOOTSEL when the bootloader volume appears.
   Pico 2 normally labels this volume **RP2350**. See the official
   [BOOTSEL instructions](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html).
3. Drag/copy the downloaded UF2 onto that bootloader volume in Finder or the
   Linux file manager. Wait for the copy and automatic reboot to finish. The
   bootloader volume disappears and **CIRCUITPY** appears. Do not copy the UF2
   onto the later CIRCUITPY filesystem.
4. Read `boot_out.txt` on CIRCUITPY and confirm CircuitPython **9.2.8** and the
   Pico 2 W identity. Record the result; no Nodus code has been deployed yet.

On macOS, CIRCUITPY normally mounts at `/Volumes/CIRCUITPY`. Linux desktops
commonly use `/media/LOGIN/CIRCUITPY` or `/run/media/LOGIN/CIRCUITPY`. The file
manager and `lsblk -o NAME,LABEL,MOUNTPOINTS` show the actual Linux mount.
A filesystem mount is where files are copied; a `/dev/` serial endpoint is
where the REPL is accessed. They are not interchangeable targets.

If no bootloader drive appears, check BOOTSEL timing and use a known data cable.
If only RP2350 reappears, check UF2 target and completed copy. A previously
installed Nodus `boot.py` can still affect USB behavior after interpreter
replacement; a UF2 reinstall is not a guaranteed clean filesystem reset.

## 2. Discover the serial endpoint

Compare devices before and after connecting the Pico. These `find` commands
avoid shell wildcard errors when there are no matching devices.

macOS:

```sh
find /dev -maxdepth 1 -name 'cu.usbmodem*' -print
```

Use the newly appearing `/dev/cu.usbmodem...` callout path, for example
`/dev/cu.usbmodem1334301`. Your suffix will differ.

Linux:

```sh
find /dev -maxdepth 1 -name 'ttyACM*' -print
ls -l /dev/serial/by-id/
```

The new endpoint might be `/dev/ttyACM0`; it can change with other USB devices.
A matching `/dev/serial/by-id/...` symlink is useful when available. Compare
before/after and inspect the symlink target rather than selecting an unrelated
serial device. The by-id directory is not present on every system.

For permission errors, inspect `ls -l /dev/ttyACM0` using the actual path and
`id -nG`. On Ubuntu/Debian the serial group is usually `dialout`; if confirmed,
an administrator can grant membership:

```sh
sudo usermod -a -G dialout "$USER"
```

Log out and back in, then verify membership. Other distributions can use another
group. Follow the local lab policy instead of changing device permissions to
world-writable. See Adafruit's
[Linux serial permissions guide](https://learn.adafruit.com/welcome-to-circuitpython/advanced-serial-console-on-linux).

## 3. Connect using run_screen

In the serial terminal tab, from the repository root, use your actual endpoint:

```sh
# macOS example; replace the path
bash scripts/run_screen /dev/cu.usbmodem1334301 115200
```

```sh
# Linux example; replace the path
bash scripts/run_screen /dev/ttyACM0 115200
```

Use only the block for your OS. The equivalent single connection is
`screen DEVICE_PATH 115200`, but the wrapper retries when the device disappears
and returns at the **same path**. If the path changes, stop the wrapper,
rediscover the port, and start it with the new path. Its source is
[scripts/run_screen](../../scripts/run_screen).

After Nodus deployment, Pico2 W enables both console and secondary data CDC
interfaces. You may see two serial endpoints. The REPL belongs to the console;
identify it by the CircuitPython banner/`>>>` prompt, not solely by endpoint
number. A silent secondary data endpoint is not evidence of a crashed board.
Only one serial program should own the console at a time; close any VS Code
serial-monitor connection before starting `screen`.

## 4. Read, evaluate, print, loop

Press **Ctrl-C** to interrupt running code. If prompted, press a key to enter
the REPL. `KeyboardInterrupt` is expected after an intentional interrupt.
At the `>>>` prompt, type these commands one line at a time without typing the
prompt itself:

```python
print("Hello from the Pico")
2 + 3
import sys, os, board
print(sys.implementation)
print(board.board_id)
print(os.listdir("/"))
import gc
gc.collect()
print(gc.mem_free())
```

`5` is the arithmetic result; the other output depends on your board and files.
`board.board_id` should identify the Pico 2 W. This Python runs on the Pico,
not in your laptop's `.venv`. REPL variables are temporary and are lost on reload.
`dir(board)` can show available pin names. Shell commands such as `ls` belong
in the other terminal tab, not at `>>>`.

| Keys | Meaning |
| --- | --- |
| Ctrl-C inside the serial console | Interrupt the board program or cancel the current input |
| Ctrl-D at `>>>` | Soft reload and run the board application again |
| Ctrl-E at `>>>` | Enter paste mode for an indented multiline snippet |
| Ctrl-D while in paste mode | Execute that pasted snippet; this differs from Ctrl-D at `>>>` |
| Ctrl-A, then K, then Y | Tell screen to kill its current window |

These are the normal CircuitPython interactive controls described in the
[REPL guide](https://learn.adafruit.com/welcome-to-circuitpython/the-repl).
Ctrl-D does not rerun Nodus `boot.py`; power-cycle after changing the RW jumper.
Interrupting Nodus also stops its normal sensor, switch-control, and networking
work, so use the REPL only on the classroom fixture while observing outputs.

## 5. Check BME280 wiring before deploying Nodus

The following uses built-in `busio`; a BME280 driver is not yet needed. On the
fresh board, enter paste mode with Ctrl-E, paste the block, then Ctrl-D:

```python
import board
import busio

i2c = busio.I2C(board.GP1, board.GP0)
try:
    if i2c.try_lock():
        try:
            print([hex(address) for address in i2c.scan()])
        finally:
            i2c.unlock()
    else:
        print("I2C lock unavailable; stop and inspect before retrying")
finally:
    i2c.deinit()
```

Expect `['0x76']` with only the course BME280 attached. An empty list suggests
power, pin, pullup, or soldering problems. `0x77` means the address selector
needs checking. Disconnect power before rewiring. A scan proves an address
responds; Nodus driver reads in Lesson 4 establish usable BME280 data.

Optional S1 LED check, still before Nodus owns the pins: type one line at a time,
observe On, then set Off and release the pin:

```python
import digitalio
s1 = digitalio.DigitalInOut(board.GP28)
s1.switch_to_output(value=False)
s1.value = True
s1.value = False
s1.deinit()
```

Do not reuse these direct hardware probes while Nodus owns the same bus/pins.
If interrupted Nodus leaves a resource in use, return to a planned fresh probe
session instead of creating duplicate bus or output objects.

## 6. Leave the console or capture a session

`run_screen` reconnects even after a deliberate screen exit. To stop it, press
Ctrl-A, K, confirm Y, then immediately press Ctrl-C during its reconnect delay.
Alternatively unplug the board after writes finish, then Ctrl-C while the
wrapper reports that it is waiting. Ctrl-C while attached goes to CircuitPython
and is not the wrapper's exit command. Ctrl-A, D detaches screen and can leave
an existing session holding the port; use the kill sequence for these lessons.

For an optional capture, Ctrl-A then Shift-H toggles screen logging to its
current logfile, normally `screenlog.0` in the launch directory. The wrapper
starts a new screen session after reconnects, so re-enable logging as needed.
It does not automatically create a `~/cu.usb*.log` capture. Redact credentials
before sharing logs and confirm where the file was written.

Adafruit documents a macOS `screen` flow-control caveat: after disconnecting,
CircuitPython output can stall until the console reconnects. Keep the console
attached during observations and reconnect before treating a post-exit stall
as a firmware failure. See its
[Mac serial guide](https://learn.adafruit.com/welcome-to-circuitpython/advanced-serial-console-on-mac-and-linux).

## Acceptance and reference observations

Submit the UF2/version identity, filesystem mount path, serial path, REPL
arithmetic result, I2C address, and optional S1 observation. Demonstrate a clean
exit from `run_screen` and a reconnect. Explain why RP2350, CIRCUITPY, and the
serial endpoint each serve a different purpose. Hardware results must be
observed, not copied from the expected-output examples.
