# Robot ↔ server protocol

Final as of M4 (2026-11-24), updated in M6 when the real robot joined (2026-11-24), and for the wake word (2026-11-26). The server code is in `server/src/desk_buddy/server.py` and `server/src/desk_buddy/link/`. Two clients follow it:
- the robot firmware (`firmware/src/net.cpp`);
- the fake robot (`python -m desk_buddy.fake_robot`), a reference client in Python.

## Overview

- **One WebSocket per robot:** `ws://<host>:<port>/robot`. By default `SERVER_HOST=127.0.0.1` and `SERVER_PORT=8765`; the real robot needs `SERVER_HOST=0.0.0.0` (see [Going on the LAN](#going-on-the-lan)).
- **Text frames** are JSON objects with a `"type"` field (control messages).
- **Binary frames** are audio: 16 kHz, 16-bit signed little-endian mono PCM.
- **The server is the source of truth.** It decides the state; the robot only shows it, sends audio (and button presses, if it has a button), and plays what it is sent.
- **One robot at a time.** While a robot is connected, the server uses the robot's mic and speaker. When it disconnects, the PC mic and speaker take over again. Conversation memory survives reconnects.
- **The talk button is the PC key** (Right Ctrl by default, `PTT_KEY`), held in the CARL window (the server's). It drives a connected robot as well as the PC. A robot button or touch pad is optional and can send `button` later.
- **The wake word** ("Hey CARL") is detected on the robot, which sends `wake`; the server decides whether to listen. The key always wins over a wake.

## Connecting

1. The robot opens the WebSocket.
2. Within **5 s**, its first frame must be a `hello` text frame:
   ```json
   {"type": "hello", "token": "<ROBOT_TOKEN>", "fw_version": "0.2.0-m6"}
   ```
   - `token`: the same value as `ROBOT_TOKEN` in the server's `.env` (at least 16 characters, at most 256). It is compared in constant time and never logged. The firmware also refuses tokens with spaces, quotes or backslashes.
   - `fw_version`: optional, any string. The server keeps the first 64 characters for its log.
3. The server replies `welcome`, then (only if the server has wake word settings) `wake_config`, then the current `state`:
   ```json
   {"type": "welcome"}
   {"type": "wake_config", "enabled": true, "threshold": 0.97}
   {"type": "state", "name": "idle"}
   ```
4. Messages then flow both ways until either side closes.

If `ROBOT_TOKEN` is blank, the server doesn't listen at all and logs `Robot link: off (no ROBOT_TOKEN)`.

### Close codes

| Code | Meaning | What the robot should do |
|---|---|---|
| `4001` | Bad token, or this IP is blocked after repeated failures (see [Hardening](#hardening)). | Stop retrying; the token needs fixing. |
| `4002` | No valid `hello` within 5 s. This covers a binary first frame, bad JSON, a different message type, and a token that isn't valid UTF-8. Also sent when too many connections are already waiting for their hello. | Fix the firmware; retrying with the same frame fails again. |
| `4003` | Replaced by a newer connection with a valid token. | Don't reconnect from this socket; the newer one is in charge. |
| `1012` | The server is shutting down or restarting. | Reconnect with a back-off. |
| `1000` | Normal close. | Reconnect with a back-off. |
| `1011` | Server-side send failure, or the send queue overflowed (the robot isn't reading fast enough). | Reconnect with a back-off. |

**The newest connection wins.** After a Wi-Fi drop, the server may not notice the old socket is dead for up to 40 s (two missed pings). Letting the reconnect replace it means the robot is back straight away. The replaced turn is interrupted, as with any disconnect.

**The firmware can't see close codes.** The WebSocket library it uses (links2004/WebSockets 2.7.3) reads the code only for a debug log and reports every close the same way. So the firmware infers a refusal from timing instead; see [Robot reconnect rules](#robot-reconnect-rules).

## Robot → server

| Message | Example | Notes |
|---|---|---|
| `hello` | `{"type": "hello", "token": "…", "fw_version": "0.2.0-m6"}` | First frame only. A later `hello` is logged and ignored. |
| `button` | `{"type": "button", "action": "down"}` / `{"type": "button", "action": "up"}` | **Optional** push-to-talk on the robot (the M6 robot has none; the PC key does this). Down starts a recording; up ends it and starts the turn. Down while CARL is thinking or speaking interrupts it. Rate-limited (see [Hardening](#hardening)). |
| `touch` | `{"type": "touch"}` | The head touch pad. Logged only (not used yet). |
| `stats` | `{"type": "stats", "data": {"rssi": -58, "heap": 123456}}` | Free-form diagnostics. `data` must be an object; it is logged at debug level. |
| `wake` | `{"type": "wake", "score": 0.99}` | The robot heard the wake word. `score` (required) is the detector's averaged probability, a number from 0 to 1. Logged as `Robot: wake (score 0.99)`. Rate-limited to one every 2 s (see [Hardening](#hardening)). See [Wake word](#wake-word). |

**Audio up:** binary frames of 20 ms (640 bytes) of PCM.
- **The robot streams while the state is `listening`,** and stops when the state changes. No extra message is needed: the server's state says when to listen. The firmware flushes stale mic audio and removes the mic's DC offset before streaming.
- **The server keeps frames only while a recording is open:** from the PC key's press (or `button down`) to its release, or from an accepted `wake` to the end of the question. Frames at other times are dropped silently, as are frames with an odd number of bytes.
- **The PC key keeps the robot's mic open 250 ms after release,** so the last frames still in flight over Wi-Fi arrive before the turn starts. Pressing again within those 250 ms ends the first clip at once (it is then too short and is dropped as a tap, by design).
- A recording is capped at 30 s; audio after that is dropped and the turn uses the first 30 s. A press shorter than 0.3 s is ignored as a tap.

### Wake word

The robot runs the detector only while the state is `idle`. On a detection:

1. The robot's eyes open at once, and it sends `{"type": "wake", "score": 0.99}`. It keeps buffering its mic from about 300 ms before the moment of detection (a pre-roll of up to 3 s, in PSRAM).
2. If the server accepts the wake (CARL is idle), it sends `state listening`. The robot then streams **the buffered audio first**, then live audio, as 20 ms binary frames as usual. The buffer starts 300 ms before the detection, so a question said in one breath keeps its first word; its first ~300 ms may therefore contain the end of the phrase, which the server handles. Nothing is sent before `state listening`, and nothing after the state leaves `listening`.
3. The server ends the question itself when the speech ends (about 0.8 s of silence), after 4 s with no speech (a false wake: back to `idle`), or after 15 s at most. It sends `state thinking` (or `idle`) and the robot stops streaming, exactly as after a key release.

A `wake` while CARL isn't idle is ignored, and the robot drops its pre-roll if `state listening` doesn't come. Pressing the PC key during a wake recording takes over as push-to-talk. No audio leaves the robot until the phrase is heard or the key is pressed.

## Server → robot

| Message | Example | Notes |
|---|---|---|
| `welcome` | `{"type": "welcome"}` | Hello accepted. |
| `state` | `{"type": "state", "name": "listening"}` | One of `idle`, `listening`, `thinking`, `speaking`. `sleeping` is reserved. |
| `emotion` | `{"type": "emotion", "name": "happy"}` | Sent once per reply, before `state speaking`. One of `neutral`, `happy`, `sad`, `surprised`, `sleepy`, `thinking`, `curious`. |
| `audio_start` | `{"type": "audio_start", "rate": 16000}` | One sentence of speech follows as binary frames. |
| `audio_end` | `{"type": "audio_end"}` | The sentence's audio is complete; play out what is buffered. |
| `audio_stop` | `{"type": "audio_stop"}` | Interrupted: drop everything buffered and stop playing now. |
| `look` | `{"type": "look", "pan": 20, "tilt": -5, "speed": 0.5}` | **Reserved** for the servos (not wired yet). |
| `led` | `{"type": "led", "mode": "pulse"}` | **Reserved** for the antenna LED (not wired yet). |
| `volume` | `{"type": "volume", "level": 70}` | **Reserved.** |
| `wake_config` | `{"type": "wake_config", "enabled": true, "threshold": 0.97}` | The wake word on or off, and its detection threshold (a number from 0.5 to 0.99). Sent right after `welcome` when the server has these settings, and again whenever they change. It overrides the robot's own NVS settings until the robot restarts; without it the robot uses its NVS settings. |

The robot should ignore any message type it doesn't know, so new ones can be added without breaking old firmware.

A typical turn, as the robot sees it:

```
state listening      (PC key pressed: the robot starts streaming its mic)
state thinking       (key released: the robot stops streaming)
emotion curious
state speaking
audio_start, binary × n, audio_end     (sentence 1)
audio_start, binary × n, audio_end     (sentence 2)
state idle
```

**Audio down:** each sentence is `audio_start`, then binary frames of **100 ms (3200 bytes**; the last may be shorter), then `audio_end`.
- The server paces the frames at real time, about **200 ms ahead** of playback, so the robot needs only a small buffer (the firmware has 1 s) and an interrupt takes effect quickly.
- On `audio_stop`, up to about 200 ms of audio that was already queued may still arrive afterwards; the robot should drop audio frames until the next `audio_start`.
- **Why 100 ms, not 20 ms:** the ESP32's TCP stack (lwIP) queues at most 6 received segments per socket (`CONFIG_LWIP_TCP_RECVMBOX_SIZE`), with a 5760-byte window and a 250 ms TCP timer. Sending one small segment per 640-byte frame capped the robot at about 31 frames a second, below real time. After about 7 s of speech the server's send queue filled and the link closed with `1011`. Full-size segments hit the window limit first instead. Measured on the first robot voice check, 2026-11-24 (`ROBOT_CHUNK_BYTES` in `link/robot.py`).
- The firmware waits for about 60 ms of a sentence before it starts playing, to ride out network jitter. `audio_end` plays a shorter sentence anyway.

## Limits and robustness

- **Frames are capped at 64 KB.** A larger frame closes the connection (code `1009`).
- **Pings:** the server pings every 20 s and closes the connection if no pong arrives within 20 s. WebSocket libraries answer pings automatically. The firmware also pings every 15 s and drops the link after two missed pongs (5 s each).
- **Bad frames are never fatal.** Malformed JSON, an unknown `type` or a bad field is ignored, and logged once per burst (again after 1 s without one).
- **Recording cap:** 30 s per recording.
- **Shutdown:** on Ctrl+C the server closes connections with `1012` and waits at most 5 s. Closing one link waits at most 2 s.

### Hardening

Added in M6, when the server moved onto the LAN. All of it is in memory only and resets on restart; the numbers are constants in `server.py` and `link/robot.py`.

- **Bad-token throttle, per client IP:** 5 failures within 60 s block that IP for 60 s. A missing or malformed hello (a timeout included) counts as a failure too. A blocked IP is accepted and closed with `4001` at once, before any hello is read, and the block is logged once. A successful hello clears that IP's failures.
- **Half-open cap:** at most 4 connections may wait for their hello at once, and at most 2 from one IP. Extra connections are closed with `4002` at once.
- **Wake limit:** one `wake` every 2 s per connection; extra ones are dropped and logged once per burst (again only after 2 s without drops).
- **Button flood limit:** 10 `button down` messages a second, with a burst of 10; extra downs are dropped and logged once per burst. `up` is never limited: an `up` without its `down` is harmless, because the orchestrator ignores an `up` when it isn't listening.
- **Send-queue cap:** 128 queued items. On overflow the server logs a warning once, closes the link with `1011` and drops further sends; the robot reconnects.

### Robot reconnect rules

What the firmware does (`firmware/src/net.cpp`, rules in `firmware/include/link_policy.h`):

- **Back-off:** after any close or failed connect it waits 1 s, then 2, 4, 8, 16 and 30 s (the most). A `welcome` resets it.
  - Exception: if the link has dropped within 2 s of `welcome` three times in a row (two robots replacing each other, say), `welcome` stops resetting the back-off.
- **Refused:** the close code isn't visible to the firmware (see above), so a close that arrives **less than 1 s after `hello`** stands in for `4001`/`4002`.
  - Three of those in a row stop the retries, and the robot prints `Server refused the robot (bad token or protocol) - check: token <value>`.
  - It tries again every 5 minutes, and a new `wifi`, `server` or `token` command retries at once.
  - A server that doesn't answer `hello` within 5 s counts as an ordinary failure and is retried with the back-off.
- **Replaced (`4003`):** arrives after `welcome`, so the robot backs off and reconnects as for any drop.
- **Wi-Fi:** the robot rejoins by itself. While it's off the eyes sleep and the server uses the PC mic and speaker.
- **A connect is blocking** for up to 5 s. With the firewall rule in place a stopped server refuses at once; without it Windows silently drops the connect, and each attempt takes the full 5 s.

## Going on the LAN

The server listens on localhost by default. For the real robot:

1. In `server/.env`, set `SERVER_HOST=0.0.0.0`. At startup the server logs the PC's LAN address and the robot command to use:
   `Robot link: on (ws://192.168.1.20:8765/robot — set the robot with: server 192.168.1.20:8765)`.
2. Allow the port on **Private** networks only, once, as administrator. In an elevated **PowerShell**:
   ```powershell
   New-NetFirewallRule -DisplayName "Desk Buddy robot link" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
   ```
   Or in an elevated **Command Prompt** (cmd), where `New-NetFirewallRule` doesn't exist:
   ```
   netsh advfirewall firewall add rule name="Desk Buddy robot link" dir=in action=allow protocol=TCP localport=8765 profile=private
   ```
3. Make sure the Wi-Fi network is set to **Private** (Settings → Network & internet → Wi-Fi → your network → Network profile type).
4. Point the robot at the PC over its serial menu: `server <the PC's LAN IP>:8765` (see `firmware/README.md`). A DHCP reservation for the PC in the router keeps that IP from changing.

The link is plain `ws://`, not TLS: the token protects against other devices on the home network, but not against someone who can watch its traffic. Don't expose the port to the internet.
