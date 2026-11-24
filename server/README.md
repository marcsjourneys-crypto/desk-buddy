# Desk Buddy server

The Python brain for Desk Buddy (Windows). Managed with [uv](https://docs.astral.sh/uv/).

- Set up: `uv sync`, then copy `.env.example` to `.env` and fill it in.
- Run: `uv run python -m desk_buddy`, then hold Right Ctrl to talk (change the key with `PTT_KEY`).
- Notes: set `OBSIDIAN_VAULT_PATH` in `.env` to an existing folder (Buddy's own vault) to turn on `take_note` / `read_notes`. Daily notes go in `Daily/`, ideas in `Ideas/` (`OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER`). Leave it blank to turn the tools off.
- Robot link: set `ROBOT_TOKEN` in `.env` to turn on the robot WebSocket (`ws://127.0.0.1:8765/robot`; change it with `SERVER_HOST` / `SERVER_PORT`). Generate a token with `uv run python -c "import secrets; print(secrets.token_urlsafe(24))"`. Leave it blank to turn the link off. Protocol: `docs/protocol.md`.
- Fake robot: with Buddy running, start `uv run python -m desk_buddy.fake_robot` in a second terminal. It connects with the same `.env`, then uses the PC mic, speaker and Right Ctrl as the robot's.
- Test: `uv run pytest`
- Integration tests (real audio, COM, models, network): `uv run pytest -m integration`
- Lint / format: `uv run ruff check` and `uv run ruff format`
