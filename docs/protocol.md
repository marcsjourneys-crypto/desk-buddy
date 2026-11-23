# Robot ↔ server protocol

> **Status: draft.** Finalized during M4 (robot link). Until then, this mirrors the draft in `CLAUDE.md`.

## Draft

- One WebSocket. Text frames = JSON control messages. Binary frames = audio.
- Audio up: 16 kHz, 16-bit mono PCM, ~20 ms chunks. Audio down: 16 kHz 16-bit mono PCM (revisit if quality is poor).
- Robot → server: `hello {token, fw_version}`, `button {down|up}`, `touch {}`, `stats {...}`.
- Server → robot: `state {idle|listening|thinking|speaking|sleeping}`, `emotion {name}`, `look {pan, tilt, speed}`, `led {mode}`, `audio_start`, `audio_end`, `volume {level}`.
- The server listens on the LAN; Windows Firewall needs an inbound rule for the WebSocket port on Private networks only (exact command: see "To decide in M4").
- Server stays the source of truth for state. Robot is dumb and reconnects automatically.

## To decide in M4

- Exact JSON shape of each message (field names, types, required/optional).
- Auth failure behaviour and reconnect backoff.
- The Windows Firewall rule command (inbound, WebSocket port, Private profile only).
- Bind address: `SERVER_HOST` defaults to `0.0.0.0` so the robot can reach the server over Wi-Fi; decide whether to bind only the LAN interface instead.
