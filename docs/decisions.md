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
