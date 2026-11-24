# Wiring

> The pin map below must stay in sync with `firmware/include/pins.h`; change both in the same commit.

## Board

Hosyond ESP32-S3 DevKit N16R8 (16 MB flash, 8 MB octal PSRAM). It connects to the PC through the USB-C port marked **COM/UART** (CH343).

## Pin rules

- **Never use** GPIO 26–37: flash and octal PSRAM (35–37 are the octal PSRAM lines).
- **Avoid** the strapping pins 0, 3, 45 and 46, and the USB pins 19 and 20, for peripherals.
- GPIO 48 is the onboard RGB LED.

## Power

### Final build

| Rail | Source | Feeds |
|---|---|---|
| **5V rail** | 5 V 3 A USB-C charger → USB-C breakout with 5.1 kΩ resistors from CC1 and CC2 to GND (without them a USB-C charger gives no power) | Both servos, the amp's VIN, the antenna LED, and the ESP32 through its **5V pin** |
| **3V3 rail** | The ESP32's onboard regulator (3V3 pin) | Both eyes and the mic. Nothing that draws heavy current. |
| **GND** | Shared by everything | The breakout's GND, the ESP32 GND, every module's GND |

- **Bulk capacitors:** 1000 µF electrolytic, 16 V or more, with the stripe to GND.
  - C1 across the 5V rail, close to the servos.
  - C2 across the amp's VIN, close to the amp.
- **Leave the board's IN-OUT jumper open.** While it's open, the 5V pin only takes power in; USB does not come out on it. Never bridge it while the 5V rail is connected, or the rail would feed back into the PC's USB port.
- **Never power the servos from 3V3.** They can draw up to about 0.7 A each and would reset the board.

### Current bench setup (2026-11-24)

- The board runs on USB only.
- The amp's VIN is on **3V3**, because the 5V pin gives no USB power. Keep `vol` at about 70 or below, or loud peaks load the regulator.
- The servos aren't wired yet; they need the 5V rail. For a bench test with nothing attached, you can use a USB-A charger rated 2 A or more, through a cut USB-A cable (red = 5 V, black = GND; check with a meter), with the grounds shared.

## Pin map

| Part | Signal | GPIO | Status |
|---|---|---|---|
| Eyes (shared SPI2) | SCK (board label "SCL") | 12 | wired |
| | MOSI (board label "SDA") | 11 | wired |
| | DC | 10 | wired |
| | RST (shared, driven by the left panel only) | 9 | wired |
| | CS left / CS right | 13 / 14 | wired |
| | Backlight (both) | 18 | reserved |
| Amp MAX98357A (I2S0 TX) | BCLK / LRC / DIN | 15 / 16 / 17 | wired |
| Mic INMP441 (I2S1 RX) | SCK / WS / SD (L/R to GND) | 4 / 5 / 6 | wired |
| Touch pad | touch channel 7 | 7 | planned |
| Backup button (internal pull-up) | — | 8 | planned |
| Servos pan / tilt | LEDC PWM, 50 Hz | 1 / 2 | planned |
| Antenna WS2812B | DIN | 21 | planned |
| Onboard RGB LED | — | 48 | on board |

All of these avoid:
- GPIO 26–37, used for flash and octal PSRAM;
- the strapping pins 0, 3, 45 and 46;
- the USB pins 19 and 20.

GPIO 13 is SPI2's default MISO line, but the panels never send data back, so the bus has no MISO and 13 can be a chip select.

## Per-module connections

### Eyes: 2× GC9A01 1.28" round displays

| Display pin | Left eye | Right eye |
|---|---|---|
| VCC | 3V3 | 3V3 |
| GND | GND | GND |
| SCL (really SCK) | GPIO 12 | GPIO 12 |
| SDA (really MOSI) | GPIO 11 | GPIO 11 |
| DC | GPIO 10 | GPIO 10 |
| RST | GPIO 9 | GPIO 9 |
| CS | **GPIO 13** | **GPIO 14** |
| BLK | GPIO 18 (reserved; unconnected = full brightness) | GPIO 18 (reserved) |

### Speaker amp: MAX98357A and a 3 W speaker

| Amp pin | Connects to |
|---|---|
| VIN | 5V rail (final), 3V3 (bench, for now) |
| GND | GND |
| BCLK / LRC / DIN | GPIO 15 / 16 / 17 |
| SD | Tied to VIN. This switches the amp on (left channel); on this board, SD left loose keeps it off. |
| GAIN | Not connected (9 dB) |
| Speaker + / − | Speaker terminals |

### Mic: INMP441

| Mic pin | Connects to |
|---|---|
| VDD | **3V3 only** |
| GND | GND |
| SCK / WS / SD | GPIO 4 / 5 / 6 |
| L/R | GND (left channel) |

Keep the mic wires short and away from the speaker and servo wires.

### Servos: 2× MG90S

| Servo wire | Pan | Tilt |
|---|---|---|
| Red | 5V rail | 5V rail |
| Brown | GND | GND |
| Orange (signal) | GPIO 1 | GPIO 2 |

- Pan is limited to about ±60° and tilt to about ±20° (provisional).
- Centre the horns by hand before the first power-on: the first move goes straight to centre.

### Planned

| Part | Connection |
|---|---|
| Antenna LED (WS2812B in a brass tube) | DIN ← GPIO 21 through a 330 Ω resistor; VDD → 5V rail; GND → GND. A 100 nF capacitor at the LED is optional. |
| Touch pad (copper foil on the head) | One short wire → GPIO 7 (the chip senses touch itself; no other parts). |
| Backup button | GPIO 8 ↔ GND (internal pull-up; no resistor). |
