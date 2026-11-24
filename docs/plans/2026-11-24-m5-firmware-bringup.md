# M5 Firmware Bring-up Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** One ESP32-S3 firmware with a serial test menu that brings up the onboard LED, PSRAM, both eyes (with idle, happy, sleep and blink), the speaker, the mic with a record/playback loopback, and eased servos. The modules are reused by M6.

**Architecture:**
- The PlatformIO project lives in `firmware/`.
- The pure logic (easing, eye parameters, audio maths, servo maths) is in header-only files in `include/`, unit tested on the board with Unity.
- The hardware modules (`eyes`, `audio`, `servos`) are in `src/` and are driven by a line-based serial menu in `main.cpp`.

**Tech stack:**
- pioarduino platform `55.03.312-1` (Arduino core 3.3.12, ESP-IDF 5.5.5);
- board `esp32-s3-devkitc1-n16r8`;
- LovyanGFX 1.2.36;
- `ESP_I2S` and LEDC from the core;
- Unity.

Design: `docs/plans/2026-11-24-m5-firmware-bringup-design.md`.

---

## Conventions (read first)

- **Run every `pio` command from PowerShell, never Git Bash.** ESP-IDF's tool installer refuses MSYS/MinGW shells. `pio` isn't on PATH, so use:
  `& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" <args>`
  Run it from `C:\GitHub\desk buddy\firmware`. In these steps, `pio` is short for that full command.
- **Board:** COM9 (the CH343 UART port). Only one program can hold COM9 at a time.
- **Build:** `pio run`
- **Flash:** `pio run -t upload --upload-port COM9`
- **Unit tests (they run on the board):** `pio test --upload-port COM9 --test-port COM9`
- **Talking to the board without the interactive monitor.** Use this helper, which resets the board, optionally sends a command, and prints the output:
  `& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" scripts\serial_cmd.py "<command>" <seconds>`
  It is created in Task 1.
- **Write files with the Edit/Write tools.** Never use shell heredocs, because Git Bash mangles backslashes.
- **Every commit ends with these trailer lines:**
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01S4FHmMu7pMrHuZS9GJZYSY
  ```
- **The server tests must still pass.** These tasks don't touch `server/`, but before each commit run `uv run pytest -q` in `server/` anyway.
- **Hardware checks by the user are marked 👀.** Stop and ask the user. Don't guess what the screens show.
- **The machine setup is already done:** PlatformIO Core 6.2.0 (pioarduino), the platform's Python packages, and Windows long paths. Task 1 documents it in `firmware/README.md`.

---

### Task 1: Project scaffold, pin map, LED blink and `info`

**Files:**
- Create: `firmware/platformio.ini`
- Create: `firmware/include/pins.h`
- Create: `firmware/src/main.cpp`
- Create: `firmware/scripts/serial_cmd.py`
- Modify: `firmware/README.md`
- Modify: `docs/wiring.md`

**Step 1: `firmware/platformio.ini`**

```ini
; Desk Buddy robot firmware. See README.md for the Windows setup notes.
[platformio]
default_envs = esp32s3

[env:esp32s3]
; pioarduino = Arduino core 3.x. 55.03.312-1 is Arduino core 3.3.12 (ESP-IDF 5.5.5).
platform = https://github.com/pioarduino/platform-espressif32/releases/download/55.03.312-1/platform-espressif32.zip
board = esp32-s3-devkitc1-n16r8
framework = arduino
build_flags = -DARDUINO_USB_CDC_ON_BOOT=0
build_src_flags = -Wall -Wextra
monitor_speed = 115200
monitor_echo = yes
; RTS/DTR drive EN/BOOT on this board: keep them released so the monitor doesn't hold it in reset
monitor_rts = 0
monitor_dtr = 0
monitor_filters = esp32_exception_decoder
lib_deps = lovyan03/LovyanGFX@1.2.36
test_framework = unity
```

**Step 2: `firmware/include/pins.h`**

```cpp
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
```

**Step 3: `firmware/src/main.cpp`**

```cpp
// Desk Buddy firmware, M5 bring-up: a serial test menu for every part of the robot.
#include <Arduino.h>

#include "pins.h"

namespace {

constexpr char kFwVersion[] = "0.1.0-m5";

char line[64];
size_t lineLen = 0;

void blinkLed() {
  for (int i = 0; i < 3; i++) {
    rgbLedWrite(pins::kOnboardLed, 0, 0, 40);
    delay(150);
    rgbLedWrite(pins::kOnboardLed, 0, 0, 0);
    delay(150);
  }
}

void printInfo() {
  Serial.printf("Desk Buddy firmware %s (M5 bring-up)\n", kFwVersion);
  Serial.printf("Chip: %s rev %d, %d cores @ %lu MHz\n", ESP.getChipModel(),
                ESP.getChipRevision(), ESP.getChipCores(),
                static_cast<unsigned long>(ESP.getCpuFreqMHz()));
  Serial.printf("Flash: %lu MB\n", static_cast<unsigned long>(ESP.getFlashChipSize() >> 20));
  if (psramFound()) {
    Serial.printf("PSRAM: %.1f MB (free %.1f MB)\n", ESP.getPsramSize() / 1048576.0,
                  ESP.getFreePsram() / 1048576.0);
  } else {
    Serial.println("PSRAM: NOT FOUND - check the board setting in platformio.ini");
  }
  Serial.printf("Heap free: %lu KB\n", static_cast<unsigned long>(ESP.getFreeHeap() / 1024));
  Serial.printf("Uptime: %lu s\n", static_cast<unsigned long>(millis() / 1000));
}

void printHelp() {
  Serial.println("Commands:");
  Serial.println("  help                      this list");
  Serial.println("  info                      chip, flash, PSRAM, memory, uptime");
}

void handle(char* cmd) {
  char* name = strtok(cmd, " ");
  [[maybe_unused]] char* arg = strtok(nullptr, " ");
  if (name == nullptr) return;
  if (!strcmp(name, "help")) {
    printHelp();
  } else if (!strcmp(name, "info")) {
    printInfo();
  } else {
    Serial.printf("Unknown command '%s'\n", name);
    printHelp();
  }
}

void pollSerial() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r' || c == '\n') {
      line[lineLen] = '\0';
      if (lineLen > 0) {
        Serial.printf("> %s\n", line);
        handle(line);
      }
      lineLen = 0;
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(200);
  blinkLed();
  printInfo();
  Serial.println("Type 'help' for commands.");
}

void loop() {
  pollSerial();
  delay(1);
}
```

**Step 4: `firmware/scripts/serial_cmd.py`.** This is a helper for Claude and the subagents, run with the PlatformIO Python, which has pyserial.

```python
"""Reset the board on COM9, optionally send one command, and print the output.

Usage: python serial_cmd.py ["command"] [seconds] [port]
Run it with the PlatformIO Python (it has pyserial). Close any serial monitor first.
"""

import sys
import time

import serial


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    port = sys.argv[3] if len(sys.argv) > 3 else "COM9"
    with serial.Serial(port, 115200, timeout=0.2) as s:
        s.dtr = False
        s.rts = True  # pulse EN: reset into the app
        time.sleep(0.1)
        s.rts = False
        end = time.time() + seconds
        sent = not command
        boot_done = time.time() + 2.5  # boot blinks the LED and prints info first
        while time.time() < end:
            data = s.read(4096)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            if not sent and time.time() > boot_done:
                s.write((command + "\n").encode())
                sent = True


if __name__ == "__main__":
    main()
```

**Step 5: `firmware/README.md`.** Replace the file with:

````markdown
# Firmware

ESP32-S3 firmware for the Desk Buddy robot: PlatformIO, Arduino core 3.x (pioarduino), LovyanGFX.

- **Board:** Hosyond ESP32-S3 DevKit N16R8 (`esp32-s3-devkitc1-n16r8`). Use the **COM/UART** USB-C port (CH343).
- **Pins:** `include/pins.h` and `../docs/wiring.md` (keep them in sync).
- **Constraints:** see `../CLAUDE.md`.

## Commands (PowerShell, in `firmware/`)

`pio` isn't on PATH. Use `& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe"`, or the PlatformIO toolbar in VS Code.

| What | Command |
|---|---|
| Build | `pio run` |
| Flash | `pio run -t upload` |
| Serial monitor (type `help`) | `pio device monitor` |
| Unit tests (they run on the board) | `pio test` |

Close the serial monitor before flashing or testing; only one program can use the port.

## Windows setup notes (one-time, done 2026-11-24)

- **Long paths.** Core 3.x has paths over 260 characters. Turn on long paths once, from any PowerShell window (it shows a UAC prompt):
  `Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -Command Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1"`
- **PlatformIO Core 6.2 (pioarduino).** The platform needs Core 6.2 or newer, and the platform's own installer can't replace the running core on Windows.
  - Install it with PlatformIO closed:
    `& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" -m pip install "pioarduino==6.2.0"`
  - Don't install `6.2.0.post1`: its version counts as older than 6.2.0, so the platform refuses it.
  - If `pio` breaks with a missing module (urllib3, certifi…), reinstall with `pip install --ignore-installed "pioarduino==6.2.0"`.
- **Use PowerShell, not Git Bash.** ESP-IDF's tool installer refuses MSYS/MinGW shells ("MSys/Mingw is not supported"), and the compiler is then missing.
````

**Step 6: `docs/wiring.md`.** Replace `_To be filled in during M5._` with the pin table from the design doc, the "Pin map" section of `docs/plans/2026-11-24-m5-firmware-bringup-design.md`, copied word for word. Also:
- change the status line to `> Pin map below; keep it in sync with firmware/include/pins.h — change both in the same commit.`;
- add under **Power**: `Eyes run from 3.3 V; the amp's VIN from 5 V. All grounds are common.`

**Step 7: Build, flash and check.**
- Run `pio run`. Expected: `[SUCCESS]`, with no warnings from `src/`.
- Run `pio run -t upload --upload-port COM9`.
- Run `python scripts\serial_cmd.py "info" 6`, using the PlatformIO Python. Expected:
  - the boot `info` block shows `Flash: 16 MB` and `PSRAM: 8.0 MB`;
  - then `> info` appears and the block is printed again.

**Step 8: Commit.**
- Files: `firmware/` and `docs/wiring.md`.
- Message: `Scaffold the firmware: pin map, LED blink and info command`

---

### Task 2: Pure logic, part 1: easing and the eye model, with tests

**Files:**
- Create: `firmware/include/ease.h`
- Create: `firmware/include/eye_model.h`
- Create: `firmware/test/test_logic/test_main.cpp`

**The eye model.** Each eye is two lids. Each lid is a half-ellipse from the left corner to the right corner:
- `-1` bulges fully up, `0` is flat, `+1` bulges fully down.
- Upper `-1` with lower `+1` is an open ring.
- When the two values are equal they draw a single curve: `∩` (happy), `‿` (sleep, blink) or a line.
- Blending between any two looks is a plain lerp of the numbers.

**Step 1: Write the failing tests** (`firmware/test/test_logic/test_main.cpp`):

```cpp
// Unit tests for the pure logic in include/. They run on the board: pio test
#include <Arduino.h>
#include <unity.h>

#include "ease.h"
#include "eye_model.h"

void setUp() {}
void tearDown() {}

void test_lerp_and_clamp() {
  TEST_ASSERT_EQUAL_FLOAT(5.0f, ease::lerp(0, 10, 0.5f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, ease::clamp01(-2));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::clamp01(3));
}

void test_in_out_cubic() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, ease::inOutCubic(0));
  TEST_ASSERT_EQUAL_FLOAT(0.0625f, ease::inOutCubic(0.25f));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, ease::inOutCubic(0.5f));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::inOutCubic(1));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::inOutCubic(7));  // clamped
}

void test_progress_survives_millis_wrap() {
  TEST_ASSERT_EQUAL_FLOAT(0.5f, ease::progress(0x00000010u, 0xFFFFFF00u, 544));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::progress(100, 0, 0));  // zero duration = done
}

void test_params_lerp() {
  const eye::Params p = eye::lerp(eye::presets::idle(), eye::presets::happy(), 0.5f);
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, p.upper);
  TEST_ASSERT_EQUAL_FLOAT(0.2f, p.lower);
}

void test_closed_like_keeps_size_and_gaze() {
  eye::Params p = eye::presets::idle();
  p.gaze_x = 7;
  const eye::Params c = eye::closedLike(p);
  TEST_ASSERT_EQUAL_FLOAT(c.upper, c.lower);
  TEST_ASSERT_EQUAL_FLOAT(7.0f, c.gaze_x);
  TEST_ASSERT_EQUAL_FLOAT(p.radius, c.radius);
}

void test_same() {
  eye::Params a = eye::presets::idle();
  eye::Params b = a;
  TEST_ASSERT_TRUE(eye::same(a, b));
  b.upper += 0.1f;
  TEST_ASSERT_FALSE(eye::same(a, b));
}

void test_tween() {
  eye::Tween t;
  t.set(eye::presets::idle());
  t.to(eye::presets::happy(), 1000, 200);
  TEST_ASSERT_EQUAL_FLOAT(-1.0f, t.at(1000).upper);
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, t.at(1100).upper);  // eased midpoint is exactly halfway
  TEST_ASSERT_EQUAL_FLOAT(-0.6f, t.at(1200).upper);
  TEST_ASSERT_TRUE(t.moving(1100));
  TEST_ASSERT_FALSE(t.moving(1200));
}

void test_tween_retarget_starts_from_current() {
  eye::Tween t;
  t.set(eye::presets::idle());
  t.to(eye::presets::happy(), 0, 200);
  t.to(eye::presets::idle(), 100, 200);  // mid-blend: starts from where it is now
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, t.at(100).upper);
}

void test_blinker() {
  eye::Blinker b;
  b.blinkNow(1000);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(999));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(1000));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, b.amount(1090));  // fully closed halfway through
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(1180));
  TEST_ASSERT_FALSE(b.finished(1100));
  TEST_ASSERT_TRUE(b.finished(1180));
  b.schedule(2000, 4000);
  TEST_ASSERT_FALSE(b.finished(5999));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(5999));
}

void setup() {
  delay(2000);  // let the serial port settle
  UNITY_BEGIN();
  RUN_TEST(test_lerp_and_clamp);
  RUN_TEST(test_in_out_cubic);
  RUN_TEST(test_progress_survives_millis_wrap);
  RUN_TEST(test_params_lerp);
  RUN_TEST(test_closed_like_keeps_size_and_gaze);
  RUN_TEST(test_same);
  RUN_TEST(test_tween);
  RUN_TEST(test_tween_retarget_starts_from_current);
  RUN_TEST(test_blinker);
  UNITY_END();
}

void loop() {}
```

**Step 2: Run the tests and see them fail.**
- Run `pio test --upload-port COM9 --test-port COM9`.
- Expected: the build fails with `ease.h: No such file or directory`.

**Step 3: `firmware/include/ease.h`**

```cpp
#pragma once
// Easing and interpolation. Pure maths: no Arduino, unit tested on the board.
#include <math.h>
#include <stdint.h>

namespace ease {

inline float clamp01(float t) { return t < 0 ? 0 : (t > 1 ? 1 : t); }

inline float lerp(float a, float b, float t) { return a + (b - a) * t; }

// Slow start, slow end. 0 -> 0, 0.5 -> 0.5, 1 -> 1.
inline float inOutCubic(float t) {
  t = clamp01(t);
  return t < 0.5f ? 4 * t * t * t : 1 - powf(-2 * t + 2, 3) / 2;
}

// 0..1 progress of an animation that started at `start` and lasts `duration` ms.
// Unsigned subtraction keeps it right when millis() wraps.
inline float progress(uint32_t now, uint32_t start, uint32_t duration) {
  if (duration == 0) return 1;
  return clamp01(static_cast<float>(static_cast<uint32_t>(now - start)) / duration);
}

}  // namespace ease
```

**Step 4: `firmware/include/eye_model.h`**

```cpp
#pragma once
// One eye as a few numbers, so looks can blend smoothly and M9's browser editor can store
// them. Pure logic: no drawing here (that's src/eyes.cpp).
#include <math.h>
#include <stdint.h>

#include "ease.h"

namespace eye {

// Each lid is a half-ellipse from the left corner to the right corner:
// -1 = bulges fully up, 0 = flat, +1 = bulges fully down.
// upper -1 / lower +1 is an open ring; equal values draw one curve (happy arc, sleepy
// curve, closed line).
struct Params {
  float upper = -1;
  float lower = 1;
  float radius = 70;     // px
  float thickness = 14;  // stroke width, px
  float gaze_x = 0;      // px offset from the centre
  float gaze_y = 0;
};

inline Params lerp(const Params& a, const Params& b, float t) {
  Params p;
  p.upper = ease::lerp(a.upper, b.upper, t);
  p.lower = ease::lerp(a.lower, b.lower, t);
  p.radius = ease::lerp(a.radius, b.radius, t);
  p.thickness = ease::lerp(a.thickness, b.thickness, t);
  p.gaze_x = ease::lerp(a.gaze_x, b.gaze_x, t);
  p.gaze_y = ease::lerp(a.gaze_y, b.gaze_y, t);
  return p;
}

// Close enough that redrawing would look the same.
inline bool same(const Params& a, const Params& b) {
  auto near = [](float x, float y) { return fabsf(x - y) < 0.01f; };
  return near(a.upper, b.upper) && near(a.lower, b.lower) && near(a.radius, b.radius) &&
         near(a.thickness, b.thickness) && near(a.gaze_x, b.gaze_x) &&
         near(a.gaze_y, b.gaze_y);
}

// The same eye with both lids shut into a gentle downward curve (the bottom of a blink).
inline Params closedLike(Params p) {
  p.upper = 0.3f;
  p.lower = 0.3f;
  return p;
}

namespace presets {
inline Params idle() { return Params{}; }
inline Params happy() {
  Params p;
  p.upper = -0.6f;
  p.lower = -0.6f;
  p.gaze_y = 20;  // the arc sits above the centre; nudge it down
  return p;
}
inline Params sleep() {
  Params p;
  p.upper = 0.35f;
  p.lower = 0.35f;
  return p;
}
}  // namespace presets

// Blends from whatever is showing to a target over `duration` ms, eased.
class Tween {
 public:
  void set(const Params& p) {
    from_ = to_ = p;
    duration_ = 0;
  }
  void to(const Params& target, uint32_t now, uint32_t duration) {
    from_ = at(now);
    to_ = target;
    start_ = now;
    duration_ = duration;
  }
  Params at(uint32_t now) const {
    return lerp(from_, to_, ease::inOutCubic(ease::progress(now, start_, duration_)));
  }
  bool moving(uint32_t now) const {
    return duration_ > 0 && static_cast<uint32_t>(now - start_) < duration_;
  }

 private:
  Params from_, to_;
  uint32_t start_ = 0;
  uint32_t duration_ = 0;
};

// One blink at a time: amount() is 0 when open and rises to 1 (closed) and back over kMs.
class Blinker {
 public:
  static constexpr uint32_t kMs = 180;

  void schedule(uint32_t now, uint32_t delay) { next_ = now + delay; }
  void blinkNow(uint32_t now) { next_ = now; }

  float amount(uint32_t now) const {
    const int32_t dt = static_cast<int32_t>(now - next_);
    if (dt < 0 || dt >= static_cast<int32_t>(kMs)) return 0;
    const float t = static_cast<float>(dt) / kMs;
    return t < 0.5f ? ease::inOutCubic(t * 2) : ease::inOutCubic((1 - t) * 2);
  }
  // True once the current blink is over (time to schedule the next one).
  bool finished(uint32_t now) const {
    return static_cast<int32_t>(now - next_) >= static_cast<int32_t>(kMs);
  }

 private:
  uint32_t next_ = 0;
};

}  // namespace eye
```

**Step 5: Run the tests and see them pass.**
- Run `pio test --upload-port COM9 --test-port COM9`.
- Expected: `9 Tests 0 Failures 0 Ignored` and `PASSED`.

**Step 6: Commit.** Message: `Add easing and the parametric eye model, with on-board tests`

---

### Task 3: The eyes module, plus the `eyes` and `fps` commands

**Files:**
- Create: `firmware/src/eyes.h`
- Create: `firmware/src/eyes.cpp`
- Modify: `firmware/src/main.cpp`

**Step 1: `firmware/src/eyes.h`**

```cpp
#pragma once
// Both GC9A01 eyes: panels, frame buffers, expressions, blending and idle blinks.
#include <stdint.h>

namespace eyes {

bool begin();                   // false if the frame buffers can't be allocated
bool show(const char* name);    // idle, happy, sleep, blink, test; false if unknown
void update(uint32_t now);      // call often: redraws only while something moves
float measureFps(uint32_t ms);  // forced redraws of both eyes for `ms`, returns frames/s

}  // namespace eyes
```

**Step 2: `firmware/src/eyes.cpp`**

```cpp
#include "eyes.h"

#include <Arduino.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>

#include "eye_model.h"
#include "pins.h"

namespace {

constexpr uint16_t kCyan = 0x2F3C;  // RGB565 of #2EE6E6, the expression guide's cyan
constexpr uint16_t kBlack = 0x0000;
constexpr int kSize = 240;
constexpr int kCenter = kSize / 2;
constexpr int kSegments = 24;  // straight pieces per lid curve
constexpr int kRotation = 0;   // 0 = arc at the pin edge; change once the head is built

// One GC9A01 on the shared SPI2 bus. Only the left panel drives the shared reset line:
// if both did, starting the right eye would reset the already-started left eye.
class Panel : public lgfx::LGFX_Device {
 public:
  Panel(int cs, int rst) {
    auto b = bus_.config();
    b.spi_host = SPI2_HOST;
    b.spi_mode = 0;
    b.freq_write = 80000000;
    b.freq_read = 16000000;
    b.spi_3wire = true;
    b.use_lock = true;
    b.dma_channel = SPI_DMA_CH_AUTO;
    b.pin_sclk = pins::kEyeSck;
    b.pin_mosi = pins::kEyeMosi;
    b.pin_miso = -1;
    b.pin_dc = pins::kEyeDc;
    bus_.config(b);
    panel_.setBus(&bus_);
    auto p = panel_.config();
    p.pin_cs = cs;
    p.pin_rst = rst;
    p.pin_busy = -1;
    p.panel_width = kSize;
    p.panel_height = kSize;
    p.invert = true;
    p.readable = false;
    p.bus_shared = true;
    panel_.config(p);
    setPanel(&panel_);
  }

 private:
  lgfx::Panel_GC9A01 panel_;
  lgfx::Bus_SPI bus_;
};

Panel leftPanel(pins::kEyeCsLeft, pins::kEyeRst);
Panel rightPanel(pins::kEyeCsRight, -1);
LGFX_Sprite leftSprite(&leftPanel);
LGFX_Sprite rightSprite(&rightPanel);
Panel* panels[2] = {&leftPanel, &rightPanel};
LGFX_Sprite* sprites[2] = {&leftSprite, &rightSprite};

eye::Tween tweens[2];
eye::Params drawn[2];
eye::Blinker blinker;
bool ready = false;
bool dirty = true;  // force the next update() to redraw
bool autoBlink = true;
bool testMode = false;

// One lid: a half-ellipse from the left corner to the right, as thick anti-aliased segments.
void drawLid(LGFX_Sprite& s, const eye::Params& p, float bulge) {
  const float cx = kCenter + p.gaze_x;
  const float cy = kCenter + p.gaze_y;
  const float w = p.thickness / 2;
  float px = 0, py = 0;
  for (int i = 0; i <= kSegments; i++) {
    const float th = PI * (1 - static_cast<float>(i) / kSegments);  // left corner -> right
    const float x = cx + p.radius * cosf(th);
    const float y = cy + bulge * p.radius * sinf(th);
    if (i > 0) s.drawWedgeLine(px, py, x, y, w, w, kCyan);
    px = x;
    py = y;
  }
}

void draw(int side, const eye::Params& p) {
  LGFX_Sprite& s = *sprites[side];
  s.fillScreen(kBlack);
  drawLid(s, p, p.upper);
  if (fabsf(p.lower - p.upper) > 0.02f) drawLid(s, p, p.lower);
  s.pushSprite(0, 0);
}

// "L" and "R" inside a ring, so you can tell the eyes apart and see the panel edges.
void drawTest() {
  const char* labels[2] = {"L", "R"};
  for (int i = 0; i < 2; i++) {
    LGFX_Sprite& s = *sprites[i];
    s.fillScreen(kBlack);
    s.fillSmoothCircle(kCenter, kCenter, 118, kCyan);
    s.fillSmoothCircle(kCenter, kCenter, 112, kBlack);
    s.setTextColor(kCyan);
    s.setTextSize(8);
    s.drawString(labels[i], kCenter - s.textWidth(labels[i]) / 2, kCenter - s.fontHeight() / 2);
    s.pushSprite(0, 0);
  }
}

}  // namespace

namespace eyes {

bool begin() {
  leftPanel.init();  // left first: it owns the shared reset line
  rightPanel.init();
  for (int i = 0; i < 2; i++) {
    panels[i]->setRotation(kRotation);
    sprites[i]->setPsram(true);
    sprites[i]->setColorDepth(16);
    if (sprites[i]->createSprite(kSize, kSize) == nullptr) {
      Serial.println("Eyes: no memory for the frame buffers (is PSRAM on?)");
      return false;
    }
    tweens[i].set(eye::presets::idle());
  }
  blinker.schedule(millis(), 2000);
  ready = true;
  dirty = true;
  return true;
}

bool show(const char* name) {
  if (!ready) return false;
  const uint32_t now = millis();
  if (!strcmp(name, "blink")) {
    blinker.blinkNow(now);
    return true;
  }
  if (!strcmp(name, "test")) {
    drawTest();
    testMode = true;
    return true;
  }
  eye::Params target;
  uint32_t ms = 250;
  bool blinks = true;
  if (!strcmp(name, "idle")) {
    target = eye::presets::idle();
  } else if (!strcmp(name, "happy")) {
    target = eye::presets::happy();
  } else if (!strcmp(name, "sleep")) {
    target = eye::presets::sleep();
    ms = 600;
    blinks = false;
  } else {
    return false;
  }
  testMode = false;
  autoBlink = blinks;
  dirty = true;
  for (auto& t : tweens) t.to(target, now, ms);
  return true;
}

void update(uint32_t now) {
  if (!ready || testMode) return;
  if (autoBlink && blinker.finished(now)) blinker.schedule(now, 3000 + esp_random() % 3000);
  const float b = blinker.amount(now);
  for (int i = 0; i < 2; i++) {
    eye::Params p = tweens[i].at(now);
    if (b > 0) p = eye::lerp(p, eye::closedLike(p), b);
    if (!dirty && eye::same(p, drawn[i])) continue;
    draw(i, p);
    drawn[i] = p;
  }
  dirty = false;
}

float measureFps(uint32_t ms) {
  if (!ready) return 0;
  const uint32_t start = millis();
  uint32_t frames = 0;
  while (millis() - start < ms) {
    for (int i = 0; i < 2; i++) {
      eye::Params p = tweens[i].at(millis());
      p.gaze_x = 15 * sinf(frames * 0.2f);  // keep it moving so every frame is real work
      draw(i, p);
    }
    frames++;
  }
  dirty = true;
  return frames * 1000.0f / (millis() - start);
}

}  // namespace eyes
```

**Step 3: Wire it into `main.cpp`.**
- Add `#include "eyes.h"`.
- In `printHelp()`, add:
  ```cpp
  Serial.println("  eyes idle|happy|sleep|blink|test");
  Serial.println("  fps                       eye frame rate over 3 s");
  ```
- In `handle()`, before the final `else`:
  ```cpp
  } else if (!strcmp(name, "eyes")) {
    if (arg == nullptr || !eyes::show(arg)) Serial.println("Usage: eyes idle|happy|sleep|blink|test");
  } else if (!strcmp(name, "fps")) {
    Serial.printf("Eyes: %.1f fps (both eyes)\n", eyes::measureFps(3000));
  ```
- Remove `[[maybe_unused]]` from `arg`.
- In `setup()`, after `printInfo();`:
  ```cpp
  if (!eyes::begin()) Serial.println("Eyes: not started");
  ```
- In `loop()`, before `pollSerial();`: `eyes::update(millis());`

**Step 4: Build, flash and check what can be checked automatically.**
- Run `pio run`. Expected: `[SUCCESS]` with no warnings from `src/`.
- If `drawWedgeLine` rejects float arguments, cast them to `int32_t`.
- Flash, then run `serial_cmd.py "fps" 10`. Expected: `Eyes: N fps`. Record N; the spike measured about 23.

**Step 5: 👀 The user checks the eyes.** Ask the user to open `pio device monitor` and try each command:
- `eyes test`: L on the left eye and R on the right, each in a ring at the glass edge.
- `eyes idle`: open rings that blink on their own every 3–6 s.
- `eyes blink`: one quick blink.
- `eyes happy`: arcs (∩), blending smoothly from the rings.
- `eyes sleep`: slow closing into downward curves (‿), with no blinking.
- `fps`.

Note anything the user wants tuned, such as size, thickness, the cyan, or the arc positions. Small preset tweaks go in `eye_model.h`, with the tests updated to match.

**Step 6: Commit.** Message: `Draw both eyes with blending expressions and idle blinks`

---

### Task 4: Pure logic, part 2: audio and servo maths, with tests

**Files:**
- Create: `firmware/include/dsp.h`
- Create: `firmware/include/servo_math.h`
- Modify: `firmware/test/test_logic/test_main.cpp`

**Step 1: Add the failing tests.**
- Add `#include "dsp.h"` and `#include "servo_math.h"` to the test file.
- Add these tests, and register each with `RUN_TEST`:

```cpp
void test_mic_to_16() {
  TEST_ASSERT_EQUAL_INT16(4, dsp::micTo16(0x00010000));
  TEST_ASSERT_EQUAL_INT16(-4, dsp::micTo16(-0x00010000));
  TEST_ASSERT_EQUAL_INT16(32767, dsp::micTo16(0x7FFFFF00));   // saturates, no wrap
  TEST_ASSERT_EQUAL_INT16(-32768, dsp::micTo16(INT32_MIN));
}

void test_to_db() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::toDb(32768));
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -20.0f, dsp::toDb(3277));
  TEST_ASSERT_EQUAL_FLOAT(-96.0f, dsp::toDb(0));
}

void test_level() {
  int16_t x[100];
  for (auto& s : x) s = 16384;
  dsp::Level lv = dsp::level(x, 100);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -6.02f, lv.rms_db);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -6.02f, lv.peak_db);
  x[3] = -32768;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::level(x, 100).peak_db);
  TEST_ASSERT_EQUAL_FLOAT(-96.0f, dsp::level(x, 0).rms_db);
}

void test_is_flat() {
  int16_t zeros[50] = {0};
  TEST_ASSERT_TRUE(dsp::isFlat(zeros, 50));
  zeros[10] = 300;
  TEST_ASSERT_FALSE(dsp::isFlat(zeros, 50));
}

void test_fade_gain() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(0, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(5, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(50, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(94, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(99, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(3, 100, 0));
}

void test_scale_volume() {
  TEST_ASSERT_EQUAL_INT16(5000, dsp::scale(10000, 50));
  TEST_ASSERT_EQUAL_INT16(0, dsp::scale(10000, 0));
  TEST_ASSERT_EQUAL_INT16(-32768, dsp::scale(-32768, 100));
}

void test_tone_continues_phase() {
  int16_t a[8], b[8], all[16];
  float p1 = 0, p2 = 0;
  dsp::tone(a, 8, 1000, 10000, p1);
  dsp::tone(b, 8, 1000, 10000, p1);
  dsp::tone(all, 16, 1000, 10000, p2);
  TEST_ASSERT_EQUAL_INT16(0, a[0]);
  TEST_ASSERT_EQUAL_INT16(all[8], b[0]);   // second block carries on seamlessly
  TEST_ASSERT_EQUAL_INT16(all[15], b[7]);
}

void test_servo_pulse() {
  TEST_ASSERT_EQUAL_UINT32(1500, servo::angleToPulseUs(0));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(90));
  TEST_ASSERT_EQUAL_UINT32(500, servo::angleToPulseUs(-90));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(120));  // clamped
  TEST_ASSERT_EQUAL_UINT32(1228, servo::pulseToDuty(1500));     // 14-bit duty at 50 Hz
  TEST_ASSERT_EQUAL_UINT32(409, servo::pulseToDuty(500));
}

void test_servo_clamp() {
  TEST_ASSERT_EQUAL_FLOAT(60.0f, servo::clampAngle(75, 60));
  TEST_ASSERT_EQUAL_FLOAT(-60.0f, servo::clampAngle(-75, 60));
  TEST_ASSERT_EQUAL_FLOAT(10.0f, servo::clampAngle(10, 60));
}

void test_servo_motion() {
  servo::Motion m;
  m.moveTo(0, 90, 1000, 90);  // 90 degrees at 90 deg/s = 1 s
  TEST_ASSERT_EQUAL_FLOAT(0.0f, m.at(1000));
  TEST_ASSERT_EQUAL_FLOAT(45.0f, m.at(1500));
  TEST_ASSERT_EQUAL_FLOAT(90.0f, m.at(2000));
  TEST_ASSERT_FALSE(m.done(1999));
  TEST_ASSERT_TRUE(m.done(2000));
  m.moveTo(90, 90, 3000, 90);  // no distance: done at once
  TEST_ASSERT_TRUE(m.done(3000));
}
```

**Step 2: Run the tests and see them fail.** Expected: the build fails with `dsp.h: No such file`.

**Step 3: `firmware/include/dsp.h`**

```cpp
#pragma once
// Audio maths for 16 kHz 16-bit mono (the protocol's format). Pure: no Arduino.
#include <math.h>
#include <stddef.h>
#include <stdint.h>

namespace dsp {

constexpr int kRate = 16000;
constexpr float kToneAmp = 16000;  // half scale; volume scales it further

// INMP441 sends a 24-bit sample left-justified in a 32-bit slot. Shifting by 16 would be
// unity; 14 adds 12 dB because the INMP441 is quiet. Saturates instead of wrapping.
constexpr int kMicShift = 14;
inline int16_t micTo16(int32_t s) {
  const int32_t v = s >> kMicShift;
  return v > 32767 ? 32767 : (v < -32768 ? -32768 : static_cast<int16_t>(v));
}

// Amplitude (0..32768) to dBFS; silence floors at -96.
inline float toDb(float amp) { return amp < 0.5f ? -96.0f : 20 * log10f(amp / 32768.0f); }

struct Level {
  float rms_db;
  float peak_db;
};

inline Level level(const int16_t* x, size_t n) {
  if (n == 0) return {-96.0f, -96.0f};
  double sum = 0;
  int peak = 0;
  for (size_t i = 0; i < n; i++) {
    const int v = x[i];
    sum += static_cast<double>(v) * v;
    const int a = v < 0 ? -v : v;
    if (a > peak) peak = a;
  }
  return {toDb(sqrtf(static_cast<float>(sum / n))), toDb(static_cast<float>(peak))};
}

// True when every sample is (nearly) the same: no mic on the line.
inline bool isFlat(const int16_t* x, size_t n) {
  int lo = 32767, hi = -32768;
  for (size_t i = 0; i < n; i++) {
    if (x[i] < lo) lo = x[i];
    if (x[i] > hi) hi = x[i];
  }
  return n == 0 || hi - lo < 2;
}

// Linear fade in/out over `fade` samples at each end of a `total`-sample sound.
inline float fadeGain(size_t i, size_t total, size_t fade) {
  if (fade == 0) return 1;
  if (i < fade) return static_cast<float>(i) / fade;
  const size_t left = total - 1 - i;
  if (left < fade) return static_cast<float>(left) / fade;
  return 1;
}

inline int16_t scale(int16_t s, int volume) {
  return static_cast<int16_t>(static_cast<int32_t>(s) * volume / 100);
}

// n samples of a sine; `phase` carries on across blocks.
inline void tone(int16_t* out, size_t n, float hz, float amp, float& phase) {
  const float step = 2 * static_cast<float>(M_PI) * hz / kRate;
  for (size_t i = 0; i < n; i++) {
    out[i] = static_cast<int16_t>(amp * sinf(phase));
    phase += step;
    if (phase > 2 * static_cast<float>(M_PI)) phase -= 2 * static_cast<float>(M_PI);
  }
}

}  // namespace dsp
```

**Step 4: `firmware/include/servo_math.h`**

```cpp
#pragma once
// Hobby-servo maths (MG90S): angle -> pulse -> LEDC duty, and eased moves. Pure logic.
#include <math.h>
#include <stdint.h>

#include "ease.h"

namespace servo {

constexpr uint32_t kFreqHz = 50;
constexpr uint8_t kBits = 14;
constexpr uint32_t kPeriodUs = 1000000 / kFreqHz;  // 20 ms

inline float clampAngle(float deg, float limit) {
  return deg > limit ? limit : (deg < -limit ? -limit : deg);
}

// -90..+90 degrees -> 500..2500 us (centre 1500).
inline uint32_t angleToPulseUs(float deg) {
  deg = clampAngle(deg, 90);
  return static_cast<uint32_t>(1500 + deg * 1000.0f / 90.0f + 0.5f);
}

inline uint32_t pulseToDuty(uint32_t us) { return us * ((1u << kBits) - 1) / kPeriodUs; }

// An eased move at a capped speed.
struct Motion {
  float from = 0;
  float to = 0;
  uint32_t start = 0;
  uint32_t duration = 0;

  void moveTo(float current, float target, uint32_t now, float degPerSec) {
    from = current;
    to = target;
    start = now;
    duration = static_cast<uint32_t>(fabsf(target - current) / degPerSec * 1000);
  }
  float at(uint32_t now) const {
    return ease::lerp(from, to, ease::inOutCubic(ease::progress(now, start, duration)));
  }
  bool done(uint32_t now) const { return static_cast<uint32_t>(now - start) >= duration; }
};

}  // namespace servo
```

**Step 5: Run the tests and see them pass.** Expected: `19 Tests 0 Failures`.

**Step 6: Commit.** Message: `Add audio and servo maths, with on-board tests`

---

### Task 5: The speaker, plus the `tone` and `vol` commands

**Files:**
- Create: `firmware/src/audio.h`
- Create: `firmware/src/audio.cpp`
- Modify: `firmware/src/main.cpp`

**Step 1: `firmware/src/audio.h`**

```cpp
#pragma once
// I2S speaker (MAX98357A) and mic (INMP441), both 16 kHz 16-bit mono.
#include <stddef.h>
#include <stdint.h>

namespace audio {

bool beginSpeaker();
bool beginMic();
void setVolume(int percent);  // 0-100, clamped; starts at 20 (5 V into 3 W is loud)
int volume();
void play(const int16_t* pcm, size_t n);  // blocking, scaled by volume
void playTone(float hz, uint32_t ms);     // blocking, with 10 ms fades
size_t readMic(int16_t* out, size_t n);   // blocking; samples read (0 = mic not started)

}  // namespace audio
```

**Step 2: `firmware/src/audio.cpp`**, the speaker half. `beginMic` and `readMic` are added in Task 6.

```cpp
#include "audio.h"

#include <Arduino.h>
#include <ESP_I2S.h>

#include <algorithm>

#include "dsp.h"
#include "pins.h"

namespace {

constexpr size_t kBlock = dsp::kRate / 50;  // 20 ms
I2SClass speaker;
bool speakerOk = false;
int vol = 20;

void write(const int16_t* block, size_t n) {
  speaker.write(reinterpret_cast<const uint8_t*>(block), n * sizeof(int16_t));
}

// Without this, the DMA keeps replaying its last buffer (a buzz) after a sound ends.
void writeSilence(uint32_t ms) {
  static const int16_t zeros[kBlock] = {0};
  for (uint32_t done = 0; done < ms; done += 20) write(zeros, kBlock);
}

}  // namespace

namespace audio {

bool beginSpeaker() {
  speaker.setPins(pins::kAmpBclk, pins::kAmpLrc, pins::kAmpDin);
  // Mono data on both slots, so the amp's (L+R)/2 mix gets full level.
  speakerOk = speaker.begin(I2S_MODE_STD, dsp::kRate, I2S_DATA_BIT_WIDTH_16BIT,
                            I2S_SLOT_MODE_MONO, I2S_STD_SLOT_BOTH);
  if (speakerOk) writeSilence(100);
  return speakerOk;
}

void setVolume(int percent) { vol = std::clamp(percent, 0, 100); }

int volume() { return vol; }

void play(const int16_t* pcm, size_t n) {
  if (!speakerOk) return;
  int16_t block[kBlock];
  for (size_t i = 0; i < n; i += kBlock) {
    const size_t m = std::min(kBlock, n - i);
    for (size_t j = 0; j < m; j++) block[j] = dsp::scale(pcm[i + j], vol);
    write(block, m);
  }
  writeSilence(100);
}

void playTone(float hz, uint32_t ms) {
  if (!speakerOk) return;
  const size_t total = static_cast<size_t>(dsp::kRate) * ms / 1000;
  const size_t fade = dsp::kRate / 100;  // 10 ms
  int16_t block[kBlock];
  float phase = 0;
  for (size_t i = 0; i < total; i += kBlock) {
    const size_t m = std::min(kBlock, total - i);
    dsp::tone(block, m, hz, dsp::kToneAmp, phase);
    for (size_t j = 0; j < m; j++) {
      const float g = dsp::fadeGain(i + j, total, fade);
      block[j] = dsp::scale(static_cast<int16_t>(block[j] * g), vol);
    }
    write(block, m);
  }
  writeSilence(100);
}

}  // namespace audio
```

**Step 3: Wire it into `main.cpp`.**
- Add `#include "audio.h"`.
- Add to the help text:
  ```cpp
  Serial.println("  tone [hz]                 1 s test tone (440 Hz default)");
  Serial.println("  vol <0-100>               speaker volume (starts at 20)");
  ```
- Add to `handle()`:
  ```cpp
  } else if (!strcmp(name, "tone")) {
    const float hz = arg ? atof(arg) : 440;
    if (hz < 50 || hz > 8000) {
      Serial.println("Usage: tone [50-8000]");
    } else {
      Serial.printf("Tone %.0f Hz at volume %d\n", hz, audio::volume());
      audio::playTone(hz, 1000);
    }
  } else if (!strcmp(name, "vol")) {
    if (arg) audio::setVolume(atoi(arg));
    Serial.printf("Volume %d\n", audio::volume());
  ```
- Add to `setup()`: `if (!audio::beginSpeaker()) Serial.println("Speaker: not started");`

**Step 4: Build and flash.** Run `serial_cmd.py "tone" 5`. Expected: `Tone 440 Hz at volume 20`, with no crash.

**Step 5: 👀 The user listens.** In the monitor:
- `tone`: a clean 1 s beep, with no click at the start or end and no buzz afterwards.
- `tone 880`: a higher pitch.
- `vol 50` then `tone`: louder.
- `vol 0` then `tone`: silent.

Agree a comfortable default volume with the user; if it isn't 20, change `vol` in `audio.cpp`.

**Step 6: Commit.** Message: `Play test tones through the I2S amp`

---

### Task 6: Mic levels and the record/playback loopback

**Files:**
- Modify: `firmware/src/audio.cpp`
- Modify: `firmware/src/main.cpp`

**Step 1: Add the mic to `audio.cpp`.**
- In the anonymous namespace, add `I2SClass mic;` and `bool micOk = false;`.
- In `namespace audio`, add:

```cpp
bool beginMic() {
  mic.setPins(pins::kMicSck, pins::kMicWs, -1, pins::kMicSd);
  // INMP441 with L/R to GND: 24-bit samples in 32-bit slots, left slot only.
  micOk = mic.begin(I2S_MODE_STD, dsp::kRate, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO,
                    I2S_STD_SLOT_LEFT);
  return micOk;
}

size_t readMic(int16_t* out, size_t n) {
  if (!micOk) return 0;
  static int32_t raw[kBlock];
  size_t done = 0;
  while (done < n) {
    const size_t want = std::min(kBlock, n - done);
    const size_t got =
        mic.readBytes(reinterpret_cast<char*>(raw), want * sizeof(int32_t)) / sizeof(int32_t);
    if (got == 0) break;
    for (size_t j = 0; j < got; j++) out[done + j] = dsp::micTo16(raw[j]);
    done += got;
  }
  return done;
}
```

**Step 2: Add the commands to `main.cpp`.**
- Add `#include <algorithm>`, `#include <iterator>` and `#include "dsp.h"`.
- Add these helpers in the anonymous namespace, above `handle()`:

```cpp
// Any key stops a running test (CR/LF left over from the command line don't count).
bool keyPressed() {
  bool any = false;
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c != '\r' && c != '\n') any = true;
  }
  return any;
}

constexpr size_t kRecSamples = dsp::kRate * 3;  // 3 s = 96 KB, in PSRAM
int16_t* recording = nullptr;
size_t recorded = 0;

void runMicLevels() {
  static int16_t buf[dsp::kRate / 10];  // 100 ms
  Serial.println("Mic levels for 10 s (press any key to stop)");
  const uint32_t start = millis();
  while (millis() - start < 10000 && !keyPressed()) {
    const size_t n = audio::readMic(buf, std::size(buf));
    if (n == 0) {
      Serial.println("Mic: no data (not started)");
      return;
    }
    if (dsp::isFlat(buf, n)) {
      Serial.println("Mic: no signal (all samples equal) - is it wired and soldered?");
      return;
    }
    const dsp::Level lv = dsp::level(buf, n);
    const int bars = constrain(static_cast<int>((lv.rms_db + 60) / 2), 0, 30);  // -60..0 dBFS
    Serial.printf("%6.1f dBFS  peak %6.1f  |%-30.*s|\n", lv.rms_db, lv.peak_db, bars,
                  "##############################");
    eyes::update(millis());
  }
}

void runRecord() {
  if (recording == nullptr) {
    recording = static_cast<int16_t*>(ps_malloc(kRecSamples * sizeof(int16_t)));
  }
  if (recording == nullptr) {
    Serial.println("rec: no memory");
    return;
  }
  Serial.println("Recording 3 s... speak now");
  recorded = 0;
  while (recorded < kRecSamples) {
    const size_t n = audio::readMic(recording + recorded,
                                    std::min<size_t>(dsp::kRate / 50, kRecSamples - recorded));
    if (n == 0) break;
    recorded += n;
  }
  const dsp::Level lv = dsp::level(recording, recorded);
  Serial.printf("Recorded %.1f s, level %.1f dBFS, peak %.1f dBFS. Type 'play'.\n",
                recorded / static_cast<float>(dsp::kRate), lv.rms_db, lv.peak_db);
}

void runPlay() {
  if (recorded == 0) {
    Serial.println("Nothing recorded yet: type 'rec' first");
    return;
  }
  Serial.printf("Playing %.1f s at volume %d\n", recorded / static_cast<float>(dsp::kRate),
                audio::volume());
  audio::play(recording, recorded);
}
```

- Add to the help text:
  ```cpp
  Serial.println("  mic                       mic levels for 10 s");
  Serial.println("  rec / play                record 3 s, play it back");
  ```
- Add to `handle()`:
  ```cpp
  } else if (!strcmp(name, "mic")) {
    runMicLevels();
  } else if (!strcmp(name, "rec")) {
    runRecord();
  } else if (!strcmp(name, "play")) {
    runPlay();
  ```
- Add to `setup()`: `if (!audio::beginMic()) Serial.println("Mic: not started");`

**Step 3: Build and flash, then check without the mic.**
- Run `serial_cmd.py "mic" 6`.
- Expected, with the header not yet soldered: either `Mic: no signal …`, or level lines that don't react. Either way it must not hang.

**Step 4: 👀 The user checks, once the mic is soldered.** This can wait. If the mic isn't ready, commit and come back to it.
- `mic`: the bars rise when you talk and settle low (around −60 to −50 dBFS) when quiet. Tapping near the mic peaks.
- `rec`, say something, then `play`: you hear yourself, clearly and at a sensible level.
- If it's very quiet or clips, adjust `dsp::kMicShift` and update its test.

**Step 5: Commit.** Message: `Read mic levels and add a record/playback loopback`

---

### Task 7: Eased servos and the `servo` command

**Files:**
- Create: `firmware/src/servos.h`
- Create: `firmware/src/servos.cpp`
- Modify: `firmware/src/main.cpp`

**Step 1: `firmware/src/servos.h`**

```cpp
#pragma once
// Pan/tilt MG90S servos: eased, speed-limited moves; PWM stops once a servo arrives.
#include <stdint.h>

namespace servos {

// Blocking demo: pan centre/left/right/centre, then tilt. Returns early if stop() is true.
void sweep(bool (*stop)());

}  // namespace servos
```

**Step 2: `firmware/src/servos.cpp`**

```cpp
#include "servos.h"

#include <Arduino.h>

#include "pins.h"
#include "servo_math.h"

namespace {

constexpr float kSpeed = 90;       // deg/s
constexpr uint32_t kSettleMs = 200;  // keep driving briefly after arriving, then stop PWM
constexpr float kPanLimit = 60;
constexpr float kTiltLimit = 20;  // provisional until the head is built

// One servo. The real position is unknown at power-on, so the first move starts from
// "centre" and may jump.
struct Axis {
  int pin;
  float limit;
  float angle = 0;
  servo::Motion motion;
  bool attached = false;
  bool arrived = false;
  uint32_t arrivedAt = 0;

  void moveTo(float target, uint32_t now) {
    motion.moveTo(angle, servo::clampAngle(target, limit), now, kSpeed);
    arrived = false;
    if (!attached) attached = ledcAttach(pin, servo::kFreqHz, servo::kBits);
  }

  void update(uint32_t now) {
    if (!attached) return;
    angle = motion.at(now);
    ledcWrite(pin, servo::pulseToDuty(servo::angleToPulseUs(angle)));
    if (!motion.done(now)) return;
    if (!arrived) {
      arrived = true;
      arrivedAt = now;
    } else if (now - arrivedAt >= kSettleMs) {
      ledcDetach(pin);  // no PWM at rest: no jitter, no buzz
      attached = false;
    }
  }
};

Axis pan{pins::kServoPan, kPanLimit};
Axis tilt{pins::kServoTilt, kTiltLimit};

bool run(Axis& axis, float target, bool (*stop)()) {
  axis.moveTo(target, millis());
  while (axis.attached) {
    axis.update(millis());
    if (stop()) {
      if (axis.attached) ledcDetach(axis.pin);
      axis.attached = false;
      return false;
    }
    delay(5);
  }
  return true;
}

}  // namespace

namespace servos {

void sweep(bool (*stop)()) {
  const float panSteps[] = {0, -45, 45, 0};
  const float tiltSteps[] = {0, -15, 15, 0};
  for (float a : panSteps) {
    if (!run(pan, a, stop)) return;
  }
  for (float a : tiltSteps) {
    if (!run(tilt, a, stop)) return;
  }
}

}  // namespace servos
```

**Step 3: Wire it into `main.cpp`.**
- Add `#include "servos.h"`.
- Add help: `Serial.println("  servo                     eased pan/tilt sweep");`
- Add to `handle()`:
  ```cpp
  } else if (!strcmp(name, "servo")) {
    Serial.println("Servo sweep (press any key to stop)");
    servos::sweep(keyPressed);
    Serial.println("Servo sweep done");
  ```

**Step 4: Build and flash.**
- Run `serial_cmd.py "servo" 12`. Expected: `Servo sweep done` after about 6 s. Nothing is connected yet, so there's no motion.

**Step 5: 👀 The user checks, once the servos are wired.** This can wait.
- Wiring: servos on 5 V with a 1000 µF capacitor and a common ground.
- `servo`: smooth, eased moves with no snapping, staying within ±45° pan and ±15° tilt.
- The servos are quiet and don't jitter at rest.

**Step 6: Commit.** Message: `Add eased servo moves that stop PWM at rest`

---

### Task 8: Docs

**Files:**
- Modify: `docs/decisions.md`
- Modify: `docs/status.md`
- Modify: `CLAUDE.md`
- Memory: `m1-resume-point.md`

**Step 1: Append a `## 2026-11-24 — M5 firmware bring-up` section to `decisions.md`.** Don't edit older entries. Cover:
- **Core:** pioarduino 55.03.312-1 (Arduino core 3.3.12) on the `esp32-s3-devkitc1-n16r8` board definition. Core 4.0 is too new; the official platform is frozen at 2.x.
- **PlatformIO setup on Windows:**
  - Core 6.2 via `pioarduino==6.2.0`, not `.post1`;
  - long paths turned on;
  - PowerShell only.
- **LovyanGFX 1.2.36.** Its anti-aliased tools are `fillSmoothCircle` and `drawWedgeLine`; there is no smooth arc.
- **The eye model:** two lid bulge values plus size, thickness and gaze. A ring, `∩`, `‿` and a line all come from one model, and blends are plain lerps.
- **Eyes redraw only when something moves.** The measured frame rate goes here (about 23 fps in the spike).
- **One app with a serial menu**, and unit tests run on the board (there's no PC compiler).
- **Audio settings:** mic shift 14 (+12 dB), the speaker on both slots, silence written after each sound, and the default volume.
- **Servos:** eased, 90 °/s, and PWM detached after a 200 ms settle.
- **Dropped:** the PSRAM strip-drawing fallback, since PSRAM is confirmed (8 MB octal).

**Step 2: Update `CLAUDE.md`.**
- **Repo layout:** add under `firmware/` the lines `include/` (pins.h plus the pure logic headers), `src/` (main menu, eyes, audio, servos), `test/` (Unity, on the board) and `scripts/serial_cmd.py`.
- **Firmware constraints:** add the stack line (pioarduino core 3.3.12, LovyanGFX 1.2.36, board `esp32-s3-devkitc1-n16r8`) and "Build from PowerShell, not Git Bash (see firmware/README.md)".

**Step 3: Update `docs/status.md`.** Add a new top section, "M5 firmware bring-up". List each hardware check as passed, or pending (mic and servos until they're wired).

**Step 4: Update the memory file.** In `m1-resume-point.md`, record M5 progress, the next steps, and the Windows/pioarduino gotchas.

**Step 5: Check and commit.**
- Run `pio run` and `pio test` (all tests pass).
- Run `uv run pytest -q` in `server/`.
- Commit with the message `Document M5 firmware bring-up`.
