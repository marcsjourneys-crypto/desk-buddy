# M1 – Voice loop on the PC: design

Date: 2026-11-23 · Status: approved

## Goal

Talk to Buddy with the PC's own mic and speaker: hold a key, speak, release, and hear a streamed spoken reply. PC mic → VAD → STT → Claude (streaming) → TTS → PC speaker. No robot, no personality yet.

## Decisions made while brainstorming

| Topic | Decision | Why |
|---|---|---|
| Push-to-talk | Hold a global key (default Right Ctrl, `PTT_KEY`), polled with Win32 `GetAsyncKeyState` via ctypes | Works without terminal focus; no dependency; mirrors the robot's `button {down\|up}` |
| SAPI library | `comtypes`, rendering into an in-memory `SpMemoryStream` at 16 kHz 16-bit mono | Small, in-process, low latency; yields raw PCM we can later send to the robot |
| VAD | Silero ONNX model bundled with faster-whisper; gates and trims the released clip | No PyTorch (~2 GB) and no extra dependency; drops taps/coughs that make Whisper hallucinate |
| Interrupts | Pressing the key mid-turn stops playback, cancels the Claude stream, starts listening | Natural feel; the robot button needs the same behavior; easier to build in than retrofit |
| Config loading | Small stdlib `.env` parser | Avoids python-dotenv for a trivial format |

New runtime dependencies: `sounddevice`, `numpy`, `faster-whisper`, `anthropic`, `comtypes`.

## Architecture

One asyncio process. Each blocking piece (mic, Whisper, SAPI, playback) sits behind a small interface and runs in a thread. The orchestrator is a state machine, `idle → listening → thinking → speaking`, using the same states the robot will receive in M4.

| Module (`server/src/desk_buddy/`) | Job |
|---|---|
| `config.py` | `Settings` dataclass from `server/.env` with defaults; new key `PTT_KEY` |
| `ptt.py` | Key poller thread (~20 ms) emitting `down` / `up` events on change |
| `audio.py` | `Microphone` (16 kHz int16 InputStream, buffers between start/stop); `Speaker` (plays PCM, stoppable instantly) |
| `stt.py` | `Transcriber`: VAD gate + trim, then faster-whisper; returns `""` when there is no speech |
| `llm.py` | Streams Claude text deltas; `SentenceSplitter` yields complete sentences |
| `tts.py` | `Synthesizer` protocol `synthesize(text) -> bytes`; `SapiSynthesizer` (Piper later, same interface) |
| `loop.py` | Orchestrator, state machine, cancellation |
| `__main__.py` | Entry point: `uv run python -m desk_buddy` |

Scope guard: M1 uses a minimal placeholder system prompt (short, spoken, no markdown) and handles one turn at a time. Personality, emotion tags and session memory are M2.

## Data flow

1. **Key down:** cancel any turn in progress → `listening` → mic starts.
2. **Key up:** mic stops and hands over the PCM. Ignore clips under 0.3 s; cap recording at 30 s. → `thinking`.
3. **STT** in a worker thread. No speech → log and return to `idle`.
4. **Claude stream → SentenceSplitter → queue → TTS worker → Speaker.** Sentence N+1 is synthesized while sentence N plays. → `speaking` when the first audio starts.
5. Stream and playback both done → `idle`.

**Interrupts:** each turn is one asyncio `Task`; a key press mid-turn calls `task.cancel()`. The Claude stream closes through its async context manager, and `Speaker.stop()` aborts playback at once. A Whisper or SAPI call already running in a thread can't be killed, so its result is discarded.

## Error handling

- **Startup (fail fast, clear message):** missing `ANTHROPIC_API_KEY`; no input or output audio device (list the available ones). Preload Whisper and SAPI so the first turn isn't slow; log the one-time Whisper model download.
- **Mid-turn API errors** (network, rate limit, overloaded): log, speak a canned line ("I can't reach my brain right now"), return to `idle`. One bad turn never crashes the loop.
- **Ctrl+C:** clean shutdown of audio and the key thread.
- **Per-turn log:** transcript, reply, timings (STT ms, time to first token, time to first audio).

## Testing

**Unit tests (default `uv run pytest`, no hardware or network):**
- `config`: defaults, overrides, blank values fall back to defaults, missing API key error.
- `SentenceSplitter`: "Dr.", decimals ("3.5"), "...", "?!", chunk boundaries, final flush.
- `ptt`: edge detection with an injected key-state function.
- Orchestrator with fakes (mic, transcriber, LLM, synthesizer, speaker):
  - normal turn state sequence
  - empty transcript goes back to idle
  - API error speaks the canned line
  - interrupt mid-speech stops the speaker, cancels the stream and starts listening
  - sentences play in order

**Integration tests (marked `integration`, skipped by default; `uv run pytest -m integration`):**
- Real SAPI returns non-empty 16 kHz mono int16 PCM.
- TTS → STT round trip: SAPI says "hello buddy", and faster-whisper transcribes roughly that.
- Real VAD on silence finds no speech.
- One live Claude call (needs the key and network).

**Manual checklist:**
- hold Right Ctrl and ask a question
- the reply starts before Claude finishes
- interrupting mid-reply works
- a brief tap is ignored
- holding the key in silence is ignored

## Done when

- The manual checklist passes.
- `uv run pytest` and `uv run ruff check` are clean.
- `.env.example` lists `PTT_KEY`.
- `docs/decisions.md` and `CLAUDE.md` are updated: sounddevice confirmed, SAPI via comtypes, VAD from faster-whisper's bundled ONNX.
