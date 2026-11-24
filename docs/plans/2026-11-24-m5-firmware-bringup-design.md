# M5 firmware bring-up: design

Approved by the user on 2026-11-24.

## Goal

Bring up every part of the robot on the ESP32-S3, one at a time, with a serial test menu:
- the LED and the PSRAM check;
- both eyes, with a few real expressions;
- a speaker tone;
- mic levels and a record/playback loopback;
- an eased servo sweep.

The code is written as modules that M6 reuses when the robot joins the WebSocket link. M5 has no Wi-Fi or networking.

## Hardware state (2026-11-24)

- **Wired:** both GC9A01 eyes (3.3 V) and the MAX98357A amp (5 V).
- **Pending:** the INMP441 mic (its header isn't soldered yet), the servos, the touch pad, the antenna LED and the backup button.
- **USB:** the board is on COM9, through the CH343 USB-UART port.
- **Backlight:** BLK isn't wired, so the screens run at full brightness.

## Decisions

- **Arduino core 3.x via pioarduino**, pinned to `55.03.312-1` (Arduino core 3.3.12, ESP-IDF 5.5.5).
  - Core 4.0.0 (ESP-IDF 6.1) was 5 days old, so we wait.
  - The official PlatformIO platform is frozen at core 2.x, with the legacy I2S driver.
- **LovyanGFX 1.2.36** for the eyes. It uses DMA, drives two panels on one SPI bus, draws anti-aliased shapes and keeps sprites in PSRAM.
  - TFT_eSPI is barely maintained and has core-3.x/S3 issues.
  - Arduino_GFX lacks anti-aliasing tools.
- **One app with a serial test menu.** The modules (`eyes`, `audio`, `servos`) are kept for M6.
- **Eyes in M5: a proof plus a few core looks** (`idle`, `happy`, `sleep`, `blink`). The full set and the state/emotion mapping come in M6.
- **Unit tests run on the board** (`pio test`, Unity). There is no C++ compiler on the PC, and a board run avoids adding a toolchain.

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

## Structure

```
firmware/
  platformio.ini        pioarduino pinned; esp32-s3-devkitc-1; 16 MB flash; qio_opi PSRAM
  include/pins.h        the pin map (kept in sync with docs/wiring.md)
  src/main.cpp          setup, the serial command menu, the main loop
  src/eyes.{h,cpp}      panels, sprites, eye parameters, presets, blending, blink
  src/audio.{h,cpp}     I2S speaker and mic, tone, levels, record/play
  src/servos.{h,cpp}    eased servo motion
  src/ease.h            pure maths (easing, lerp, levels), unit tested
  test/                 Unity tests, run on the board
```

## Serial menu (115200 baud)

| Command | What it does |
|---|---|
| `help` | Lists the commands. |
| `info` | Chip, flash, PSRAM, free heap and uptime. |
| `eyes idle\|blink\|happy\|sleep\|test` | Shows an expression or a test pattern. |
| `fps` | Reports the eyes' frame rate. |
| `tone [hz]` | Plays a 1 s tone (440 Hz by default). |
| `vol <0-100>` | Sets the volume. It starts at 20. |
| `mic` | Shows level bars for 10 s. |
| `rec` / `play` | Records 3 s, then plays it back. |
| `servo` | Runs an eased sweep. |

- At boot, the onboard LED blinks, `info` is printed and the idle eyes appear.
- An unknown command prints `help`.
- Every test has a time limit or stops when a key is pressed.

## Eyes

- **Panels.** There are two LovyanGFX panel objects on SPI2, with separate CS pins, at up to 80 MHz. Only the left panel owns RST (GPIO 9); the right panel's RST is -1, so starting it up doesn't reset the left one.
- **Drawing.** There is one 240×240 RGB565 sprite per eye (about 115 KB), both in PSRAM. Each frame is drawn off-screen and pushed by DMA, aiming for 30 fps or more.
  - If PSRAM is too slow or missing, the fallback is drawing in strips from internal RAM.
- **Parametric eye.** Each eye is a set of numbers:
  - openness (0 = closed line, 1 = full ring);
  - curve (frown ↔ flat ↔ happy arc);
  - size and ring thickness;
  - gaze x/y.

  The eyes are drawn anti-aliased, in cyan on black, following `docs/eye-expressions.png`.
- **Presets and blending.** Expressions are named parameter sets, and changes are blended over 150–300 ms with easing.
  - The left and right eyes have separate parameters, so winks and asymmetric looks need no special case.
  - M9's browser editor can store the same numbers.
- **Idle life.** A blink plays on top of the current expression at random every 3–6 s.
- **Not in M5:** the guide's complex looks (hearts, stars, waveform bars, the dotted processing ring) come in M6 and M9.

## Audio

- **Format:** 16 kHz, 16-bit mono both ways, matching `docs/protocol.md`. It uses the `ESP_I2S` library (core 3.x) in standard mode.
- **Speaker (I2S0 TX):**
  - The tone has 10 ms fades, so it doesn't click.
  - Volume is set in software, starting at 20 %, because 5 V into a 3 W speaker is loud.
- **Mic (I2S1 RX):**
  - The INMP441 sends 24-bit samples in 32-bit slots on the left channel, which are converted to int16.
  - `mic` prints the RMS and peak in dBFS 10 times a second.
  - `rec` records 3 s (96 KB) into PSRAM; `play` plays it back.
  - With no mic data, the commands say so instead of hanging.

## Servos

- **PWM:** LEDC at 50 Hz, with pulses of about 500–2500 µs.
- **Motion:** eased (ease-in-out) and speed-limited. Pan is clamped to about ±60°, and tilt gets a safe range once the head is built.
- **PWM stops when a servo reaches its target,** which avoids jitter and noise.
- **Power:** servos run from 5 V with a common ground and a 1000 µF capacitor.
- **Testing:** the code is written in M5 and tested once the servos are wired.

## Testing

- **Unit tests (Unity, on the board)** cover:
  - easing and lerp;
  - preset blending;
  - the blink scheduler;
  - the dBFS level maths;
  - the 32-bit to 16-bit sample conversion;
  - the servo angle-to-pulse conversion and clamping.
- **Hand checklist (by the user), in order:**
  1. The LED blinks and `info` shows about 8 MB of PSRAM.
  2. The left eye works, then both eyes, and `fps` reports the frame rate.
  3. The expressions and the blink look right.
  4. The tone plays and the volume control works.
  5. The mic levels respond (once the header is soldered).
  6. The loopback plays back what was recorded.
  7. The servo sweep is smooth (once the servos are wired).

## Docs

- **Wiring:** `docs/wiring.md` and `include/pins.h` get the pin map above.
- **`firmware/README.md`:** build, flash, monitor and test commands.
- **`decisions.md` and `CLAUDE.md`:** the core, the library, the pins and on-board tests.
- **The expression guide** is committed as `docs/eye-expressions.png`.
