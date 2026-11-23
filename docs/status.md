# Status

Where the project stands, so work can resume after a break. Update this when a milestone moves.

## 2026-11-23 — M1 (voice loop on the PC): code done, manual check blocked

**Done** (all pushed to `main`, up to `95ddefd`):
- M0 repo setup.
- M1 design: `docs/plans/2026-11-23-m1-voice-loop-design.md`.
- M1 plan: `docs/plans/2026-11-23-m1-voice-loop.md`.
- M1 Tasks 1–10: config, push-to-talk, sentence splitter, Claude client with the `LLM_PROVIDER` seam, mic/speaker, SAPI TTS, Whisper STT with VAD, orchestrator, entry point. Every task was code-reviewed, and the fixes are committed.
- Tests:
  - `uv run pytest`: 113 passed.
  - `uv run pytest -m integration`: 10 passed, 1 skipped (live Claude, no key yet).
  - Ruff clean.
- Measured on the i7-1355U laptop:
  - Whisper `base.en` int8 takes ~0.75 s for a 3 s clip, and the transcription was exact.
  - Silence is rejected in <10 ms.
  - The SAPI voice renders correctly at 16 kHz.

**Blocked: the PC mic can't be opened.**
- **Symptom:** opening the built-in "Microphone Array (Intel® Smart Sound)" fails in every host API (MME error 1, DirectSound E_FAIL, WASAPI invalid device), every format, including its native one. Output works.
- **Ruled out:**
  - the sample rate or format
  - the Claude Code sandbox (it fails the same with the sandbox off)
  - Windows privacy settings (all Allow)
  - a restricted process token (medium integrity)
  - another app holding the mic
- **Context:** Teams used the mic on 2026-11-21. Python never appears in Windows' mic-usage list.
- **Next step:** reboot (suspected stuck audio driver), then run the probe from `server/`:
  `uv run python scripts/probe_mic.py`
  - If it says OK, continue with Task 11.
  - If it still fails, check Settings → System → Sound → Input → "Test your microphone" and any mic-mute key. Try the fifine USB mic as a second device.
  - Desk Buddy now fails at startup with a clear message naming the device and format.

**Remaining for M1 (Task 11 in the plan):**
1. Create `server/.env` from `.env.example` and set `ANTHROPIC_API_KEY`. The user does this; Claude never edits `.env`.
2. Run `uv run python -m desk_buddy` (from `server/`), hold Right Ctrl, and work through the manual checklist in Task 11.
3. Record the M1 decisions and measured latencies in `docs/decisions.md`, update `CLAUDE.md` (Server stack lines) and `server/README.md`, then commit.

**Decided during M1, not yet in `decisions.md`** (Task 11 writes them up):
- LLM provider seam: `LLM_PROVIDER` with only `anthropic` for now. OpenAI-compatible providers (e.g. Kimi) come after M3.
- New `.env` keys: `LLM_PROVIDER`, `LLM_EFFORT` (default `low`) and `PTT_KEY` (default `right_ctrl`).
- `anthropic` SDK 2.x uses `httpx2`, not `httpx`.
