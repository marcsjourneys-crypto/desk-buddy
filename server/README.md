# Desk Buddy server

The Python brain for Desk Buddy (Windows). Managed with [uv](https://docs.astral.sh/uv/).

- Set up: `uv sync`, then copy `.env.example` to `.env` and fill it in.
- Run: `uv run python -m desk_buddy`, then hold Right Ctrl to talk (change the key with `PTT_KEY`).
- Test: `uv run pytest`
- Integration tests (real audio, COM, models, network): `uv run pytest -m integration`
- Lint / format: `uv run ruff check` and `uv run ruff format`
