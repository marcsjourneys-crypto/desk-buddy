# Desk Buddy server

The Python brain for Desk Buddy (Windows). Managed with [uv](https://docs.astral.sh/uv/).

- Set up: `uv sync`, then copy `.env.example` to `.env` and fill it in.
- Run: `uv run python -m desk_buddy`, then hold Right Ctrl to talk (change the key with `PTT_KEY`). The character is CARL-42 (CARL): it greets you at startup and says goodbye on Ctrl+C. Its personality and lines are in `src/desk_buddy/brain/personality.py`, and can be edited on the dashboard.
- Dashboard: while CARL runs, open `http://127.0.0.1:8766` (the startup log says `Dashboard: http://127.0.0.1:8766`; change the port with `DASHBOARD_PORT`). It only listens on this PC.
  - **Live:** CARL's state and eye, what you said and what CARL said (with timings), and every action on this PC.
  - **Settings:** non-secret settings. A value set here wins over `.env`, each shows where it comes from and when a change applies (next question, next sentence or after a restart), and Reset goes back to `.env` or the default. Saved in `%LOCALAPPDATA%\desk-buddy\settings.json`. Secrets, hosts and ports stay in `.env`.
  - **Personality:** an About you card (your name, town, region, two-letter country code such as GB, and a short note; all optional), CARL's character paragraph and signature lines, with a reset for each and a read-only preview of the full prompt. Saved in `%LOCALAPPDATA%\desk-buddy\personality.json`. About you goes to Anthropic with every question, and the location to the search provider when CARL searches.
  - If the port is busy, CARL runs without the dashboard. If a saved file is broken, it is ignored and the page shows a banner. Delete the file to start afresh.
- Voice: CARL speaks with Piper's `en_GB-northern_english_male-medium` by default. On the first run it is downloaded (about 60 MB) into `%LOCALAPPDATA%\desk-buddy\voices` (`VOICES_DIR`). Change it with `TTS_VOICE` (another Piper voice name, or a path to an `.onnx` file with its `.onnx.json` next to it) and the pace with `TTS_SPEED` (0.5–2.0). `TTS_ENGINE=sapi` uses the built-in Windows voice instead (no download). The startup log shows `Voice: piper <name>` or `Voice: sapi`.
- Notes: set `OBSIDIAN_VAULT_PATH` in `.env` to an existing folder (CARL's own vault) to turn on `take_note` / `read_notes`. Daily notes go in `Daily/`, ideas in `Ideas/` (`OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER`). Leave it blank to turn the tools off.
- Web search: CARL looks up current facts (news, weather, scores, opening hours) with Anthropic's web search tool, using your About-you location for local answers. On by default: `WEB_SEARCH=off` turns it off, `WEB_SEARCH_MAX` (1–5, default 2) sets the searches per question; both are on the dashboard's Settings tab too. Each search is logged as `Action: web_search {"query": …} -> ok (N results)` and shown on the Live tab. Web search may need turning on for your organisation in the Anthropic Console (it's billed per search). If the API says it isn't enabled, CARL logs a hint, answers without it and keeps it off until the next restart.
- Music (Spotify Premium): CARL can play, pause, skip, change the music volume and say what's playing. Set it up once:
  1. At developer.spotify.com, open the Dashboard and create an app (pick "Web API") with the redirect URI `http://127.0.0.1:8888/callback`. Copy its Client ID.
  2. In `.env`: `SPOTIFY_CLIENT_ID=<id>` and `COMPUTER_CONTROL=on`.
  3. Run `uv run python -m desk_buddy.spotify_login` and click Agree in the browser. The login is saved in `%LOCALAPPDATA%\desk-buddy\spotify_token.json` (never print or share it).

  The startup log says `Music tools: on (Spotify, ducking)` or why they're off. At most 3 actions per question and 8 a minute; each is logged as `Action: …`. If Spotify isn't running, CARL opens it. `MUSIC_DUCK=off` stops CARL lowering the music while it listens and talks.
- Apps and websites (needs `COMPUTER_CONTROL=on`): on the dashboard's **Apps** tab, click Find apps (it asks Windows for its Start apps), tick the ones CARL may open and give each one to three spoken names ("browser"); add bookmarks (a name and an `https://` address). Saved in `%LOCALAPPDATA%\desk-buddy\apps.json` and `bookmarks.json`; changes apply at the next question. Then "open notepad" or "open the weather". After a web search, "open that page" makes the robot ask "Shall I open the page on <site>?" once CARL has replied: answer "yes" (or "yeah, go ahead", "okay, open it") within a minute, at your very next question, and it opens in your browser; anything else cancels it. CARL never opens pages on this PC or your local network. Every attempt is logged as `Action: …` and shown on the Live tab.
- YouTube (needs `COMPUTER_CONTROL=on`): "search YouTube for bread recipes" opens the results page, with no key needed. To let CARL play a chosen video ("play lo-fi beats on YouTube", "play the latest video from <channel>"), get a YouTube Data API key once:
  1. At console.cloud.google.com, create a project (or pick one).
  2. Under APIs & Services → Library, enable "YouTube Data API v3".
  3. Under APIs & Services → Credentials, create an API key and restrict it to "YouTube Data API v3".
  4. In `.env`: `YOUTUBE_API_KEY=<key>` (a secret: it's never shown on the dashboard or logged), then restart.

  The free quota is enough for about 100 plays a day. The startup log says `YouTube: on (play and search)`, `YouTube: search only (no YOUTUBE_API_KEY)` or `YouTube: off (COMPUTER_CONTROL is off)`.
- Media keys (needs `COMPUTER_CONTROL=on`): "pause the video", "next", "resume it" press the PC's play/pause, next, previous and stop keys for whatever is playing (Windows sends them to the app that last played media). Spotify is still controlled through its API. Startup log: `Media keys: on`.
- Skills (needs `COMPUTER_CONTROL=on` and [uv](https://docs.astral.sh/uv/) on `PATH`): your own Python scripts, run by voice ("run hello time", "email the team that I'm running late"). Set `SKILLS_DIR` (Settings tab, "Skills" group) to a folder, put one subfolder per skill in it, and tick the ones CARL may run on the **Apps** tab. Try the two in `examples/skills/` first (copy them into your folder). Startup log: `Skills: on (<slugs>)` or why they're off. See [Writing a skill](#writing-a-skill).
- Ducking: with `MUSIC_DUCK` on (the default), CARL lowers Spotify through its API (when music is set up) and every other app playing sound (YouTube in the browser, a podcast) in the Windows volume mixer while it listens and talks, then puts each back, unless you changed it meanwhile. It needs no computer control. If CARL crashes mid-turn, it puts the apps back at the next start (`%LOCALAPPDATA%\desk-buddy\ducked_apps.json`). Startup log: `Ducking other apps: on`.
- Robot link: set `ROBOT_TOKEN` in `.env` to turn on the robot WebSocket (`ws://127.0.0.1:8765/robot`; change it with `SERVER_HOST` / `SERVER_PORT`). Generate a token with `uv run python -c "import secrets; print(secrets.token_urlsafe(24))"`. Leave it blank to turn the link off. Protocol: `docs/protocol.md`.
- Real robot (M6): set `SERVER_HOST=0.0.0.0` in `.env` and add the Private-only Windows Firewall rule for the port, once, as administrator (PowerShell and cmd versions in `docs/protocol.md`). At startup CARL logs the PC's LAN address and the exact robot command, e.g. `Robot link: on (ws://192.168.1.20:8765/robot — set the robot with: server 192.168.1.20:8765)`. Type that `server …` line into the robot's serial menu (`firmware/README.md`). While the robot is connected, hold Right Ctrl in the CARL window to talk through the robot.
- Fake robot: with CARL running, start `uv run python -m desk_buddy.fake_robot` in a second terminal. It connects with the same `.env` and acts like the real robot: it streams the PC mic while CARL is listening and plays replies on the PC speaker. Talk by holding Right Ctrl in the CARL window (not the fake robot's).
- Test: `uv run pytest`
- Integration tests (real audio, COM, models, network): `uv run pytest -m integration`
- Lint / format: `uv run ruff check` and `uv run ruff format`

## Writing a skill

A skill is a folder inside `SKILLS_DIR`. CARL runs its `main.py`, hands it the inputs as JSON on stdin, and says what it prints in its own words. Two working examples are in `examples/skills/` at the repo root: `hello-time` (a choice, no packages) and `send-note-email` (free text and a spoken "yes"; it only prints what it would send).

### The folder

```
<SKILLS_DIR>/
  order-check/          the folder name is the skill's slug: 1-40 of a-z, 0-9 and -
    skill.json          the manifest (required)
    main.py             what runs (required)
    pyproject.toml      optional: the skill's own packages (needs uv.lock after setup)
    .env                optional: the skill's own secrets; CARL never reads it
    ...                 any other files and subfolders the script uses
```

The slug can't be a name Windows keeps for devices (`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`). CARL's tool for it is `skill_<slug>`, with `-` becoming `_`.

### The manifest (`skill.json`)

UTF-8 JSON (a BOM is fine), at most 16 KB. Unknown keys and keys given twice are errors; the Apps tab shows each problem next to the skill, with the field it's in.

```json
{
  "name": "Send note email",
  "spoken_names": ["send note email", "note email"],
  "description": "Emails a short message to the user's team. Use it only when the user asks to email or tell the team something.",
  "inputs": {
    "choices": {
      "priority": {
        "options": ["normal", "urgent"],
        "description": "urgent only if the user says so.",
        "default": "normal"
      }
    },
    "text": {"description": "the message to send", "max_length": 200, "required": true}
  },
  "needs_yes": true,
  "timeout_seconds": 20
}
```

| Key | Rule |
|---|---|
| `name` | Required. 1-40 characters, one line. Shown on the dashboard and said in the question ("Shall I run Send note email with: ...?"). |
| `spoken_names` | Required. 1-3 names, each 1-40 letters, digits, spaces, `-`, `'` and `.` (lowercased). No two skills, apps or bookmarks may share one. |
| `description` | Required. 1-500 characters; new lines and tabs allowed, no other control characters. It goes into CARL's tool description word for word, so say clearly what the skill does and when to use it. |
| `inputs` | Optional object with `choices` and/or `text`. |
| `inputs.choices` | Optional object of up to 4 choices: `{"<name>": {"options": [...], "description": "...", "default": "..."}}`. A name is 1-20 of `a-z` and `_`; `text` is reserved. `options`: 2-10 distinct options, each 1-40 of `a-z`, `0-9`, space, `_` and `-`, with no space at either end. `description`: optional, 1-200 characters, one line. `default`: optional, one of the options; a choice without a default is required. |
| `inputs.text` | Optional: the one free-text input. `description` (required, 1-200 characters, one line), `max_length` (1-500, default 300), `required` (default `false`). |
| `needs_yes` | Optional, default `false`. Use `true` for anything that sends, posts, deletes or spends: the robot reads the request back and it runs only after a spoken "yes". Such a skill takes at most 200 characters of text, on one line (all of it is read back; longer text is refused, never cut). |
| `timeout_seconds` | Optional, 1-120, default 30. Past it the script and everything it started are killed. |

### Inputs, output and exit codes

The script gets one JSON object on stdin (UTF-8): every choice (with its default filled in when the user didn't pick one), plus `"text"` when text was given (empty text is left out). Nothing is ever put on the command line.

```json
{"priority": "normal", "text": "I'm running late"}
```

```python
import json
import sys

inputs = json.load(sys.stdin)
print(f"Would send: {inputs['text']}")  # what CARL reads, and sums up
sys.exit(0)
```

- **Exit 0** is success: CARL gets the first 1000 characters of stdout (marked as untrusted data, so it can't steer CARL) and says it in its own words. Print nothing and CARL just gets "Done.". Keep it short: one or two sentences.
- **Any other exit code** is a failure, and CARL is told only "failed (exit N)".
- **stderr** never reaches CARL or the dashboard; on a failure its last 500 characters go to the server log, for you.
- The log and the Live tab show the skill, its choices and the first 60 characters of the text, and "Ran <name>." as the result, never what the script printed. For a skill that needs a yes, the Live tab's `waiting` line shows the request as it is read back.
- One skill runs at a time. A skill that needs a yes runs as soon as you say it, and CARL says its "contemplating" line if it takes more than 4 seconds.

### Packages and secrets

- **No packages:** leave out `pyproject.toml`. The skill runs with CARL's own Python (`python -s main.py`), standard library only, and needs no setup.
- **Packages:** add a `pyproject.toml` (a `[project]` with `name`, `version`, `requires-python` and `dependencies`). Ticking the skill runs `uv sync --no-config --no-editable` in its folder, into `%LOCALAPPDATA%\desk-buddy\skill-envs\<slug>` (never a `.venv` in the folder), and it must leave a `uv.lock` there. A failed setup shows its last 1000 characters on the Apps tab and the skill stays off. It then runs with `uv run --no-sync --offline`, so nothing is installed or downloaded during a question.
- **Secrets:** keep them in the skill's own `.env` (top level of its folder) or the Windows Credential Manager. CARL never reads, hashes, logs or passes that file, and the script starts with a clean environment that holds none of CARL's keys, so **the script loads its own `.env` itself** (see the comments in `examples/skills/send-note-email/main.py`).
- The environment has the usual Windows variables (`PATH`, `TEMP`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and the like), UTF-8 output, and no user site-packages.

### Approving and editing

- New skills appear on the **Apps** tab, unticked. Tick one to approve it: CARL checks it, sets it up if it has packages, and records a fingerprint of every file in the folder (except the top-level `.env`). CARL can use it from the next question.
- **Any edit switches it off.** A change to any covered file (an edit, a `git pull`, a new data file) shows the skill as "changed: re-approve", and CARL won't run it until you tick it again. The files are checked again just before every run, and again when you say "yes".
- At most 20 skills can be on at once.

### Limits and caveats

- **Where to keep `SKILLS_DIR`:** outside any other Python or uv project (a skill with packages inside another project is refused, because uv would install that project's code), and outside OneDrive folders that keep files online-only (those are reparse points, and links of any kind make a skill invalid).
- **At most 200 files and 5 MB** per skill folder, not counting `.venv`, `__pycache__` and dot-folders.
- **What the fingerprint can't do:** it catches edits and updates, but not a live attacker on this PC who swaps files between the check and the run. A skill that executes data files or downloads code is beyond it.
- **Process trees:** at the timeout the whole process tree is killed, but a process that detached itself from the tree may survive.
- **Output** is read into memory until the script ends or its timeout, so don't print megabytes.
- **Restoring files:** if you put a changed skill's files back exactly, it shows as on again, and CARL offers it again the next time it rebuilds its tools (after any change on the dashboard). To bring it back at once, untick it and tick it again (or approve it again through the API: a skill that's already on needs no setup, and CARL looks at the skills again at the next question).
- **Approving trusts the files as they are when you tick.** Edits made while the setup runs are checked again afterwards, but you aren't shown them: look at the folder before ticking.
