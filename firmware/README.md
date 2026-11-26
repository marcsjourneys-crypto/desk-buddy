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
| `config` | The settings, with the password and token masked (`****` plus the token's last 2 characters), and the saved wake word settings. |
| `net` | Wi-Fi status, IP and signal strength; the link state (`NoConfig`, `WifiConnecting`, `Connecting`, `WaitingWelcome`, `Linked`, `Backoff`, `Refused`); reconnects; mic frames sent and dropped; wake mode (see below); free heap. |
| `forget` | Wipes the Wi-Fi, server and token (not the wake word settings). |

What to expect in the monitor: `Wi-Fi: joining …`, `Wi-Fi connected <ip>`, then `Linked to <server>`, and the eyes wake up. At boot the firmware also prints `Last reset:` (power-on, software restart, or a crash such as `PANIC`), so a crash reboot can be told from a power-on. CARL greets the robot unless it only just started. Hold Right Ctrl in the CARL window on the PC to talk.

**Never paste the `wifi` or `token` lines into a chat, an issue or a screenshot.** The firmware never prints them back in full.

**If it doesn't link:**
- `Wi-Fi: still joining … (network not found)`: check the name (2.4 GHz only). `connect failed (password?)`: check the password.
- `Can't connect to …, retry in Ns`: CARL isn't running, the IP is wrong, `SERVER_HOST` isn't `0.0.0.0`, or the firewall rule is missing. Without the rule each attempt also hangs for about 5 s.
- `Server refused the robot (bad token or protocol) - check: token <value>`: the token doesn't match `ROBOT_TOKEN`. After repeated bad tokens the server also blocks the robot's IP for 60 s. The robot retries every 5 minutes, or at once after a new `token` command.
- While the link is connected, `tone`, `mic`, `rec`, `play`, `servo`, `fps`, `wake test` and `wake raw` are refused (they would fight the link for the speaker, the mic, the wake engine or the main loop). `wake info`, `wake on|off` and `wake threshold` work at any time.

## Wake word ("Hey CARL")

From `../docs/plans/2026-11-25-wake-word.md`: TFLite Micro (prebuilt in Arduino core 3.3.12: `esp-tflite-micro` 1.4.0, `esp-nn` 1.3.2) and TFLM's audio microfrontend (vendored in `lib/microfrontend`, see its README) run a microWakeWord model on the robot. Code: `src/wake.cpp` (the engine), wake mode in `src/net.cpp`, the mic reader and pre-roll in `src/audio.cpp`; the pure logic in `include/wake_logic.h`, `include/wake_mode.h` and `include/pre_roll.h`.

**Put the model in** (each time it changes), in `firmware/`:
1. Copy the trained `stream_state_internal_quant.tflite` to `models/hey_carl.tflite`.
2. Check `models/hey_carl.json` (cutoff, window size, arena size).
3. `python scripts/model_to_header.py --ops` writes `models/hey_carl_model.h` and lists the model's operators.
4. Build and flash (`pio run -t upload`).

Without the header the firmware still builds; `wake test` then says there's no model, and wake mode stays off. The model and the header are gitignored (the training data is for personal use only), so a fresh clone has neither.

**Retraining** (other spellings, more samples, or after false wakes):
1. Train in Google Colab with the microWakeWord notebook (`notebooks/basic_training_notebook.ipynb` in `OHF-Voice/micro-wake-word`, or the community one-config notebook), `target_word = "hey carl"`; see Task 0.1 in the plan for the steps. A T4 GPU is faster; the CPU works too.
2. Download `stream_state_internal_quant.tflite` and copy it to `models/hey_carl.tflite`.
3. Update `models/hey_carl.json` if the notebook's cutoff, window size or arena size changed.
4. `python scripts/model_to_header.py --ops` (every operator must be registered; a `MISSING` one needs adding in `src/wake.cpp`).
5. Rebuild and flash (`pio run -t upload`, from PowerShell, with CARL stopped so COM is free), then check with `wake info` and `wake test` before linking.

### Wake mode (normal use)

While the robot is linked and CARL is **idle**, the robot listens for "Hey CARL" by itself:

1. **It hears the phrase:** the eyes switch to `listening` at once, and the robot sends `{"type": "wake", "score": 0.99}` to the server. `net` counts it under "wakes sent".
2. **The server answers `state listening`:** the robot streams the audio kept since the detection first, starting 300 ms before the detecting step so a question said in one breath keeps its first word (that start may hold the end of "CARL"; the server handles it). It goes out faster than real time, then live audio follows, as with the PC key. The pre-roll holds 3 s (96 KB of PSRAM).
3. **No answer within 2 s** (the server ignored it, e.g. it wasn't idle): the audio is dropped, the eyes go back to the state's look, and listening resumes after 2 s ("unanswered").

- **When it listens:** only while linked, the server's state is `idle`, and the wake word is on. It pauses while listening, thinking and speaking, and ignores detections for **2 s after any state change** (and after each restart of the engine): "ignored" in `net`.
- **Privacy:** no audio leaves the robot until the phrase is heard, the PC key is pressed, or the server opens a follow-up window after a hands-free reply (it sends `state listening`, as for any question). The pre-roll's audio is only ever sent after a detection the server answered.
- **The PC key** works as before, with the wake word on or off.

| Command | Does |
|---|---|
| `wake on` / `wake off` | Turns wake mode on or off. Saved; on by default once a model is built in. |
| `wake threshold <0.50-0.99>` | How sure the model must be (the sliding window's mean). Saved. Higher means fewer false wakes but more misses. Until set, the model's own cutoff (0.97) applies. |

- Both apply at once, and also while linked. **The server can override both** with `wake_config` (sent after `welcome`, from the dashboard): the override lasts until the robot restarts and isn't saved. `config` shows the saved values, `net` the ones in use and where they came from (`saved`, `server` or `model`).
- `net` shows wake mode as `listening`, `listening (holding off)`, `heard, waiting for the server`, `paused (state …)`, `paused (no link)`, `off`, `no model built in` or `engine failed`, plus the counters: wakes sent, unanswered, ignored.
- The engine starts at boot when the wake word is on (it takes ~30 KB of internal RAM, so it claims it before Wi-Fi), or the first time it's turned on later.

**Who reads the mic.** One task, the mic reader (`audio.cpp`), is the only reader of the I2S mic while the link is up. It hands each 20 ms frame to whoever the mode says: the wake engine plus the pre-roll ring (idle), only the pre-roll (a wake was sent, waiting for the server), or the link (listening; after a wake, the pre-roll's kept frames go first and live frames queue behind them). The wake engine and the link never read the mic themselves, so they can't overlap, and switching between them loses no audio. `wake test`/`wake raw` use the same reader while the link is down.

| Command | Shows |
|---|---|
| `wake info` | The model: wake word, size, operators (a missing one is marked `MISSING`), input/output shape and quantisation; each arena's size, use and place (internal RAM, or PSRAM as a fallback); the internal heap before and after the engine started, and now. |
| `wake test` | `HEARD (score 0.98)` on each detection (the eyes flash surprised), and every 3 s: model step time (avg, max), frontend time per 20 ms frame, core 0 load, eye fps, free heap and PSRAM. Any letter stops it. Uses the threshold in use (see above). |
| `wake raw` | The model's probability and the window's score 10 times a second, for tuning the cutoff. |

- `wake test` and `wake raw` are refused while the robot link is connected (wake mode owns the engine and the mic then). Nothing fires for 2 s after a start or a detection.
- The eyes play the animated "thinking" look during the test, so the fps shows what detection costs them (the cap is ~30 fps; the target is ≥ 20).
- **Memory:** both arenas (the tensors, and the model's streaming state) are in internal RAM, after one unexplained "Cache error, MMU entry fault" crash with them in PSRAM in Phase 0. At start the engine builds the model once on roomy PSRAM arenas to measure what `AllocateTensors` really uses (the streaming state's buffers are allocated there too), then rebuilds it on internal arenas of that size plus a quarter (at least 2 KB). If internal RAM is short, an arena stays in PSRAM and a `WARNING` is printed. In the measuring pass the tensor arena starts at the manifest's `tensor_arena_size` and doubles up to 256 KB if `AllocateTensors` fails; set it to a bit over what `wake info` says is used.

### Feature check (design note, not built yet)

The model only works if the robot's features match training. Plan for `scripts/wake_features_check.py`:
1. A `wake dump` command records ~3 s from the mic, then prints the raw PCM (base64) and the 40-value feature frames the board computed from it, between marker lines.
2. The script (run with `serial_cmd.py`'s port handling) saves the PCM as a WAV and computes features from that WAV with `pymicro_features` (the same microfrontend, as Python bindings; it's a dev-only tool in `tools/wakeword/`, never a server dependency).
3. It compares frame by frame: the frame count must match, and each value should be within ±1 (fixed-point rounding); it prints the worst frame and the mean absolute difference. A systematic offset means a setting differs (window, PCAN, noise reduction); a level offset points at the mic scaling (`dsp::kMicShift`).

A known WAV can also be checked without the mic by streaming it to the board instead of recording.

## Windows setup notes (one-time, done 2026-11-24)

- **Long paths.** Core 3.x has paths over 260 characters. Turn on long paths once, from any PowerShell window (it shows a UAC prompt):
  `Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -Command Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1"`
- **PlatformIO Core 6.2 (pioarduino).** The platform needs Core 6.2 or newer, and the platform's own installer can't replace the running core on Windows.
  - Install it with PlatformIO closed:
    `& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" -m pip install "pioarduino==6.2.0"`
  - Don't install `6.2.0.post1`. PlatformIO's semver check reads 6.2.0.post1 as a pre-release of 6.2.0, so the platform refuses it.
  - If `pio` breaks with a missing module (urllib3, certifi…), reinstall with `pip install --ignore-installed "pioarduino==6.2.0"`.
- **Use PowerShell, not Git Bash.** ESP-IDF's tool installer refuses MSYS/MinGW shells ("MSys/Mingw is not supported"), and the compiler is then missing.
