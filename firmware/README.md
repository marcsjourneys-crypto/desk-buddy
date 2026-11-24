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
| Serial monitor (type `help`) | `pio device monitor -p COM9` (naming the port stops it grabbing a Bluetooth COM port when the board is missing) |
| Unit tests (they run on the board) | `pio test` |

Close the serial monitor before flashing or testing; only one program can use the port.

## Windows setup notes (one-time, done 2026-11-24)

- **Long paths.** Core 3.x has paths over 260 characters. Turn on long paths once, from any PowerShell window (it shows a UAC prompt):
  `Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -Command Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1"`
- **PlatformIO Core 6.2 (pioarduino).** The platform needs Core 6.2 or newer, and the platform's own installer can't replace the running core on Windows.
  - Install it with PlatformIO closed:
    `& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" -m pip install "pioarduino==6.2.0"`
  - Don't install `6.2.0.post1`. PlatformIO's semver check reads 6.2.0.post1 as a pre-release of 6.2.0, so the platform refuses it.
  - If `pio` breaks with a missing module (urllib3, certifi…), reinstall with `pip install --ignore-installed "pioarduino==6.2.0"`.
- **Use PowerShell, not Git Bash.** ESP-IDF's tool installer refuses MSYS/MinGW shells ("MSys/Mingw is not supported"), and the compiler is then missing.
