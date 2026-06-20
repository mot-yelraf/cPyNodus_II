# XIAO ESP32-S3 CircuitPython Bring-Up

This note describes how to take a Seeed Studio XIAO ESP32-S3 or XIAO
ESP32-S3 Sense from factory state to a Mac-mounted CircuitPython volume at
`/Volumes/CIRCUITPY`.

Verified `xesp32s3` runtime for cPyNodus_II:

- Board family: Seeed Studio XIAO ESP32-S3 / XIAO ESP32-S3 Sense
- CircuitPython: `10.2.1`
- Expected mounted runtime volume on macOS: `/Volumes/CIRCUITPY`

References:

- CircuitPython board page:
  <https://circuitpython.org/board/seeed_xiao_esp32s3/>
- CircuitPython 10.2.1 UF2:
  <https://downloads.circuitpython.org/bin/seeed_xiao_esp32_s3_sense/en_US/adafruit-circuitpython-seeed_xiao_esp32_s3_sense-en_US-10.2.1.uf2>
- CircuitPython 10.2.1 BIN:
  <https://downloads.circuitpython.org/bin/seeed_xiao_esp32_s3_sense/en_US/adafruit-circuitpython-seeed_xiao_esp32_s3_sense-en_US-10.2.1.bin>
- Adafruit ESP32 CircuitPython install guide:
  <https://learn.adafruit.com/circuitpython-with-esp32-quick-start/installing-circuitpython>
- Seeed XIAO ESP32-S3 getting started and pin map:
  <https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/>

## Important Constraints

- Do not copy Nodus firmware until the board mounts as `/Volumes/CIRCUITPY`.
- Use a data-capable USB-C cable. Charge-only cables are a common failure mode.
- Use the board's `Boot` button to enter ESP32-S3 ROM boot mode when serial
  flashing is required.
- The Seeed pin map identifies `Boot` as GPIO0 boot mode and `Reset` as
  `CHIP_PU`.
- CircuitPython 10.2.1 is the stable XIAO ESP32-S3 target for this project.
- The current local `xesp32s3-mpy` deploy requires a CircuitPython `10.2.1`
  `mpy-cross`. The build script intentionally rejects a `9.2.8` compiler for
  this target.

## Required Mac Tools

Recommended:

- Chrome or another Web Serial-capable Chromium browser for browser flashing
- A terminal for checking mount and serial state

Optional command-line fallback:

```bash
python3 -m pip install --user esptool
```

## Confirm the Factory State

1. Disconnect the XIAO from external circuits.
2. Plug the XIAO into the Mac over USB-C.
3. Check whether CircuitPython is already installed:

   ```bash
   ls /Volumes/CIRCUITPY
   ```

4. If `/Volumes/CIRCUITPY` exists, the board already boots CircuitPython.
   Continue to [Verify CircuitPython](#verify-circuitpython).
5. If `/Volumes/CIRCUITPY` does not exist, continue to installation.

## Installation Path A: UF2 Bootloader Available

Use this path only if the board exposes a USB mass-storage bootloader volume
after a bootloader-button sequence.

1. Download the CircuitPython `10.2.1` UF2 from the CircuitPython board page or
   from the direct UF2 link above.
2. Put the board into bootloader mode. Try double-clicking `Reset`; if that does
   not expose a bootloader volume, hold `Boot`, tap `Reset`, then release
   `Boot`.
3. If a bootloader volume appears in `/Volumes`, copy the UF2 to that volume.
4. Wait for the board to reboot and remount.
5. Confirm that macOS now sees the runtime volume:

   ```bash
   ls /Volumes/CIRCUITPY
   ```

If no bootloader volume appears, use the serial flashing path.

## Installation Path B: Factory Serial Flashing

This is the most reliable factory path for ESP32-S3 boards that do not expose a
UF2 bootloader volume.

1. Download the CircuitPython `10.2.1` BIN from the CircuitPython board page or
   from the direct BIN link above.
2. Put the XIAO into ROM bootloader mode:
   - Hold `Boot`.
   - Tap `Reset`.
   - Release `Boot`.
3. Identify the serial port:

   ```bash
   ls /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null
   ```

4. Flash with Web Serial ESPTool, or use command-line `esptool`.

Browser flashing:

- Open Adafruit WebSerial ESPTool from the CircuitPython board page.
- Select the XIAO serial port.
- Choose the downloaded `.bin`.
- Flash at offset `0x0`.

Command-line flashing:

```bash
PORT=/dev/cu.usbmodem21201
BIN=~/Projects/mcu_libs/circuitPython_10.x.x/adafruit-circuitpython-seeed_xiao_esp32_s3_sense-en_US-10.2.1.bin

python3 -m esptool --chip esp32s3 --port "$PORT" erase_flash
python3 -m esptool --chip esp32s3 --port "$PORT" write_flash -z 0x0 "$BIN"
```

5. After flashing completes, tap `Reset` or unplug/replug USB.
6. Wait several seconds for CircuitPython to format and mount its filesystem.
7. Confirm:

   ```bash
   ls /Volumes/CIRCUITPY
   ```

## Verify CircuitPython

Once `/Volumes/CIRCUITPY` exists:

1. Confirm the expected files:

   ```bash
   ls -la /Volumes/CIRCUITPY
   ```

2. The volume should contain a `boot_out.txt` file.
3. Read it:

   ```bash
   cat /Volumes/CIRCUITPY/boot_out.txt
   ```

4. Confirm it reports:
   - CircuitPython `10.2.1`
   - A Seeed Studio XIAO ESP32-S3 board name

At this point the board is ready for cPyNodus_II deployment.

## Prepare cPyNodus_II XIAO Firmware

The XIAO target uses the `xesp32s3` build tree:

```bash
scripts/nodus_mpy.sh --target xesp32s3 --clean
```

The build script searches the standard local `~/Projects/mcu_libs` layout,
including target-specific CircuitPython `10.2.1` compiler paths, before it
falls back to command names on `PATH`. It skips mismatched candidates, so the
shared CircuitPython `9.2.8` compiler cannot satisfy an `xesp32s3` build. If
the correct compiler is installed in that tree, no environment override is
needed.

This command must use a CircuitPython `10.2.1` `mpy-cross`. To inspect the
available local compilers:

```bash
find ~/Projects/mcu_libs \( -name mpy-cross -o -name 'mpy-cross-*' \) \
  -type f -exec sh -c \
  'for p do printf "%s: " "$p"; "$p" --version; done' sh {} +
```

Use only a compiler whose output includes both `CircuitPython 10.2.1` and
`mpy v6.3`. If the script fails with a compiler mismatch, set the correct
compiler explicitly:

```bash
export MPY_CROSS_XESP32S3="$HOME/Projects/mcu_libs/circuitPython_10.x.x/mpy-cross-macos-10.2.1-arm64"
scripts/nodus_mpy.sh --target xesp32s3 --clean
```

The expected output tree is:

```text
build/firmware/xesp32s3/
build/firmware/xesp32s3/BUILD_INFO
build/firmware/xesp32s3/cpynodus_ii/
build/firmware/xesp32s3/lib/
```

`BUILD_INFO` must report:

```text
target=xesp32s3
content=xesp32s3-mpy
circuitpython=10.2.1
mpy_abi=mpy v6.3
```

## Deploy to the Mounted XIAO

Only deploy after `/Volumes/CIRCUITPY` is mounted.

Preview first:

```bash
scripts/deploy_nodus.sh \
  --target /Volumes/CIRCUITPY \
  --content xesp32s3-mpy \
  --dry-run
```

Deploy:

```bash
scripts/deploy_nodus.sh \
  --target /Volumes/CIRCUITPY \
  --content xesp32s3-mpy
```

The deploy script validates `build/firmware/xesp32s3/BUILD_INFO` before it
copies files. It should refuse to deploy if the firmware image was compiled for
the wrong board or with the wrong compiler.

## First Nodus Boot Expectations

After deploy:

1. Eject `/Volumes/CIRCUITPY` cleanly in Finder or with `diskutil`.
2. Unplug and reconnect USB.
3. The board should run `boot.py`, then `code.py`.
4. On XIAO ESP32-S3, firmware currently forces host-edit mode and ignores the
   XIAO RW guard so `/Volumes/CIRCUITPY` remains visible for recovery.
5. Do not rely on `D8` for XIAO RWFS mode until the guard pin is reassigned or
   the temporary recovery patch is removed.

## Troubleshooting

`/Volumes/CIRCUITPY` never appears:

- Try a different USB-C cable and Mac port.
- Re-enter ROM bootloader mode with `Boot` held while tapping `Reset`.
- Reflash the `.bin` at offset `0x0`.
- Check macOS serial devices with:

  ```bash
  ls /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null
  ```

The device mounts, then disappears after Nodus deploy:

- Confirm the deployed `boot.py` still has the temporary XIAO forced edit-mode
  recovery patch. Without that patch, a grounded RW guard can make `boot.py`
  disable USB mass storage intentionally.

Deploy refuses `xesp32s3-mpy`:

- Read the refusal message. The most common cause is a missing or wrong
  CircuitPython `10.2.1` `mpy-cross`.
- Check the local compiler versions:

  ```bash
  find ~/Projects/mcu_libs \( -name mpy-cross -o -name 'mpy-cross-*' \) \
    -type f -exec sh -c \
    'for p do printf "%s: " "$p"; "$p" --version; done' sh {} +
  ```

- Rebuild with:

  ```bash
  export MPY_CROSS_XESP32S3="$HOME/Projects/mcu_libs/circuitPython_10.x.x/mpy-cross-macos-10.2.1-arm64"
  scripts/nodus_mpy.sh --target xesp32s3 --clean
  ```

Serial console shows `code.py` errors:

- Capture the traceback before editing files.
- Reboot in host-edit mode with `D8` ungrounded.
- Inspect `/Volumes/CIRCUITPY/boot_out.txt`, `/Volumes/CIRCUITPY/code.py`, and
  `/Volumes/CIRCUITPY/cpynodus_ii/`.
