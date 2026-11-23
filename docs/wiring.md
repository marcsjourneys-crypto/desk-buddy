# Wiring

> **Status: stub.** Filled in during M5 (firmware bring-up). The pin map here must stay in sync with `firmware/include/pins.h` — change both in the same commit.

## Board

Hosyond ESP32-S3 DevKit N16R8 (16 MB flash, 8 MB octal PSRAM).

## Pin rules

- **Never use** GPIO 35, 36, 37 — used by the octal PSRAM on the N16R8.
- **Avoid** strapping pins 0, 3, 45, 46 and USB pins 19, 20 for peripherals.
- GPIO 48 is the onboard RGB LED.

## Power

- Servos run from 5 V (not 3.3 V), with a common ground and a 1000 µF cap across servo power.
- Supply: 5 V 3 A USB-C.

## Pin map

_To be filled in during M5._
