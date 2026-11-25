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

## 2026-11-24 — M6 robot joins the link

- **The talk button is the PC key (Right Ctrl) for now,** held in the Buddy window. The robot has no button or touch pad wired yet. While a robot is connected, the key opens and closes the robot's mic recording instead of being ignored (this reverses the M4 "PC key is ignored" rule). A robot button, touch pad or wake word can send the existing `button` message later. · Alternatives: wire a button first.
- **The robot's mic stays open 250 ms after the key is released** (`RELEASE_DELAY`), so frames still in flight over Wi-Fi arrive before the turn starts. A re-press inside those 250 ms ends the first clip at once; it is then too short and is dropped as a tap, by design.
- **The robot streams its mic exactly while the server's state is `listening`.** No new protocol message; the server stays the source of truth. The firmware flushes stale DMA audio, then removes the mic's DC offset with a one-pole high-pass filter (r = 0.995, seeded with the first sample so the offset doesn't thump).
- **Robot settings are typed once over serial and kept in NVS** (`wifi`, `server`, `token`, `config` masked, `forget`, plus `net` for status). They survive reflashing and nothing secret goes into the build. · Alternatives: a gitignored `secrets.h` (a rebuild per change); a captive portal (more code and a library).
- **Two firmware libraries, pinned:** `links2004/WebSockets@2.7.3` (core 3.3 has no WebSocket client) and `bblanchon/ArduinoJson@7.4.3` (the JSON control messages, with correct escaping in `hello`). The server gained no dependencies.
- **The library doesn't expose close codes.** In WebSockets 2.7.3 the code of a received close is parsed only for a debug log, and the app gets the same `WStype_DISCONNECTED` for every close. So the firmware treats a close less than 1 s after `hello` as a refusal; three in a row stop the retries (`Server refused the robot … check: token <value>`), with a retry every 5 minutes and at once after a settings change. A missing `welcome` (5 s) is an ordinary failure. The library's own auto-reconnect is disabled so the firmware's back-off (1 s doubling to 30 s) drives every attempt. · Alternatives: patch the library.
- **Robot audio threading:** the mic task and the playback task run on core 1; the WebSocket and the eyes run in the main loop. Speech is buffered in a 1 s PSRAM ring and each sentence starts after ~60 ms is buffered; `audio_stop` clears it and fades out the block in hand. Mic frames reach the main loop through an 8-deep queue. The serial audio tests (and `servo`, `fps`) are refused while the link is connected.
- **Speech to the robot goes in 100 ms frames (3200 bytes), not 20 ms.** The ESP32's lwIP queues at most 6 received segments per socket (`CONFIG_LWIP_TCP_RECVMBOX_SIZE`; window 5760 bytes; 250 ms TCP timer), so one small segment per 640-byte frame capped the robot at ~31 frames/s, below real time, and after ~7 s of speech the server's send queue overflowed and closed the link (`1011`). Full-size segments hit the window first instead. Found in the first voice check. Mic frames up stay 20 ms. · Alternatives: raise the lwIP mailbox size (needs a custom ESP-IDF build).
- **The eyes follow state and emotion** (`firmware/include/looks.h`): idle → idle, listening → wide and attentive, thinking → the animated swirl, speaking → the reply's emotion, not connected → sleep. Emotions: neutral → idle, happy → happy, sad → the new tilted `sad` look, surprised → the burst, sleepy → sleep, thinking → skeptical, curious → look up. The eye model gained `tilt`; the lead's eye work added crescent looks, `lookaround`, and animated specials (thinking swirl, surprised burst, `excited` sparkle; the old drifting look-up is `ponder`). Reference images: `docs/looking.png`, `look around.png`, `thinking.png`, `surprised.png`, `excited.png`. The lids are capped at ~30 fps so the main loop has time for the link.
- **Hardening for the LAN** (in memory, constants in `server.py` and `link/robot.py`): 5 bad tokens from one IP within 60 s block it for 60 s (timeouts and bad hellos count; blocked IPs get `4001` at once); at most 4 connections waiting for a hello, 2 per IP (`4002`); 10 button downs a second with a burst of 10 (ups are never limited); a send queue of 128 items, which closes with `1011` on overflow; closing a link waits at most 2 s.
- **LAN setup:** `SERVER_HOST=0.0.0.0`, a Private-only firewall rule for TCP 8765 (PowerShell and cmd versions in `docs/protocol.md`), and the startup log prints the PC's LAN IP with the exact `server` command. A DHCP reservation for the PC is recommended. Without the firewall rule, connects to a stopped server time out after ~5 s instead of failing fast (Windows drops rather than refuses), which blocks the robot's main loop for that long.
- **Not in M6:** servos following `look`, the antenna LED and the touch pad, because they aren't wired yet. Their messages stay reserved.
- **Voice check passed (user):** the robot linked over Wi-Fi; a question through the robot's mic was transcribed correctly; the reply played on the robot's speaker; an interrupt worked; reconnecting worked. Stopping the server gracefully or killing it both make the robot print `Disconnected` within ~0.5 s, and the eyes sleep. Measured from key release: STT 1048 ms for 2.9 s of audio, first token 2577 ms, first audio 2969 ms.

## 2026-11-25 — CARL-42 personality

- **The character is renamed Buddy → CARL-42** ("CARL" for short; the Cognitive Assistant Robot for Life). Only the character: the repo, the `desk_buddy` package, the commands and the GitHub repo keep their names. The prompt, canned lines, logs (`CARL [happy]: …`) and docs change. The forget command also accepts "Carl" or "Karl" (Whisper's frequent spelling) as a filler ("Hey Carl, new topic"), alongside "Buddy". · Plan: `docs/plans/2026-11-25-carl-personality.md`.
- **A light *Hitchhiker's Guide* flavour, as an original character.** Calm, helpful and brief, with a dry, cosmic aside roughly one reply in four, never when the user is frustrated or rushed and never more than one per reply. Short homage lines are fine in a personal robot ("Don't panic", 42, the fish); CARL never imitates a character from the books (no Marvin) and never quotes them at length.
- **Signature lines, each with a rule so they stay a treat.** None is remembered, and a key press stops any of them like other speech.
  - `GREETING` "Don't panic. CARL is online.": once at startup (a `greet` event queued after `Ready`), and when a robot connects (the server's `on_connect` hook queues `greet` right after the connect's `reset`), skipped if a greeting finished playing in the last 60 s (`GREET_GAP`; an interrupted or failed one doesn't count) or CARL isn't idle.
  - `FAREWELL` "So long, and thanks for all the fish.": on Ctrl+C, as the last exit-stack callback pushed, so it runs first, before the key watcher, the server and the devices close. It interrupts whatever was happening, is cut off after 5 s (`FAREWELL_TIMEOUT`, with a hard cap of 6 s, `FAREWELL_HARD_CAP`), and any failure is logged and skipped. It replaces the logged "Bye".
  - `PONDERING` "Contemplating life, the universe, and everything.": if no reply sentence is ready 4 s after CARL starts thinking, i.e. after transcription ends (`PONDER_AFTER`; counting from key release would let a long question's STT trigger it), at most once every 5 minutes (`PONDER_EVERY`); the reply plays after it. While the line plays the reply's emotion is held back, then sent when it ends. The "First audio" latency log is for the reply only.
  - A failing `PONDERING` or `TASK_DONE` line is skipped with a warning (`_say_extra`): an extra line must never cost the real reply.
  - `TASK_DONE` "Another mystery of the universe solved.": after a reply whose turn ran at least one successful tool call, with probability 1/4 (`TASK_DONE_CHANCE`), not after a failed reply. `ClaudeChat` keeps a running `tools_succeeded` count; the loop compares it before and after the reply (a chat without the count never triggers it).
  - 42 is prompt guidance only: say plainly when you don't know, and at most once per conversation add that the answer might be 42.
- **`ERROR_LINE`** is now "Sorry, my brain seems to be on the other side of the galaxy right now." Refusal and forget lines are unchanged.
- **The voice is chosen separately** (a Piper audition is next); a British voice may suit CARL.
