# Wiring

> Pin map below; keep it in sync with firmware/include/pins.h — change both in the same commit.

## Board

Hosyond ESP32-S3 DevKit N16R8 (16 MB flash, 8 MB octal PSRAM).

## Pin rules

- **Never use** GPIO 35, 36, 37 — used by the octal PSRAM on the N16R8.
- **Avoid** strapping pins 0, 3, 45, 46 and USB pins 19, 20 for peripherals.
- GPIO 48 is the onboard RGB LED.

## Power

- Servos run from 5 V (not 3.3 V), with a common ground and a 1000 µF cap across servo power.
- Supply: 5 V 3 A USB-C.
- Eyes run from 3.3 V; the amp's VIN from 5 V. All grounds are common.

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
| Mic INMP441 (I2S1 RX) | SCK / WS / SD (L/R to GND) | 4 / 5 / 6 | planned |
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
