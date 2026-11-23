# M0 – Repo Setup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A Windows-friendly monorepo skeleton that a fresh clone can `uv sync`, test and lint — nothing more.

**Architecture:** `server/` is a uv-managed Python 3.12 project with a src layout (single package `desk_buddy`), no runtime deps, dev deps pytest + ruff. `docs/` and `firmware/` get stubs only. Approved design: `docs/plans/2026-11-23-m0-repo-setup-design.md`.

**Tech stack:** uv 0.13.9 (installed via winget), Python 3.12.10, uv_build backend, pytest, ruff.

**Context:** Brief (`CLAUDE.md`) defines M0. Repo currently has `CLAUDE.md`, `.gitignore`, `.claude/agents/python-reviewer.md`, and the design doc. Every commit ends with the `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` line. Once tests exist, run `uv run pytest` before every commit.

---

### Task 1: Repo hygiene + doc/firmware stubs

**Files (create):**
- `.gitattributes` — `* text=auto eol=lf` (plus `*.png binary`, `*.wav binary`)
- `THIRD_PARTY_NOTICES.md` — heading per borrowed file; entry for `.claude/agents/python-reviewer.md` with source URL `https://github.com/affaan-m/everything-claude-code` @ `4adeee6` and the **full MIT license text** (Copyright (c) 2026 Affaan Mustafa), fetched verbatim from upstream `LICENSE` via `gh api`.
- `docs/protocol.md` — "Draft – finalized in M4" + the protocol section copied from `CLAUDE.md`.
- `docs/wiring.md` — "Filled in during M5; must stay in sync with `firmware/include/pins.h`" + the pin constraints (no 35/36/37; avoid 0, 3, 45, 46, 19, 20; 48 = onboard RGB).
- `docs/decisions.md` — header + format note only (entries come in Task 4).
- `firmware/README.md` — "PlatformIO (Arduino) project created in M5. Target: ESP32-S3 N16R8."

**Modify:** `.claude/agents/python-reviewer.md` attribution comment → append "Full license: THIRD_PARTY_NOTICES.md".

**Steps:**
1. Create files above.
2. `git add --renormalize .` (converts existing CRLF working copies to LF in the index).
3. `git status` — expect only the new/modified files.
4. Commit: `Add .gitattributes, third-party notices, and docs/firmware stubs`.

### Task 2: uv project + package skeleton + ruff

**Files (create):**
- `server/.python-version` → `3.12`
- `server/pyproject.toml`:
  ```toml
  [project]
  name = "desk-buddy"
  version = "0.1.0"
  description = "Server brain for the Desk Buddy desk robot"
  readme = "README.md"
  requires-python = ">=3.12"
  dependencies = []

  [build-system]
  requires = ["uv_build>=0.13.9,<0.14"]
  build-backend = "uv_build"

  [tool.pytest.ini_options]
  testpaths = ["tests"]
  addopts = "-ra"

  [tool.ruff]
  line-length = 100
  target-version = "py312"

  [tool.ruff.lint]
  select = ["E", "F", "W", "I", "B", "UP"]
  ```
- `server/README.md` — 5 lines: what it is, `uv sync`, `uv run pytest`, `uv run ruff check`, copy `.env.example` → `.env`.
- `server/src/desk_buddy/__init__.py` → docstring + `__version__ = "0.1.0"`
- `server/src/desk_buddy/brain/__init__.py` → docstring "LLM brain: personality, emotion tags, tools (M2+)."

**Steps:**
1. Create files.
2. `cd server && uv add --dev pytest ruff` — writes `[dependency-groups] dev` and `uv.lock`; creates `.venv` (gitignored).
3. `uv run python -c "import desk_buddy; print(desk_buddy.__version__)"` → `0.1.0`.
4. `uv run ruff check` and `uv run ruff format --check` → clean.
5. Commit `server/` (incl. `uv.lock`): `Create uv project with desk_buddy package and ruff config`.

### Task 3: Smoke tests + .env.example (TDD)

**Files:** Test `server/tests/test_smoke.py`; create `server/.env.example`.

**Step 1 – failing tests:**
```python
"""Smoke tests: the package installs, and .env.example stays secret-free."""

from pathlib import Path

import desk_buddy

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

REQUIRED_KEYS = {
    "ANTHROPIC_API_KEY", "LLM_MODEL",
    "STT_MODEL", "STT_DEVICE", "STT_COMPUTE_TYPE",
    "TTS_ENGINE", "MAX_SENTENCES",
    "OBSIDIAN_VAULT_PATH", "OBSIDIAN_DAILY_FOLDER", "OBSIDIAN_IDEAS_FOLDER",
    "ROBOT_TOKEN", "SERVER_HOST", "SERVER_PORT",
    "COMPUTER_CONTROL_ENABLED",
}


def _parse_env_example() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"Malformed line in .env.example: {line!r}"
        entries[key.strip()] = value.strip()
    return entries


def test_package_has_version():
    assert desk_buddy.__version__


def test_env_example_lists_required_keys():
    missing = REQUIRED_KEYS - _parse_env_example().keys()
    assert not missing, f"Missing from .env.example: {sorted(missing)}"


def test_env_example_values_are_blank():
    filled = {k for k, v in _parse_env_example().items() if v}
    assert not filled, f".env.example must have blank values: {sorted(filled)}"
```
(ruff format will reflow the set literal one-per-line; that's fine.)

**Step 2:** `uv run pytest -v` → version test PASSES, both env tests FAIL (`FileNotFoundError`).

**Step 3 – create `server/.env.example`:** grouped sections, a `#` comment above each key describing it and its default (e.g. `# Default: claude-haiku-5-5`, `# Default: base.en`, `# cpu | cuda (default cpu)`, `# int8 on CPU`, `# sapi | piper`, `# Default: 2`, `# Absolute path to the vault`, `# Default: Daily`, `# Default: Ideas`, `# Shared secret the robot sends in hello`, `# Default: 0.0.0.0` / `8765`, `# false | true — off by default`). Every `KEY=` with nothing after `=`.

**Step 4:** `uv run pytest -v` → 3 passed. `uv run ruff check && uv run ruff format --check` → clean (run `uv run ruff format` first if needed).

**Step 5:** Commit: `Add .env.example and smoke tests`.

### Task 4: decisions.md + CLAUDE.md path update

**Modify `docs/decisions.md`** — entries dated 2026-11-23, each: decision · why · alternatives considered:
1. Server targets Windows, not macOS (dev laptop i7-1355U, 16 GB, no GPU) — alt: Mac Mini.
2. STT faster-whisper int8 on CPU, `base.en` — alt: mlx-whisper (Mac only), openai-whisper (slower).
3. TTS SAPI → Piper — alt: macOS `say`.
4. src layout, single `desk_buddy` package — alt: flat top-level packages.
5. uv installed via winget — alt: astral installer, pip.
6. ruff for lint/format; no mypy yet — alt: mypy now.
7. python-reviewer agent adapted from ECC (MIT) instead of whole ECC plugin — alt: install ECC plugin.

**Modify `CLAUDE.md`:** `server/brain/personality.py` → `server/src/desk_buddy/brain/personality.py`; in Repo layout add `server/src/desk_buddy/` and note "decisions log: docs/decisions.md".

**Steps:** edit → `uv run pytest` (still green) → commit `Record M0 decisions and update brief paths` → `git push`.

---

## Verification (M0 done)

1. Fresh clone into scratchpad: `git clone https://github.com/marcsjourneys-crypto/desk-buddy.git` → `cd desk-buddy/server` → `uv sync` → `uv run pytest` (3 passed) → `uv run ruff check` → `uv run ruff format --check`. All clean.
2. `git ls-files --eol` shows `i/lf` for text files.
3. Run `python-reviewer` agent and `/code-review` on the M0 commits; fix anything real.
4. Plan file copied into `docs/plans/2026-11-23-m0-repo-setup.md` as part of Task 1's commit.
