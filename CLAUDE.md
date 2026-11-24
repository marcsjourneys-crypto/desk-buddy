# Desk Buddy — project brief for Claude Code

A retro tin-toy desk robot that listens, talks, has a personality, takes dictation into my Obsidian vault, and (later) controls my computer. Built from scratch: my own server, my own firmware. Inspired by cgro00/desk-robot (MIT), but no code is copied from it.

## How we work

- Work one milestone at a time (see Roadmap). Start each in plan mode; show me the plan before writing code.
- Small commits with clear messages. Run tests before every commit.
- Ask before adding a new dependency. Prefer the standard library when it's reasonable.
- Secrets live in `server/.env` (gitignored). Commit a `.env.example` with every key listed, values blank.
- Keep this file current: when a decision changes, update it in the same commit.
- The server runs on a Windows 11 PC and targets Windows only. Current machine: Intel i7-1355U laptop, 16 GB RAM, no discrete GPU — so everything must run well on CPU; CUDA is an optional speed-up, never a requirement.
- (Changed 2026-11-23: the server was originally planned for a Mac Mini. All Mac-specific choices — mlx-whisper, `say`, AppleScript — were replaced with Windows equivalents.)

## Architecture

Thin robot, smart server. The robot streams audio and button presses; the server does all the thinking.

```
ESP32-S3 robot  <── WebSocket over Wi-Fi (token auth) ──>   PC server (Python) 
  mic, speaker, 2 round eyes,                               VAD → STT → Claude → TTS
  2 servos, antenna LED, touch                              tools: notes, music, apps
                                                            dashboard (localhost)
```

The server must also run with the PC's own mic and speaker (no robot connected), so the whole voice loop can be built and tested before the hardware works.

## Repo layout

```
server/                 Python brain (uv-managed, src layout)
  src/desk_buddy/       the single importable package
  tests/                pytest
firmware/               ESP32-S3 firmware (PlatformIO, Arduino framework)
docs/                   protocol.md, wiring.md, decisions.md, plans/
```

- Decisions log: `docs/decisions.md` — add an entry whenever a decision is made or changed.
- Server commands (run inside `server/`): `uv sync`, `uv run pytest`, `uv run ruff check`, `uv run ruff format`.
- Integration tests (real audio/COM/models/network) are marked and skipped by default: `uv run pytest -m integration`.

## Server stack (decided)

- Python 3.12+, managed with uv. Tests with pytest.
- Async core: asyncio + websockets, or FastAPI if the dashboard needs it — decide in M4. FastAPI is likely, since the planned web UI covers settings, personality and expressions.
- Python comes from uv's own builds only (`python-preference = "only-managed"`). The Microsoft Store Python can't open microphones.
- VAD: Silero VAD — the ONNX model bundled with faster-whisper (`vad_filter`), no PyTorch.
- Audio I/O: sounddevice (PortAudio) for the PC mic/speaker.
- STT: faster-whisper (CTranslate2), int8 on CPU. Start with `base.en`; model size, device (cpu/cuda) and compute type configurable in `.env`.
- LLM: Anthropic API via the official `anthropic` SDK. Default model `claude-haiku-5-5` for low latency; configurable in `.env`. Stream responses so speech starts before the full reply is done. Provider is pluggable via `LLM_PROVIDER` (only `anthropic` today); OpenAI-compatible providers such as Kimi are planned after M3.
- TTS: start with the built-in Windows SAPI voices (via comtypes), then Piper (local, runs on Windows) as the default voice. Cloud voice (e.g. ElevenLabs) optional later behind the same interface.
- Obsidian: write markdown files directly into the vault folder (path in `.env`). No plugin needed.

## Personality

- Original character (name TBD; currently **Buddy**). Cheerful and calm, with the odd dry, witty line. Do not use existing fictional characters or voice clones.
- One system prompt in `server/src/desk_buddy/brain/personality.py`, plus canned lines (error, refusal and "fresh start" now; wake and sleep in M6).
- Remembers the conversation until restart: the last `HISTORY_TURNS` exchanges, and only what was actually heard. An interruption is noted on the user's next message, never in Buddy's stored reply. Saying "new topic", "forget that" or "start over" clears it (fixed phrases in `brain/memory.py`).
- Every reply starts with an emotion tag: `[neutral] [happy] [sad] [surprised] [sleepy] [thinking] [curious]`. The server strips it from speech and sends it to the robot's eyes.
- Replies are short by default (configurable max sentences). Spoken style: no markdown, no lists.

## Tools the LLM can call

Every tool is a plain Python function with a strict JSON schema, registered in `tools/registry.py` (built by `tools.build_tools`). `ClaudeChat` runs its own streaming tool loop in `llm.py`: up to 3 API rounds per reply, and tools are not allowed on the last round.

- `take_note(text, kind, title)`:
  - `note` appends a timestamped line to `Daily/YYYY-MM-DD.md`.
  - `idea` creates `Ideas/YYYY-MM-DD <title>.md` with frontmatter (date, tags).
  - Claude lightly cleans up the dictated text.
- `read_notes(date)` — reads back a day's daily note and ideas.
- Buddy has a vault of its own (`OBSIDIAN_VAULT_PATH`; blank means the tools are off). It only appends and creates, never edits or deletes, and every path is checked to stay inside the vault. Folder names are configurable.
- Treat text that `read_notes` returns as untrusted. It must be labelled as such before computer-control tools exist (M7).
- Later: `play_music`, `media`, `open_app`, `open_website`, `computer_volume`, `show_weather`.

## Safety rules for computer-control tools (non-negotiable)

Anyone (or any TV) near the robot can talk to it.

- Off by default; a setting turns it on.
- Allowlists only. The model chooses a name from a list; the server builds every command and URL. Never pass model text into a shell or command string. Always use argument lists, never `shell=True`.
- Rate limits: max 3 actions per question, 8 per minute.
- Anything powerful (e.g. running an allowlisted script, sleep/shutdown) requires a spoken "yes" on the next turn, checked by code.
- Every action is logged and shown on the dashboard.

## Robot ↔ server protocol (draft — finalize in docs/protocol.md during M4)

- One WebSocket. Text frames = JSON control messages. Binary frames = audio.
- Audio up: 16 kHz, 16-bit mono PCM, ~20 ms chunks. Audio down: 16 kHz 16-bit mono PCM (revisit if quality is poor).
- Robot → server: `hello {token, fw_version}`, `button {down|up}`, `touch {}`, `stats {...}`.
- Server → robot: `state {idle|listening|thinking|speaking|sleeping}`, `emotion {name}`, `look {pan, tilt, speed}`, `led {mode}`, `audio_start`, `audio_end`, `volume {level}`.
- The server listens on the LAN; Windows Firewall needs an inbound rule for the WebSocket port on Private networks only (document the exact command in docs/protocol.md).
- Server stays the source of truth for state. Robot is dumb and reconnects automatically.

## Hardware (ordered)

- Hosyond ESP32-S3 DevKit N16R8 (16 MB flash, 8 MB octal PSRAM, two USB-C ports)
- 2× GC9A01 1.28" 240×240 round TFT (SPI, shared bus, separate CS) — the eyes
- INMP441 I2S MEMS mic (L/R pin to GND)
- NULLLAB MAX98357A I2S amp + small 3 W speaker
- 2× MG90S servos (pan, tilt), 608ZZ bearing for the neck
- WS2812B LED in a brass-tube antenna, copper-foil touch pad on the head, tactile button backup
- 5 V 3 A USB-C supply, 1000 µF cap across servo power

## Firmware constraints

- Do NOT use GPIO 35, 36, 37 (octal PSRAM on N16R8).
- Avoid strapping pins 0, 3, 45, 46 and USB pins 19, 20 for peripherals. GPIO 48 is the onboard RGB LED.
- Servos powered from 5 V, not 3.3 V; common ground; stop sending PWM when a servo reaches its target (kills jitter and noise).
- Freeze servos while listening so the mic doesn't hear them.
- Servo motion always eased (no snapping); pan limit about ±60°.
- Pin map lives in `firmware/include/pins.h` and `docs/wiring.md` — keep them in sync.

## Roadmap

- **M0 – Repo setup.** Monorepo layout, uv project, pytest, `.env.example`, `docs/decisions.md`.
- **M1 – Voice loop on the PC.** PC mic → VAD → STT → Claude (streaming) → TTS → PC speaker. Push-to-talk via keyboard first.
- **M2 – Personality.** System prompt, emotion tags parsed and logged, short spoken replies, conversation memory for the session.
- **M3 – Obsidian dictation.** `take_note` / `read_notes` tools, tests against a temp vault.
- **M4 – Robot link.** WebSocket server, token auth, `docs/protocol.md`, and a fake-robot Python client for tests.
- **M5 – Firmware bring-up** (can run in parallel with M1–M4): blink + PSRAM check, one eye, two eyes, mic levels, speaker tone, record/playback loopback, servo sweep with easing.
- **M6 – Robot joins the link.** Stream mic, play TTS, eyes react to state/emotion, servos follow look, antenna LED follows state.
- **M7 – PC computer control.** Spotify search via Web API (free dev key) and playback via `spotify:` URIs / Web API (remote playback control needs Premium), media keys via Win32 `SendInput` (ctypes), allowlisted apps (launched by full exe path, argument lists) and sites, master volume. All safety rules above.
- **M8 – Dashboard.** Local web page: live state, transcript, last actions, and settings, including:
  - the computer-control switch;
  - the vault path and other non-secret settings, which move from `.env` to a settings file the UI edits (secrets stay in `.env`);
  - personality editing (the prompt and canned lines move to an editable file).
- **M9 – Expression library** (after M6). Design and preview eye expressions in the browser and map them to emotions.
- **Later:** wake word, camera board (XIAO ESP32S3 Sense) for face tracking, "hands" agent for other computers (e.g. a Mac), Raspberry Pi 7" console base.
