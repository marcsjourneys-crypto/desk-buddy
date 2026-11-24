# M4 robot link: design

Approved by the user on 2026-11-23.

## Goal

The robot (later an ESP32-S3) connects to the server over one WebSocket. It sends button presses and mic audio, and receives state, emotion and Buddy's voice. In M4 a fake robot stands in for the hardware, for both the tests and a voice check.

## Decisions

- **The robot takes over the devices.** While a robot is connected, its mic, speaker and button are used, and Right Ctrl is ignored. When no robot is connected, the PC mic and speaker are used, as in M1–M3.
- **FastAPI and uvicorn**, so the same server can host the M8 dashboard and the M9 expression library later.
  - New runtime dependencies: `fastapi`, `uvicorn[standard]` and `websockets` (the latter already comes with uvicorn[standard]).
  - New dev dependency: `httpx`, which FastAPI's `TestClient` needs.
- **The fake robot is a test helper and a voice tool.** `python -m desk_buddy.fake_robot` uses the PC mic, speaker and Right Ctrl over the WebSocket.
- **Localhost by default.** `SERVER_HOST=127.0.0.1` and `SERVER_PORT=8765`. LAN binding and the firewall rule are documented now and turned on in M6.
- **The newest authenticated connection replaces the old one.** After a Wi-Fi drop, a stale socket can linger for up to 40 s, so refusing the reconnect would be worse.
- **A wake word comes later** (most likely run on the robot). The protocol reserves `wake {}` for it now.

## Architecture

```
FastAPI app (uvicorn, SERVER_HOST:SERVER_PORT)
  /robot  WebSocket ──> RobotLink (one robot at a time)
                          ├─ RobotMic     buffers audio frames between button down/up
                          ├─ RobotPlayer  sends TTS audio paced in real time
                          └─ button/touch → events queue
DeviceSwitch (Mic + Player) ── robot if connected, else PC mic/speaker
Orchestrator (unchanged core) ── on_state/on_emotion → RobotLink.send(...)
```

- **One process, one asyncio loop.** `python -m desk_buddy` runs uvicorn and the orchestrator together. Ctrl+C shuts down both.
- **The switch is behind the existing interfaces.** `DeviceSwitch` implements the `Mic` and `Player` protocols in `loop.py`, so the orchestrator stays almost unchanged.
- **Disconnects.** When the robot disconnects, any turn in progress is interrupted and the PC devices take over again. Conversation memory survives reconnects.
- **Interrupts.** `Player.stop()` on the robot sends `audio_stop`, so the robot drops its playback buffer.
- **Playback pacing.** `RobotPlayer.play()` sends audio at real time with about 200 ms of lead, and blocks for the length of the sentence. That keeps "only what was heard is remembered" accurate and stops the robot's buffer from overflowing.

## Protocol (full text goes in `docs/protocol.md`)

- **Connecting.** The robot opens `ws://<host>:<port>/robot`.
  - Its first frame must be `{"type":"hello","token":…,"fw_version":…}`, within 5 s.
  - The token is compared with `hmac.compare_digest` and never logged.
  - The server replies `welcome` and then the current `state`.
- **Close codes:**
  - `4001`: bad token.
  - `4002`: no hello within 5 s.
  - `4003`: replaced by a newer connection.
- **The token.** `ROBOT_TOKEN` in `.env`, at least 16 characters. Blank means the robot link is off and the log says `Robot link: off`.
- **Text frames are JSON**, in the form `{"type": …}`.
  - Robot → server: `hello`, `button {action: down|up}`, `touch {}` (logged only), `stats {…}` (logged at debug level).
  - Server → robot: `welcome`, `state {name}`, `emotion {name}`, `audio_start {rate}`, `audio_end`, `audio_stop`.
  - Reserved: `look`, `led` and `volume` (server → robot, from M6); `wake {}` (robot → server, later).
- **Audio** is 16 kHz, 16-bit mono PCM in binary frames of about 20 ms.
  - From the robot, frames are accepted only between `button down` and `up`; others are dropped. The 30 s cap applies.
  - To the robot, each sentence is sent as `audio_start`, then binary chunks, then `audio_end`.
- **Robustness:**
  - Frames are capped at 64 KB.
  - Malformed JSON or an unknown `type` is logged and ignored.
  - uvicorn sends WebSocket pings every 20 s to detect a dead robot.
- **Firewall.** The exact `New-NetFirewallRule` command, for Private networks only, is documented for M6.

## Components

- `link/protocol.py`: parses and validates messages, as pure functions.
- `link/robot.py`: `RobotLink` (the hello and auth, the receive loop, sending), `RobotMic` and `RobotPlayer`.
- `link/switch.py`: `DeviceSwitch`.
- `server.py`: the FastAPI app with the `/robot` route.
- `fake_robot.py`: a client class for the tests, plus a command-line voice tool.
- Changes to existing files:
  - `__main__.py` runs uvicorn alongside the orchestrator.
  - `config.py` and `.env.example` get `ROBOT_TOKEN`, `SERVER_HOST` and `SERVER_PORT`.

## Errors

- **The robot disconnects mid-turn:** the turn is interrupted, the PC takes over again, and the log says `Robot disconnected`.
- **A send fails during playback:** this is treated as a disconnect.
- **The port is already in use:** Buddy stops at startup with a clear message.
- **A bad frame arrives:** it is logged and ignored. Bad frames are never fatal.

## Tests

- **Unit tests:**
  - protocol parsing;
  - `DeviceSwitch` routing;
  - `RobotPlayer` pacing and stopping, with a fake clock.
- **In-process tests with `TestClient`:**
  - the hello and auth paths and their close codes;
  - a new connection replacing an old one;
  - a full turn driven by the fake robot with canned audio and a fake STT, LLM and TTS, checking that `state`, `emotion` and the audio messages arrive in order;
  - an interrupt that sends `audio_stop`.
- **Integration test:** a real uvicorn instance on a free port, with the fake robot connecting over a real socket.
- **Voice check by the user:** Buddy runs in one terminal and `fake_robot` in another. The user talks through the "robot".
