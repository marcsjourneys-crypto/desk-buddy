#pragma once
// Pin map for the Hosyond ESP32-S3 DevKit N16R8. Keep in sync with docs/wiring.md
// (change both in the same commit).
// Never use GPIO 26-37 (flash + octal PSRAM). Avoid 0, 3, 45, 46 (strapping) and
// 19, 20 (USB) for peripherals.

namespace pins {

// Eyes: two GC9A01 240x240 panels on one SPI bus (SPI2). The boards label SCK/MOSI as
// "SCL"/"SDA". The panels never send data back, so the bus has no MISO.
constexpr int kEyeSck = 12;
constexpr int kEyeMosi = 11;
constexpr int kEyeDc = 10;
constexpr int kEyeRst = 9;  // shared by both panels; only the left panel drives it
constexpr int kEyeCsLeft = 13;
constexpr int kEyeCsRight = 14;
constexpr int kEyeBacklight = 18;  // reserved, not wired yet

// Speaker: MAX98357A on I2S (TX). SD left unconnected = mono at default gain.
constexpr int kAmpBclk = 15;
constexpr int kAmpLrc = 16;
constexpr int kAmpDin = 17;

// Mic: INMP441 on I2S (RX), L/R tied to GND (left slot).
constexpr int kMicSck = 4;
constexpr int kMicWs = 5;
constexpr int kMicSd = 6;

// Planned (not wired yet).
constexpr int kTouch = 7;   // touch channel 7, copper-foil pad on the head
constexpr int kButton = 8;  // backup button to GND, internal pull-up
constexpr int kServoPan = 1;
constexpr int kServoTilt = 2;
constexpr int kAntennaLed = 21;  // WS2812B

constexpr int kOnboardLed = 48;  // onboard RGB LED (WS2812)

}  // namespace pins
