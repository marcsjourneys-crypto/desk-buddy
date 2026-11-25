# Firmware

ESP32-S3 firmware for the Desk Buddy robot: PlatformIO, Arduino core 3.x (pioarduino), LovyanGFX, links2004/WebSockets and ArduinoJson. It joins the home Wi-Fi, links to Buddy's `/robot` WebSocket (`../docs/protocol.md`), and is Buddy's mic, speaker and eyes. The serial test menu from the M5 bring-up is still there (type `help`).

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

**PowerShell or Command Prompt?** The `& "$env:USERPROFILE\…"` form above only works in PowerShell. In a Command Prompt (cmd) use `"%USERPROFILE%\.platformio\penv\Scripts\pio.exe" run` instead. Simplest in either: add the folder to PATH for the session and type plain `pio`:
- PowerShell: `$env:Path += ";$env:USERPROFILE\.platformio\penv\Scripts"`
- cmd: `set PATH=%PATH%;%USERPROFILE%\.platformio\penv\Scripts`

## Connect the robot

The robot needs three settings: the Wi-Fi network, the PC running CARL (the server), and the robot token. Type them once into the serial monitor (`pio device monitor -p COM9`). They are saved on the robot (in NVS), survive reflashing, and never go into the code. The robot connects as soon as all three are set.

1. **On the PC:** follow "Real robot" in `../server/README.md`: `SERVER_HOST=0.0.0.0` in `server/.env`, the firewall rule (once, as administrator), and start CARL. Its log shows the line to type in step 3.
2. `wifi <name> <password>`: the Wi-Fi network.
   - The name can't contain spaces.
   - The password is everything after the space that follows the name, so it may contain spaces, but no tabs. It must be 8–63 characters, or a 64-digit hex key. Use `-` for an open network.
   - The ESP32-S3 only does 2.4 GHz Wi-Fi.
3. `server <PC's LAN IP>:8765`: copy it from CARL's startup log (`set the robot with: server …`). The PC's IP can change, so give the PC a **DHCP reservation** in the router.
4. `token <value>`: `ROBOT_TOKEN` from `server/.env` (16–256 characters, no spaces, quotes or backslashes).

Then:

| Command | Shows |
|---|---|
| `config` | The settings, with the password and token masked (`****` plus the token's last 2 characters). |
| `net` | Wi-Fi status, IP and signal strength; the link state (`NoConfig`, `WifiConnecting`, `Connecting`, `WaitingWelcome`, `Linked`, `Backoff`, `Refused`); reconnects; mic frames sent and dropped; free heap. |
| `forget` | Wipes the Wi-Fi, server and token. |

What to expect in the monitor: `Wi-Fi: joining …`, `Wi-Fi connected <ip>`, then `Linked to <server>`, and the eyes wake up. CARL greets the robot unless it only just started. Hold Right Ctrl in the CARL window on the PC to talk.

**Never paste the `wifi` or `token` lines into a chat, an issue or a screenshot.** The firmware never prints them back in full.

**If it doesn't link:**
- `Wi-Fi: still joining … (network not found)`: check the name (2.4 GHz only). `connect failed (password?)`: check the password.
- `Can't connect to …, retry in Ns`: CARL isn't running, the IP is wrong, `SERVER_HOST` isn't `0.0.0.0`, or the firewall rule is missing. Without the rule each attempt also hangs for about 5 s.
- `Server refused the robot (bad token or protocol) - check: token <value>`: the token doesn't match `ROBOT_TOKEN`. After repeated bad tokens the server also blocks the robot's IP for 60 s. The robot retries every 5 minutes, or at once after a new `token` command.
- While the link is connected, `tone`, `mic`, `rec`, `play`, `servo` and `fps` are refused (they would fight the link for the speaker, the mic or the main loop).

## Windows setup notes (one-time, done 2026-11-24)

- **Long paths.** Core 3.x has paths over 260 characters. Turn on long paths once, from any PowerShell window (it shows a UAC prompt):
  `Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -Command Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1"`
- **PlatformIO Core 6.2 (pioarduino).** The platform needs Core 6.2 or newer, and the platform's own installer can't replace the running core on Windows.
  - Install it with PlatformIO closed:
    `& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" -m pip install "pioarduino==6.2.0"`
  - Don't install `6.2.0.post1`. PlatformIO's semver check reads 6.2.0.post1 as a pre-release of 6.2.0, so the platform refuses it.
  - If `pio` breaks with a missing module (urllib3, certifi…), reinstall with `pip install --ignore-installed "pioarduino==6.2.0"`.
- **Use PowerShell, not Git Bash.** ESP-IDF's tool installer refuses MSYS/MinGW shells ("MSys/Mingw is not supported"), and the compiler is then missing.
