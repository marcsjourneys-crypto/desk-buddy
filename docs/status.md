# Status

Where the project stands, so work can resume after a break. Update this when a milestone moves.

## 2026-11-25 — Web search and About you done

- **Built:**
  - **About you:** a card at the top of the Personality tab (name, town, region, ISO country code, a short note; all optional), saved in `personality.json`. It becomes an "About the user" block in the prompt, and the card says what goes to Anthropic and the search provider.
  - **Web search:** Anthropic's `web_search_20250305` server tool, on by default (`WEB_SEARCH`, `WEB_SEARCH_MAX` 1–5, default 2, in a new "Web" group on the Settings tab), using the About-you location. Searches show on the Live tab and in the log as `Action: web_search …`, and are remembered as a system note on the user's message. If the API key doesn't allow search, CARL logs a hint and carries on without it until the restart.
  - Review fixes: no sentence breaks inside cited answers, a narrower "search is off" check, searches reported as soon as their results arrive, and stricter About-you checks.
- **Plan:** `docs/plans/2026-11-25-web-search-about-you.md`.
- **Tests:** 909 server tests pass (17 integration tests deselected, including the new live web search test, not yet run).
- **Check by the user (passed 2026-11-25):**
  1. Fill in About you (name, town, country, a note). Ask "what's my name?" and check that CARL knows.
  2. "What's the weather tomorrow?": CARL searches, answers for your town, and the dashboard shows the search with its sources.
  3. "Who won <a recent match>?" gets a current answer without reading out web addresses.
  4. Set `WEB_SEARCH` off on the dashboard, ask again, and check that CARL says it can't look things up.
  5. Ask "where did you find that?" after a search: CARL knows it searched.
- **Next:** optionally `uv run pytest -m integration tests/integration/test_web_search_live.py` (it needs web search enabled for the organisation in the Anthropic Console), local skills (allowlisted scripts by name, spoken yes for powerful ones), a write-only Keys section and first-run setup, the servo check, then in-page controls or the rest of M7.

## 2026-11-25 — M8 dashboard done

- **Built:**
  - `http://127.0.0.1:8766` (`DASHBOARD_PORT`), served from CARL's own event loop. Its Live tab shows the state, an SVG eye, the transcript with timings, and actions. Its Settings tab edits non-secret settings: the dashboard wins over `.env`, and each one shows its source, when it applies and a reset. Its Personality tab edits the character and the signature lines, with a prompt preview.
  - `events.py` (the event bus), `settings_store.py` (the settings file and live apply), `personality_store.py`, `dashboard.py` and `dashboard_static/`.
  - Safety: Host, Origin and token checks, CSP, `textContent` only, and no secrets in any response (tested).
- **Plan:** `docs/plans/2026-11-25-m8-dashboard.md`.
- **Tests:** 810 server tests pass (16 integration tests deselected).
- **Browser check by the user (passed 2026-11-25):**
  1. Open the page while talking to CARL: the transcript, state and actions update live.
  2. Turn ducking off, then ask a question with music playing: the music doesn't duck.
  3. Change the greeting, restart CARL, and hear the new one.
  4. Make the character grumpier: the next answer sounds different. Then reset it.
- **Next:** web search through the Anthropic API (to be planned), the servo check, then in-page controls or the rest of M7.

## 2026-11-25 — M7 Spotify control built (voice check pending)

- **Built:**
  - Untrusted-text wrapping for tool results (`tools/untrusted.py`), used by `read_notes` and the music tools, with a prompt rule.
  - `ActionGuard` (`tools/actions.py`): `COMPUTER_CONTROL` off by default, 3 actions per question and 8 per minute, one `Action:` log line per attempt.
  - Spotify (`spotipy==2.26.0`): `tools/spotify.py` with device choice and start-and-wait, and the tools `play_music`, `music_control`, `music_volume` and `now_playing`. Login: `python -m desk_buddy.spotify_login` (PKCE).
  - Ducking while CARL listens and talks (`tools/ducking.py`, `MUSIC_DUCK`).
- **Plan:** `docs/plans/2026-11-25-m7-spotify.md`.
- **Tests:** 620 server tests pass (16 integration tests deselected; the live Spotify search passes with the user's login).
- **Voice check by the user (pending):** "Play some Queen"; pause, resume, skip and back; "What's playing?"; "Turn the music down"; ask a question while music plays (it ducks and comes back); with Spotify closed, ask for music (CARL starts it); ask for 4 things in one go (CARL stops at 3).
- **Next:** the voice check, the servo check, then the rest of M7 (media keys, apps, websites, PC volume) or M8 (the dashboard).

## 2026-11-25 — CARL's voice (Piper) done (voice check pending)

- **Built:** Piper TTS (`piper-tts==1.8.0`) as the default engine, with CARL's voice `en_GB-northern_english_male-medium`, chosen by the user from an audition of 8 voices. New settings: `TTS_VOICE`, `TTS_SPEED`, `VOICES_DIR`. A missing voice downloads at startup (about 60 MB); `TTS_ENGINE=sapi` is the fallback. Piper's 22050 Hz output is resampled to 16 kHz in numpy (`tts.resample`).
- **Speed (i7-1355U, measured outside the app):** 90–210 ms per typical sentence including resampling (about 20× real time); SAPI took 16–20 ms. So first audio should come roughly 0.1–0.2 s later than with SAPI.
- **Tests:** 478 server tests pass (15 integration tests deselected, including 2 new real-Piper ones, which pass).
- **Voice check by the user (pending):** start CARL and listen to the greeting in the new voice; ask a question and note the first-audio time in the log; try it through the robot too.
- **Next:** the servo check, then M7 (PC computer control). Before M7, mark `read_notes` output as untrusted.

## 2026-11-25 — CARL-42 personality done (voice check pending)

- **Built:**
  - The character is renamed Buddy → **CARL-42** (CARL), with a light *Hitchhiker's* prompt: a dry, cosmic aside about one reply in four, and the 42 rule. Logs read `CARL [happy]: …`. Package and commands keep the `desk_buddy` name.
  - Signature lines (`brain/personality.py`), none remembered, all interruptible: `GREETING` at startup and on robot connect (deduped within 60 s), `FAREWELL` on Ctrl+C (bounded to 5 s, hard cap 6 s, replaces "Bye"), `PONDERING` when no reply audio 4 s after transcription ends (at most every 5 min), `TASK_DONE` after a successful tool call one time in four.
  - New error line: "Sorry, my brain seems to be on the other side of the galaxy right now."
- **Plan:** `docs/plans/2026-11-25-carl-personality.md`.
- **Tests:** 432 server tests pass (13 integration tests deselected).
- **Voice check by the user (pending):** the greeting at startup; dry asides now and then, not every time; an unknowable question (maybe 42); a slow question gets the pondering line; a note sometimes gets "Another mystery of the universe solved."; Ctrl+C says the farewell; a robot linking later is greeted, one linking right after startup isn't.
- **Next:** the Piper voice audition, the servo check, then M7 (PC computer control). Before M7, mark `read_notes` output as untrusted.

## 2026-11-24 — M6 robot joins the link done

- **Built (server):**
  - The PC key (Right Ctrl, in the Buddy window) drives a connected robot's mic, with a 250 ms release delay.
  - Hardening for the LAN: a per-IP bad-token throttle, a cap on half-open connections, a button-down rate limit and a 128-item send queue that closes with `1011` on overflow.
  - The LAN IP and the exact robot `server …` command are logged at startup when `SERVER_HOST=0.0.0.0`.
  - The fake robot streams while Buddy is listening, like the real one.
  - Speech goes to the robot in 100 ms frames (the ESP32's TCP stack couldn't keep up with 20 ms ones).
- **Built (firmware, `0.2.0-m6`):**
  - Settings in NVS over serial (`wifi`, `server`, `token`, `config`, `forget`) and a `net` status command.
  - A Wi-Fi and WebSocket client (`src/net.cpp`) with its own back-off and a timing-based refusal rule (the library hides close codes).
  - A mic task that streams DC-blocked 20 ms frames while the state is `listening`, and a playback task fed from a 1 s PSRAM ring.
  - The eyes follow state and emotion, with new looks (`sad` with tilt; the lead's crescents, `lookaround`, and the animated thinking swirl, surprised burst and `excited` sparkle).
- **Plan:** `docs/plans/2026-11-24-m6-robot-joins.md` (design: `…-m6-robot-joins-design.md`). Protocol: `docs/protocol.md`.
- **Tests:** 51 Unity tests pass on the board; 400 server tests pass (13 integration tests deselected).
- **Voice check by the user, passed:** the robot linked over Wi-Fi; a question through its mic was transcribed correctly; the reply played on its speaker; an interrupt and a reconnect worked. A graceful server stop and a hard kill are both noticed within ~0.5 s (the eyes sleep). Measured from key release: STT 1048 ms for 2.9 s of audio, first token 2577 ms, first audio 2969 ms.
- **To do by the user:** add the Private-only firewall rule for port 8765 (`docs/protocol.md`). Without it, each connect to a stopped server hangs the robot for about 5 s. A DHCP reservation for the PC is recommended.
- **Still waiting on hardware:** the servo check (servos on the 5 V supply), servos following `look`, the antenna LED and the touch pad. Their messages stay reserved.
- **Next:** the servo check, then M7 (PC computer control). Before M7, mark `read_notes` output as untrusted.

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
