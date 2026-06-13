# Nodus Pinouts

Pin numbers (physical) for Nodus wiring. Use the section that matches your build.

## Pico2 W I2C Devices (no soil sensor)

- 1 (GP0): I2C_0 SDA
- 2 (GP1): I2C_0 SCL
- 3 (GND): I2C_0 GND
- 4 (GP2): I2C_1 SDA
- 5 (GP3): I2C_1 SCL
- 7 (GP5): S1 ENABLE
- 8 (GND): GND for S1 EN
- 13 (GND): GND for S2 EN
- 14 (GP10): S2 ENABLE
- 16 (GP12): Battery Charge Controller Fault
- 17 (GP13): BCC Charging
- 18 (GND): GND for RW Enable
- 19 (GP14): RW ENABLE
- 22 (GP17): Factory reset input, hold LOW at boot for 5s to force `ACTIVE_PROFILE = "nodusweb"` and reboot
- 23 (GND): GND for Reset/Status LED
- 24 (GP18): Status LED
- 27 (GP21): Switch 2
- 28 (GND): Switch 2 GND
- 33 (AGND): Switch 1 GND
- 34 (GP28 / ADC2): Switch 1
- 36 (3V3(OUT)): I2C_0 VCC
- 38 (GND): I2C_1 GND
- 39 (VSYS): I2C_1 VCC (+5vdc)

## Pico2 W Soil Sensor (Waveshare Pico-2CH-RS485 HAT)

- 1 (GP0) CH1 UART TX
- 2 (GP1) CH1 UART RX
- 6 (GP4) CH2 UART TX
- 7 (GP5) CH2 UART RX
- 13 (GND): GND for S2 EN
- 14 (GP10): S2 ENABLE
- 18 (GND): GND for RW Enable
- 19 (GP14): RW ENABLE
- 22 (GP17): Factory reset input, hold LOW at boot for 5s to force `ACTIVE_PROFILE = "nodusweb"` and reboot
- 23 (GND): GND for Reset/Status LED
- 24 (GP18): Status LED
- 27 (GP21): Switch 2
- 28 (GND): Switch 2 GND
- 38 GND: GND (Ground)
- 39 VCC: VSYS (Power input)


All other pins: Unused by the default Nodus mappings. See the Pico2 W default
pinout for GP/GND/Power details.

## Seeed Studio XIAO ESP32-S3 Sense

Default bring-up mapping:

- `SDA` / `D4` / `A4` / GPIO5: I2C SDA
- `SCL` / `D5` / `A5` / GPIO6: I2C SCL
- `D0` / `A0` / GPIO1: S1 ENABLE
- `D1` / `A1` / GPIO2: S1 switch control
- `D2` / `A2` / GPIO3: S2 ENABLE
- `D3` / `A3` / GPIO4: S2 switch control
- `D8` / GPIO7: RW ENABLE, hold LOW at boot for app-writable filesystem
- Factory reset input: not assigned by default
- `TX` / GPIO43 and `RX` / GPIO44: optional manual UART/RS485 use

The XIAO profile factory-probes only the default `SCL`/`SDA` I2C bus. Additional
analog or digital inputs can be configured manually in TOML when supported by a
feature adapter.
