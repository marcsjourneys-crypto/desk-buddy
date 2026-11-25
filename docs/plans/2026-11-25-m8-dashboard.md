# M8 Dashboard: design and plan

Approved by the user on 2026-11-25.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

## Goal

A local web page for CARL with three tabs:
- **Live:** the state and emotion, the transcript, actions and the robot link.
- **Settings:** non-secret settings, edited in the browser.
- **Personality:** CARL's character paragraph and signature lines.

In-page controls (push-to-talk, stop, forget, music buttons) come later.

## Decisions

- **This PC only:** a second listener on **`127.0.0.1:8766`** (`DASHBOARD_PORT`, default 8766) in the same process and asyncio loop. The robot keeps its LAN port 8765. There's no password, because nothing off this PC can connect.
- **No build step:** plain HTML, CSS and JS served by FastAPI. No npm, no framework, no new dependencies.
- **Live updates** go over one WebSocket (`/api/events`), with an in-memory ring of the last 100 events for a page that has just loaded. Nothing is written to disk.
- **Settings file:** `%LOCALAPPDATA%\desk-buddy\settings.json`. Precedence is **dashboard over `.env` over default**, and the page shows where each value comes from and has a reset for each.
- **Personality file:** `%LOCALAPPDATA%\desk-buddy\personality.json`. Only the **character paragraph** and the **signature lines** are editable. The technical rules stay in code and are always included: spoken style, emotion tags, the interruption and action notes, the untrusted-data rule, and the notes and music instructions.
- **Secrets never reach the page:**
  - the Anthropic key, `ROBOT_TOKEN`, `SPOTIFY_CLIENT_ID` and the Spotify token are never sent;
  - host and port settings aren't editable.
- **CSRF protection:**
  - every change (PUT/POST) and the WebSocket must carry an `Origin` that is `http://127.0.0.1:8766` or `http://localhost:8766`;
  - every change must carry an `X-CARL-Token` header holding a random per-process token, which is injected into the served HTML;
  - anything else gets a 403.
- **The dashboard is never fatal:** a busy port logs a warning and CARL runs without it, and a corrupt JSON file logs a warning, is ignored and shows a banner.

## Editable settings

| Group | Keys | Applies |
|---|---|---|
| Computer control | `COMPUTER_CONTROL`, `MUSIC_DUCK` | next question |
| Conversation | `MAX_SENTENCES`, `HISTORY_TURNS` | next question |
| Voice | `TTS_SPEED` | next sentence |
| Voice | `TTS_VOICE` | after restart |
| Notes | `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER` | next question |
| Brain | `LLM_MODEL`, `LLM_EFFORT` | next question |
| Speech recognition and keys | `STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `PTT_KEY` | after restart |

- **Validation** reuses `config.py`'s validators, with no duplicated rules.
- **Live keys:** the running objects pick up a new value without a restart.
  - **The settings that affect turns** (computer control, ducking, max sentences, history, model, effort, the vault) are read from a settings holder at each turn. Tools are rebuilt when computer control or the vault changes.
  - **TTS speed** is set on the synthesizer.
  - **History turns:** a smaller value trims at the next turn.
- **Restart keys:** the dashboard's value is saved; the page shows "after restart"; startup reads the merged value.

## Personality file

```json
{
  "character": "You are CARL-42, …",
  "lines": {
    "greeting": {"text": "…", "emotion": "happy"},
    "farewell": {…}, "pondering": {…}, "task_done": {…},
    "error": {…}, "refusal": {…}, "forget": {…}
  }
}
```

- **Missing keys:** fall back to the code defaults.
- **Limits:**
  - `character` is at most 3000 characters, with no `{`/`}` format traps; the prompt is assembled without `str.format` on user text.
  - Each line is at most 200 characters, and its emotion must be one of `EMOTIONS`.
- **Reset:** per item.
- **Read-only preview:** the page shows the full assembled system prompt.
- **When edits apply:** at the next question. The greeting and farewell are read when spoken.

## Events (`/api/events`; the newest 100 are kept)

| `type` | Fields |
|---|---|
| `state` | `name` |
| `emotion` | `name` |
| `heard` | `text`, `stt_ms` |
| `reply` | `text`, `emotion`, `first_token_ms`, `first_audio_ms` |
| `line` | `text`, `emotion` (a canned line) |
| `action` | `tool`, `args` (bounded), `result` (bounded, untrusted-wrapped text passed as data), `outcome` (`ok`, `failed`, `refused`) |
| `robot` | `connected`, `fw_version` |
| `interrupted` | — |

- **Fields:** each event carries `ts` (ISO). Text is sent as JSON strings and the page renders it with `textContent`, never `innerHTML`, because transcripts and Spotify names are untrusted.
- **Delivery:** the bus is non-blocking, with a bounded queue per subscriber, and a slow page loses events rather than stalling CARL.

## API (on `127.0.0.1:8766` only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The page, with the CSRF token injected. |
| GET | `/static/*` | CSS and JS. |
| GET | `/api/status` | Voice, music tools on/off, robot link, dashboard and CARL version. No secrets. |
| GET | `/api/settings` | For each key: value, source (dashboard / .env / default), and applies (now / restart). |
| PUT | `/api/settings` | Partial update. Validates and saves; returns the new view or per-field errors. |
| DELETE | `/api/settings/{key}` | Resets one key to `.env` or the default. |
| GET | `/api/personality` | The character, the lines and the assembled preview. |
| PUT | `/api/personality` | Partial update. Validates and saves. |
| DELETE | `/api/personality/{item}` | Resets `character` or one line. |
| WS | `/api/events` | Sends the recent history, then live events. |

## Tasks

1. **Event bus.**
   - `events.py` (`EventBus`: publish, subscribe, a ring of 100, bounded per-subscriber queues).
   - Emitters in the orchestrator (state, emotion, heard, reply, line, interrupted, with timings), `ActionGuard` (action) and the robot server (robot connect and disconnect).
   - Tests.
   - Commit: `Add an event bus for the dashboard`.
2. **Settings store.**
   - `settings_store.py`: load, merge, validate with the config validators, save atomically (temp file plus `os.replace`), reset, and the source and applies view.
   - `config.load_settings` merges the file.
   - A live settings holder read at each turn, with the live-apply hooks.
   - Tests: precedence, validation, a corrupt file, atomic save, live apply.
   - Commit: `Store dashboard settings over .env`.
3. **Personality store.**
   - `brain/personality.py` is split into an editable character and lines plus locked rules.
   - `personality_store.py`: load, validate, save, reset.
   - Prompt assembly with no format injection.
   - Tests: the locked rules are always present, limits, reset, a corrupt file.
   - Commit: `Make CARL's personality editable`.
4. **Dashboard server.**
   - `dashboard.py`: the FastAPI app with the API above, started on `127.0.0.1:DASHBOARD_PORT` with the M4 `start_server` pattern.
   - The startup log line `Dashboard: http://127.0.0.1:8766`.
   - Tests on a real loopback server:
     - no secret ever appears in any response (search the bodies for planted secret values);
     - changes are rejected without the token or with a foreign Origin, and the WebSocket with a foreign Origin;
     - it binds 127.0.0.1;
     - a busy port isn't fatal.
   - Commit: `Serve the dashboard on localhost`.
5. **The page.**
   - `dashboard_static/index.html`, `app.js`, `style.css`, with three tabs.
   - **Live:** the status bar and an SVG eye that follows state and emotion; transcript; actions.
   - **Settings:** forms grouped as above, showing source, applies and reset.
   - **Personality:** a textarea, the lines and the preview.
   - Uses `textContent` only, reconnects with back-off, and has no external CDN.
   - Commit: `Add the dashboard page`.
6. **Docs.**
   - `CLAUDE.md` (dashboard, settings precedence, M8 status), `server/README.md`, `.env.example` (`DASHBOARD_PORT`), `decisions.md`, `status.md`.
   - Commit: `Document M8 dashboard`.

## User check

1. Open `http://127.0.0.1:8766` while talking to CARL, and watch the live transcript, state and actions.
2. Turn music ducking off, ask a question with music playing, and check that the music doesn't duck.
3. Change the greeting, restart CARL, and hear the new one.
4. Make the character paragraph grumpier; the next answer should sound different. Then reset it.
