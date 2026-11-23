# M0 – Repo setup: design

Date: 2026-11-23 · Status: approved

## Goal

A monorepo skeleton that a fresh clone can sync, test and lint on Windows with uv — and nothing more. No runtime dependencies, no config loader, no audio code; those arrive in M1 when we know what they need to be.

## Decisions made while brainstorming

| Topic | Decision | Why |
|---|---|---|
| uv install | `winget install astral-sh.uv` | Standard Windows package manager; independent of system Python |
| Package layout | src layout, single package `desk_buddy` (`server/src/desk_buddy/...`) | One import name, no clashes with generic names (`audio`, `tools`); tests can't pass against uninstalled files |
| Lint/format | ruff (dev-only) | One fast tool; `python-reviewer` already uses it |
| Type checking | none for now | Audio libraries have patchy stubs; revisit later |
| Firmware | README only | PlatformIO project created in M5 |

## Layout

```
CLAUDE.md                 path updated → server/src/desk_buddy/…
THIRD_PARTY_NOTICES.md    full MIT text for .claude/agents/python-reviewer.md
.gitattributes            normalize text files to LF in the repo
server/
  pyproject.toml          Python >=3.12, no runtime deps; dev deps pytest, ruff
  uv.lock                 committed
  .python-version         3.12
  .env.example            every known key, values blank, one-line comment each
  src/desk_buddy/__init__.py        __version__
  src/desk_buddy/brain/__init__.py  placeholder for M2 personality.py
  tests/test_smoke.py
firmware/README.md
docs/decisions.md         dated entries: decision · why · alternatives
docs/protocol.md          stub with the draft from CLAUDE.md (finalized M4)
docs/wiring.md            stub (filled in M5 with pins.h)
```

`.env.example` keys: `ANTHROPIC_API_KEY`, `LLM_MODEL`, `STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `TTS_ENGINE`, `MAX_SENTENCES`, `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_DAILY_FOLDER`, `OBSIDIAN_IDEAS_FOLDER`, `ROBOT_TOKEN`, `SERVER_HOST`, `SERVER_PORT`, `COMPUTER_CONTROL_ENABLED`. Spotify keys are added in M7.

## Testing

`test_smoke.py`:
1. `import desk_buddy` works and exposes `__version__`.
2. `.env.example` lists every required key and every value is blank (enforces the "no secrets in the example" rule).

## Done when

From a fresh clone, inside `server/`: `uv sync`, `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` all pass. Then `python-reviewer` and `/code-review` run clean on the result.

## Commits

1. This design doc.
2. `.gitattributes`, `THIRD_PARTY_NOTICES.md`, docs and firmware stubs.
3. uv project, package skeleton, ruff config.
4. `.env.example` + smoke tests.
5. `decisions.md` entries + `CLAUDE.md` path update.
