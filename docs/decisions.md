# Decisions

A running log of project decisions. Newest last. When a decision changes, add a new entry (don't rewrite history) and update `CLAUDE.md` in the same commit.

Format: **Decision** — why · alternatives considered.

## 2026-11-23

- **Server targets Windows, not macOS.** The server runs on a Windows 11 PC (current dev machine: i7-1355U laptop, 16 GB RAM, no discrete GPU), so everything must run well on CPU; CUDA is optional. · Alternatives: Mac Mini M4 Pro (original brief).
- **STT: faster-whisper, int8 on CPU, starting with `base.en`.** Fast on CPU via CTranslate2; model, device and compute type configurable. · Alternatives: mlx-whisper (Apple Silicon only), openai-whisper (much slower on CPU).
- **TTS: Windows SAPI voices first, then Piper as the default.** SAPI needs zero setup; Piper is local and runs on Windows. · Alternatives: macOS `say` (Mac only).
- **Computer control (M7) uses Windows mechanisms.** Spotify Web API, Win32 `SendInput` media keys, apps launched by full exe path. · Alternatives: AppleScript / Apple Shortcuts (Mac only).
- **src layout with a single `desk_buddy` package** (`server/src/desk_buddy/`). One import name avoids clashes with generic names like `audio` or `tools`, and tests can't pass against uninstalled files. · Alternatives: flat top-level packages (`server/brain/`), single package without `src/`.
- **uv installed via winget.** Standard Windows package manager; independent of the system Python. · Alternatives: astral.sh PowerShell installer, `pip install uv`.
- **ruff for lint and format; no type checker yet.** One fast dev-only tool. · Alternatives: ruff + mypy now (audio libraries have patchy type stubs, so it would be noisy early).
- **Code review: `python-reviewer` agent adapted from everything-claude-code (MIT), plus `/code-review`.** Gets a focused Python checklist plus Desk Buddy safety checks without the whole ECC plugin's many auto-triggering agents and hooks. Attribution in `THIRD_PARTY_NOTICES.md`. · Alternatives: install the full ECC plugin.
- **Text files stored with LF** (`.gitattributes`). · Alternatives: let Git use CRLF per platform.
- **Use uv-managed Python only** (`python-preference = "only-managed"` in `server/pyproject.toml`). The venv had been built on the Microsoft Store Python, whose app package has no microphone capability: every mic failed on MME, DirectSound and WASAPI, while uv's standalone CPython opens them all. · Alternatives: WDM-KS capture (exclusive, native rates only, skips Windows mic processing); a python.org install.

## 2026-11-23 — M1 voice loop

- **Audio I/O: sounddevice confirmed.** The mic input stream stays open and only buffers while the key is held. One long-lived output stream plays sentences without gaps. · Alternatives: PyAudio.
- **SAPI via comtypes on a dedicated COM thread.** It renders to an in-memory 16 kHz stream with `SVSFIsNotXML`. · Alternatives: PowerShell subprocess, pywin32, pyttsx3.
- **VAD: faster-whisper's bundled Silero ONNX (`vad_filter=True`).** No PyTorch. · Alternatives: silero-vad package.
- **Push-to-talk: a global hold key via `GetAsyncKeyState` (ctypes).** `PTT_KEY` defaults to Right Ctrl. · Alternatives: Enter toggle, tap + VAD end.
- **Interrupts: pressing the key mid-turn cancels the turn task and stops playback.**
- **LLM effort `low` by default (`LLM_EFFORT`)** for latency on Haiku 5.5. Measured on the i7-1355U laptop, counted from key release, for a 5 s question: STT 666 ms, first token 1245 ms, first audio 1815 ms. · Alternatives: default `medium`.
- **Whisper `beam_size=1`** (greedy) for CPU latency.
- **LLM provider seam: `LLM_PROVIDER` (only `anthropic` for now) + `llm.create_chat()`.** The loop depends only on `stream_reply()`. OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3, once tool calling is settled. · Alternatives: Kimi support in M1; no seam.
- **New `.env` keys:** `LLM_PROVIDER`, `LLM_EFFORT` (default `low`), `PTT_KEY` (default `right_ctrl`).
- **`anthropic` SDK 2.x uses `httpx2`**, not `httpx`.
- **Async tests use `asyncio.run`**, so no pytest-asyncio.

## 2026-11-23 — M2 personality

- **Character: cheerful and calm, with the odd dry, witty line.** It's one prompt in `brain/personality.py`. The name is still TBD (currently Buddy).
- **Emotion tags are read by a streaming parser** (`brain/emotion.py`). A missing or unknown tag means neutral, and known tags later in the reply are removed so they aren't spoken. · Alternatives: structured output, a separate classification call.
- **Memory lasts until restart, capped at `HISTORY_TURNS` exchanges (default 20).** · Alternatives: forget after a period of idle time.
- **Only what was heard is remembered.**
  - A sentence counts once it starts playing.
  - Failed turns, and turns interrupted before any speech, are dropped.
- **Interruptions are noted on the user's next message, not in Buddy's stored reply.** In the first live test, Claude copied an "(interrupted)" marker from its own stored reply into a new reply, and the voice spoke it. · Alternatives: a marker at the end of the stored reply (the original design).
- **Forget command: fixed phrases checked in code** ("new topic", "forget that", "start over"…, in `brain/memory.py`). · Alternatives: a Claude tool.
- **Short replies are enforced by the prompt only** (`MAX_SENTENCES`, about thirty words), so "tell me three facts" still works.

## 2026-11-23 — M3 design and roadmap

- **Buddy gets an Obsidian vault of its own.** Daily notes go in `Daily/YYYY-MM-DD.md` and ideas in `Ideas/YYYY-MM-DD <title>.md`. Buddy only appends and creates; it never edits or deletes. · Alternatives: write into the user's main vault, match an existing daily-notes plugin.
- **Dictated notes are lightly cleaned by Claude**: mishearings, punctuation and filler are fixed, and Buddy confirms briefly. · Alternatives: verbatim, summarised.
- **Tools run in our own streaming loop inside `ClaudeChat`**, up to 3 rounds, with `tool_choice: none` on the last round and strict schemas. · Alternatives: the SDK's beta tool runner; catching note phrases in code without Claude.
- **Planned browser UI.** M8 grows to cover settings (the vault path and other non-secret settings move to a UI-editable file; secrets stay in `.env`) and personality editing. A new M9 adds an eye-expression library after M6. This makes FastAPI the likely M4 choice.

## 2026-11-23 — M3 Obsidian dictation

- **The vault boundary is checked lexically** (`os.path.abspath` plus `is_relative_to`), not with `Path.resolve()`. On Windows, `resolve()` sometimes returned `\\?\` paths depending on timing, so the check failed at random. Buddy only writes paths it builds itself, and file names are sanitised by `safe_title`. · Alternatives: `resolve()` (follows symlinks, but flaky here).
- **Folder settings are validated with `PureWindowsPath`.** A drive, a root or `..` is rejected, including drive-relative paths like `C:x`. A plain string check let `C:x` through.
- **Logs show only an 80-character preview of each tool result.** That's enough to see that a note was saved without copying everything you dictate into the console log.
- **Running out of tokens before any text plays the error line.** A tool round can use up `MAX_TOKENS` before Buddy says anything, and silence would be confusing.
- **To do before M7:** text that `read_notes` returns comes from files, not from the user. Before computer-control tools exist, it must be marked as untrusted so a planted note can't steer Buddy (prompt injection).
- **Known minor issue:** in the voice check, Buddy repeated the prompt's example confirmation ("Noted: pick up dog food.") word for word. Harmless; vary the example if it becomes repetitive.
- **Voice check passed (user):** a daily note, an idea file whose misheard title was cleaned up, and a read-back summary. First audio took 2.5–3.8 s with a tool round, against about 1.8 s without one.

## 2026-11-24 — M4 robot link

- **FastAPI and uvicorn, in the same asyncio loop as the voice loop.** One process; Ctrl+C stops both. uvicorn's own signal handling is turned off, and the socket is bound first so a busy port is a clear startup error. FastAPI will also host the M8 dashboard and M9 expression library. · Alternatives: the plain `websockets` library; a separate server process.
- **The robot takes over the devices.** While a robot is connected, its mic, speaker and button are used and the PC key is ignored. `DeviceSwitch` sits behind the orchestrator's existing `Mic` and `Player` interfaces, so the core loop only gained a `reset` event. · Alternatives: PC and robot both active.
- **The newest authenticated connection replaces the old one (close code `4003`).** A dead Wi-Fi socket can linger for up to 40 s before pings catch it; refusing the reconnect would leave the robot deaf for that long. · Alternatives: refuse while one is connected.
- **Localhost by default** (`SERVER_HOST=127.0.0.1`, `SERVER_PORT=8765`). LAN binding and the Private-only firewall rule are documented in `docs/protocol.md` and switched on in M6.
- **`ROBOT_TOKEN` of at least 16 characters; blank turns the link off.** Compared with `hmac.compare_digest` on bytes and never logged (the hello message's repr masks it). A token that isn't valid UTF-8 closes with `4002`, not a server error.
- **Speech to the robot is paced at real time with about 200 ms of lead.** It keeps "only what was heard is remembered" accurate, keeps the robot's buffer small and lets `audio_stop` take effect quickly. Known: up to ~200 ms of already-queued audio can arrive after `audio_stop`, so the robot drops audio until the next `audio_start`.
- **Robot mic recording follows the button, not the orchestrator.** The link starts and ends the clip on `button down`/`up`, so a fast re-press can't lose the start of the next clip.
- **Tests run a real uvicorn server on a loopback port** with the fake robot client, instead of FastAPI's `TestClient`. That exercises the real socket, ping and close-code paths, and avoids adding `httpx`. · Alternatives: `TestClient` (needs `httpx`).
- **The fake robot is a test helper and a voice tool** (`python -m desk_buddy.fake_robot`), so the link can be voice-checked before the hardware works.
- **`wake {}` is reserved** for an on-robot wake word ("Later" on the roadmap). The server accepts and ignores it.
- **Deferred to M6** (when the server listens on the LAN): throttling failed auth attempts, a cap on open connections, limits on button floods, and bounded send queues.
- **Voice check passed (user):** a question, an interrupt mid-reply and a disconnect through the fake robot. The follow-up after the interrupt didn't mention it (the M2 fix, now checked). Not checked by hand: Right Ctrl after the robot disconnects (covered by tests).

## 2026-11-24 — M5 firmware bring-up

- **Arduino core 3.x via pioarduino, pinned to `55.03.312-1` (core 3.3.12, ESP-IDF 5.5.5),** on the ready-made `esp32-s3-devkitc1-n16r8` board definition (16 MB flash, octal PSRAM). Core 4.0 was days old; the official PlatformIO platform is frozen at core 2.x with the legacy I2S driver. · Alternatives: core 2.x on the official platform.
- **Windows setup it needed:** PlatformIO Core 6.2 via `pioarduino==6.2.0` (PlatformIO's semver check reads `.post1` as a pre-release), installed while PlatformIO isn't running; long paths turned on; `pio` run from PowerShell only. Written up in `firmware/README.md`.
- **LovyanGFX 1.2.36 for the eyes.** It has anti-aliased circles and `drawWedgeLine`, but no smooth arc, so curves are drawn as thick wedge segments. · Alternatives: TFT_eSPI (core-3.x/S3 issues), Arduino_GFX (no anti-aliasing).
- **The eye model is two lid bulge values plus size, thickness and gaze.** A ring, `∩`, `‿` and a closed line all come from it, and every blend is a plain lerp. M9's editor can store the same numbers. · Alternatives: bitmaps per expression.
- **One 200×200 sprite in internal RAM, drawn once and pushed to both eyes when they match, redrawn only while something moves.** Measured: 10.9 fps with two 240×240 PSRAM sprites, 25.7 fps now; 16 segments per curve (24 gives 22 fps; the user found 16 smooth). The wedge-line drawing is the cost, not PSRAM. Free heap with everything running: 229 KB.
- **One app with a serial test menu,** and the pure logic in header-only files tested with Unity on the board (no C++ compiler on the PC). 28 tests.
- **The PSRAM strip-drawing fallback was dropped:** PSRAM is confirmed (8 MB octal).
- **Audio:** speaker on I2S port 0 with mono data on both slots; mic on port 1 (INMP441, 24-bit in 32-bit slots, left). The mic gets +12 dB (`kMicShift = 14`); speech peaked near −4 dBFS close up, so this may come down. The idle level read −28 to −38 dBFS, probably room noise plus the INMP441's DC offset; M6 should remove DC before streaming. Stale DMA audio is flushed before each recording. Volume starts at 20.
- **The board's 5V pin gives no USB power** (the IN-OUT jumper is open), so the amp runs on 3.3 V for now; the 5 V 3 A supply will feed the amp and servos in the final wiring. Don't bridge IN-OUT while an external 5 V supply can be connected.
- **Servos: attached once, eased at 90 °/s, and stopped with duty 0 after a 200 ms settle** instead of detaching the pin, which could cut a pulse short and twitch the servo. Pan ±60°, tilt ±20° (provisional). The first move after power-on jumps to centre.
- **Hardware checks passed (user):** LED and PSRAM, both eyes (expressions, blink, smoothness), the tone and volume, mic levels and the record/playback loopback. **Pending:** the servo sweep, once the servos are wired to the 5 V supply.
