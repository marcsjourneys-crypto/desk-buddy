# Desk Buddy server

The Python brain for Desk Buddy (Windows). Managed with [uv](https://docs.astral.sh/uv/).

- Set up: `uv sync`, then copy `.env.example` to `.env` and fill it in.
- Run: `uv run python -m desk_buddy`, then hold Right Ctrl to talk (change the key with `PTT_KEY`).
- Notes: set `OBSIDIAN_VAULT_PATH` in `.env` to an existing folder (Buddy's own vault) to turn on `take_note` / `read_notes`. Daily notes go in `Daily/`, ideas in `Ideas/` (`OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER`). Leave it blank to turn the tools off.
- Test: `uv run pytest`
- Integration tests (real audio, COM, models, network): `uv run pytest -m integration`
- Lint / format: `uv run ruff check` and `uv run ruff format`
