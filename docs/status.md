# Status

Where the project stands, so work can resume after a break. Update this when a milestone moves.

## 2026-11-24 — M5 firmware bring-up (servo check pending)

- **Built** (`firmware/`): one ESP32-S3 app with a serial test menu (`help`, `info`, `eyes idle|happy|sleep|blink|test`, `fps`, `tone`, `vol`, `mic`, `rec`/`play`, `servo`).
  - Eyes: parametric two-lid model, blending, idle blinks, 25.7 fps on both GC9A01s.
  - Audio: I2S speaker and INMP441 mic, 16 kHz 16-bit mono, record/playback loopback.
  - Servos: eased, speed-limited moves with no PWM at rest.
- **Plan:** `docs/plans/2026-11-24-m5-firmware-bringup.md` (design: `…-m5-firmware-bringup-design.md`). Pin map: `docs/wiring.md`.
- **Tests:** 28 Unity tests pass on the board; the server's 377 still pass.
- **Hardware checks by the user, passed:** LED and PSRAM (8 MB), both eyes, tone and volume, mic levels, record/playback.
- **Pending:** wire the servos to the 5 V supply (with the 1000 µF cap and a common ground), centre the horns by hand, then run `servo`.
- **Wiring notes:** the amp is on 3.3 V because the board's 5V pin gives no USB power; it moves to the 5 V supply later. Use `pio device monitor -p COM9` so the monitor never grabs a Bluetooth port.
- **Next:** the servo check, then M6 (the robot joins the link: Wi-Fi, WebSocket client, streaming mic and speech, eyes follow state and emotion).

## 2026-11-24 — M4 robot link done

- **Built:**
  - A FastAPI `/robot` WebSocket in the same loop as the voice loop (`server.py`), with token auth (`ROBOT_TOKEN`; blank means off).
  - `link/`: the protocol, `RobotLink` with `RobotMic` and `RobotPlayer`, and `DeviceSwitch`, which hands the mic, speaker and button to the robot while it is connected.
  - The fake robot: `python -m desk_buddy.fake_robot` (a test client and a voice tool).
  - `docs/protocol.md`, the final protocol, including the M6 LAN and firewall steps.
- **Plan:** `docs/plans/2026-11-23-m4-robot-link.md` (design: `…-m4-robot-link-design.md`).
- **Tests:** 377 unit tests pass, including a real loopback server driven by the fake robot. Integration tests are unchanged (13).
- **Voice check by the user:** a question, an interrupt and a disconnect through the fake robot. The follow-up after an interrupt no longer mentions it (the M2 fix, now checked).
- **Not checked by hand:** Right Ctrl after the robot disconnects (covered by tests).
- **Deferred to M6:** auth-failure throttling, a connection cap, button-flood limits, bounded queues.
- **Next:** M5 (firmware bring-up) and then M6 (the robot joins the link). Before M7, mark `read_notes` output as untrusted.

## 2026-11-23 — M3 Obsidian dictation done

- **Built:**
  - `take_note` (daily line or idea file) and `read_notes`, in `tools/notes.py`.
  - The tool registry, in `tools/registry.py`.
  - A streaming tool loop in `ClaudeChat` (up to 3 rounds).
  - `OBSIDIAN_VAULT_PATH`. When it's blank, the tools are off and Buddy says notes aren't set up.
- **Plan:** `docs/plans/2026-11-23-m3-obsidian.md`.
- **Tests:** 256 unit and 13 integration tests pass. The integration tests include a live note round trip on the real API.
- **Voice check by the user:** a daily note, an idea file whose misheard title was cleaned up, and a read-back summary. All worked.
- **Not checked by hand:** that cutting Buddy off and then asking a follow-up no longer makes it say "interrupted" (the M2 fix).
- **Before M7:** mark `read_notes` output as untrusted (see `decisions.md`).
- **Next:** M4, the robot link (WebSocket server, token auth, `docs/protocol.md`, a fake robot client). Decide FastAPI vs plain websockets there.

## 2026-11-23 — M2 personality done

- **Built:**
  - Character prompt.
  - Emotion tags, shown in the log as `Buddy [happy]: …` and stripped from speech.
  - Memory until restart (`HISTORY_TURNS`, default 20).
  - "New topic" / "forget that" clears the memory.
- **Plan:** `docs/plans/2026-11-23-m2-personality.md`.
- **Tests:** 171 unit and 12 integration tests pass. The integration tests include a live Labrador follow-up.
- **Voice check by the user:**
  - The follow-up "Are they good hunters?" worked.
  - "New topic" cleared the memory.
  - Interrupts worked.
  - The replies were short and calm.
- **Fixed after the voice check:** Claude had copied the "(interrupted)" marker into a reply. Interruptions are now noted on the user's next message instead.
- **Not checked by hand:** the M1 quick-tap check and the check with another window focused.
- **Next:** M3, Obsidian dictation (`take_note` and `read_notes` tools).

## 2026-11-23 — M1 voice loop working end to end

First live run: the user asked about owls and Buddy answered out loud. STT 666 ms, first token 1245 ms, first audio 1815 ms (from key release). Startup, reply and the "Bye" on exit are confirmed. Decisions, `CLAUDE.md` and `server/README.md` are updated.

**Still to check by hand before calling M1 closed** (Task 11, Step 1):
- Pressing the key mid-reply stops speech and logs "Interrupted".
- A quick tap logs "Ignored … tap".
- Holding the key in silence for 2 s logs "No speech detected".
- It works while another window has focus.

**Next after that:** M2 (personality).

## Earlier on 2026-11-23 — M1 (voice loop on the PC): code done, manual check blocked

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

**Mic fixed (later on 2026-11-23).** The cause was the Microsoft Store Python, not the driver: its app package has no mic capability. `server/pyproject.toml` now forces uv's own Python. After rebuilding `.venv`, every mic opens and all tests pass. If the mic fails again, check `uv run python -c "import sys; print(sys._base_executable)"` isn't under `WindowsApps`. The notes below are kept for history.

**Was blocked: the PC mic can't be opened.**
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
