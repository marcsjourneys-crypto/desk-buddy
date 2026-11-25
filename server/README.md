# Desk Buddy server

The Python brain for Desk Buddy (Windows). Managed with [uv](https://docs.astral.sh/uv/).

- Set up: `uv sync`, then copy `.env.example` to `.env` and fill it in.
- Run: `uv run python -m desk_buddy`, then hold Right Ctrl to talk (change the key with `PTT_KEY`). The character is CARL-42 (CARL): it greets you at startup and says goodbye on Ctrl+C. Its personality and lines are in `src/desk_buddy/brain/personality.py`.
- Voice: CARL speaks with Piper's `en_GB-northern_english_male-medium` by default. On the first run it is downloaded (about 60 MB) into `%LOCALAPPDATA%\desk-buddy\voices` (`VOICES_DIR`). Change it with `TTS_VOICE` (another Piper voice name, or a path to an `.onnx` file with its `.onnx.json` next to it) and the pace with `TTS_SPEED` (0.5–2.0). `TTS_ENGINE=sapi` uses the built-in Windows voice instead (no download). The startup log shows `Voice: piper <name>` or `Voice: sapi`.
- Notes: set `OBSIDIAN_VAULT_PATH` in `.env` to an existing folder (CARL's own vault) to turn on `take_note` / `read_notes`. Daily notes go in `Daily/`, ideas in `Ideas/` (`OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER`). Leave it blank to turn the tools off.
- Music (Spotify Premium): CARL can play, pause, skip, change the music volume and say what's playing. Set it up once:
  1. At developer.spotify.com, open the Dashboard and create an app (pick "Web API") with the redirect URI `http://127.0.0.1:8888/callback`. Copy its Client ID.
  2. In `.env`: `SPOTIFY_CLIENT_ID=<id>` and `COMPUTER_CONTROL=on`.
  3. Run `uv run python -m desk_buddy.spotify_login` and click Agree in the browser. The login is saved in `%LOCALAPPDATA%\desk-buddy\spotify_token.json` (never print or share it).

  The startup log says `Music tools: on (Spotify, ducking)` or why they're off. At most 3 actions per question and 8 a minute; each is logged as `Action: …`. If Spotify isn't running, CARL opens it. `MUSIC_DUCK=off` stops CARL lowering the music while it listens and talks.
- Robot link: set `ROBOT_TOKEN` in `.env` to turn on the robot WebSocket (`ws://127.0.0.1:8765/robot`; change it with `SERVER_HOST` / `SERVER_PORT`). Generate a token with `uv run python -c "import secrets; print(secrets.token_urlsafe(24))"`. Leave it blank to turn the link off. Protocol: `docs/protocol.md`.
- Real robot (M6): set `SERVER_HOST=0.0.0.0` in `.env` and add the Private-only Windows Firewall rule for the port, once, as administrator (PowerShell and cmd versions in `docs/protocol.md`). At startup CARL logs the PC's LAN address and the exact robot command, e.g. `Robot link: on (ws://192.168.1.20:8765/robot — set the robot with: server 192.168.1.20:8765)`. Type that `server …` line into the robot's serial menu (`firmware/README.md`). While the robot is connected, hold Right Ctrl in the CARL window to talk through the robot.
- Fake robot: with CARL running, start `uv run python -m desk_buddy.fake_robot` in a second terminal. It connects with the same `.env` and acts like the real robot: it streams the PC mic while CARL is listening and plays replies on the PC speaker. Talk by holding Right Ctrl in the CARL window (not the fake robot's).
- Test: `uv run pytest`
- Integration tests (real audio, COM, models, network): `uv run pytest -m integration`
- Lint / format: `uv run ruff check` and `uv run ruff format`
