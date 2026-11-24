# Robot ↔ server protocol

Final as of M4 (2026-11-24). The server code is in `server/src/desk_buddy/server.py` and `server/src/desk_buddy/link/`. The fake robot (`python -m desk_buddy.fake_robot`) is a working reference client.

## Overview

- **One WebSocket per robot:** `ws://<host>:<port>/robot`. By default `SERVER_HOST=127.0.0.1` and `SERVER_PORT=8765`.
- **Text frames** are JSON objects with a `"type"` field (control messages).
- **Binary frames** are audio: 16 kHz, 16-bit signed little-endian mono PCM.
- **The server is the source of truth.** It decides the state; the robot only shows it, sends button presses and audio, and plays what it is sent.
- **One robot at a time.** While a robot is connected, the server uses the robot's mic, speaker and button, and ignores the PC's push-to-talk key. When it disconnects, the PC mic and speaker take over again. Conversation memory survives reconnects.

## Connecting

1. The robot opens the WebSocket.
2. Within **5 s**, its first frame must be a `hello` text frame:
   ```json
   {"type": "hello", "token": "<ROBOT_TOKEN>", "fw_version": "0.1.0"}
   ```
   - `token`: the same value as `ROBOT_TOKEN` in the server's `.env` (at least 16 characters, at most 256). It is compared in constant time and never logged.
   - `fw_version`: optional, any string. The server keeps the first 64 characters for its log.
3. The server replies `welcome`, then the current `state`:
   ```json
   {"type": "welcome"}
   {"type": "state", "name": "idle"}
   ```
4. Messages then flow both ways until either side closes.

If `ROBOT_TOKEN` is blank, the server doesn't listen at all and logs `Robot link: off (no ROBOT_TOKEN)`.

### Close codes

| Code | Meaning | What the robot should do |
|---|---|---|
| `4001` | Bad token. | Stop retrying; the token needs fixing. |
| `4002` | No valid `hello` within 5 s. This covers a binary first frame, bad JSON, a different message type, and a token that isn't valid UTF-8. | Fix the firmware; retrying with the same frame fails again. |
| `4003` | Replaced by a newer connection with a valid token. | Don't reconnect from this socket; the newer one is in charge. |
| `1012` | The server is shutting down or restarting. | Reconnect with a back-off. |
| `1000` | Normal close. | Reconnect with a back-off. |
| `1011` | Server-side send failure. | Reconnect with a back-off. |

**The newest connection wins.** After a Wi-Fi drop, the server may not notice the old socket is dead for up to 40 s (two missed pings). Letting the reconnect replace it means the robot is back straight away. The replaced turn is interrupted, as with any disconnect.

## Robot → server

| Message | Example | Notes |
|---|---|---|
| `hello` | `{"type": "hello", "token": "…", "fw_version": "0.1.0"}` | First frame only. A later `hello` is logged and ignored. |
| `button` | `{"type": "button", "action": "down"}` / `{"type": "button", "action": "up"}` | Push-to-talk. Down starts a recording; up ends it and starts the turn. Down while Buddy is thinking or speaking interrupts it. |
| `touch` | `{"type": "touch"}` | The head touch pad. Logged only (not used yet). |
| `stats` | `{"type": "stats", "data": {"rssi": -58, "heap": 123456}}` | Free-form diagnostics. `data` must be an object; it is logged at debug level. |
| `wake` | `{"type": "wake"}` | **Reserved** for an on-robot wake word (later). Accepted and logged, not used yet. |

**Audio up:** binary frames of about 20 ms (640 bytes) of PCM. They are only kept between `button down` and `button up`; frames at other times are dropped silently, as are frames with an odd number of bytes. A recording is capped at 30 s; audio after that is dropped and the turn uses the first 30 s. A press shorter than 0.3 s is ignored as a tap.

## Server → robot

| Message | Example | Notes |
|---|---|---|
| `welcome` | `{"type": "welcome"}` | Hello accepted. |
| `state` | `{"type": "state", "name": "listening"}` | One of `idle`, `listening`, `thinking`, `speaking`. `sleeping` is reserved for M6. |
| `emotion` | `{"type": "emotion", "name": "happy"}` | Sent once per reply, before `state speaking`. One of `neutral`, `happy`, `sad`, `surprised`, `sleepy`, `thinking`, `curious`. |
| `audio_start` | `{"type": "audio_start", "rate": 16000}` | One sentence of speech follows as binary frames. |
| `audio_end` | `{"type": "audio_end"}` | The sentence's audio is complete; play out what is buffered. |
| `audio_stop` | `{"type": "audio_stop"}` | Interrupted: drop everything buffered and stop playing now. |
| `look` | `{"type": "look", "pan": 20, "tilt": -5, "speed": 0.5}` | **Reserved** for M6 (servos). |
| `led` | `{"type": "led", "mode": "pulse"}` | **Reserved** for M6 (antenna LED). |
| `volume` | `{"type": "volume", "level": 70}` | **Reserved** for M6. |

The robot should ignore any message type it doesn't know, so new ones can be added without breaking old firmware.

A typical turn, as the robot sees it:

```
state listening      (after button down)
state thinking       (after button up)
emotion curious
state speaking
audio_start, binary × n, audio_end     (sentence 1)
audio_start, binary × n, audio_end     (sentence 2)
state idle
```

**Audio down:** each sentence is `audio_start`, then binary frames of 20 ms (640 bytes; the last may be shorter), then `audio_end`. The server paces the frames at real time, about **200 ms ahead** of playback, so the robot needs only a small buffer (0.5 s is plenty) and an interrupt takes effect quickly. On `audio_stop`, up to about 200 ms of audio that was already queued may still arrive afterwards; the robot should drop audio frames until the next `audio_start`.

## Limits and robustness

- **Frames are capped at 64 KB.** A larger frame closes the connection (code `1009`).
- **Pings:** the server pings every 20 s and closes the connection if no pong arrives within 20 s. WebSocket libraries answer pings automatically.
- **Bad frames are never fatal.** Malformed JSON, an unknown `type` or a bad field is logged and ignored.
- **Recording cap:** 30 s per button press.
- **Shutdown:** on Ctrl+C the server closes connections with `1012` and waits at most 5 s.
- **Reconnects:** the robot should reconnect automatically after any close except `4001`, `4002` and `4003`, with a back-off (for example 1 s, doubling up to 30 s).

## Going on the LAN (M6)

M4 listens on localhost only. When the real robot joins in M6:

1. In `server/.env`, set `SERVER_HOST=0.0.0.0`.
2. In an elevated PowerShell, allow the port on Private networks only:
   ```powershell
   New-NetFirewallRule -DisplayName "Desk Buddy robot link" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
   ```
3. Make sure the Wi-Fi network is set to **Private** (Settings → Network & internet → Wi-Fi → your network → Network profile type).
4. Point the robot at `ws://<the PC's LAN IP>:8765/robot`.

The link is plain `ws://`, not TLS: the token protects against other devices on the home network, but not against someone who can watch its traffic. Don't expose the port to the internet.

**Deferred to M6:** limits on failed-auth attempts, a cap on open connections, and limits on button floods and queue sizes.
