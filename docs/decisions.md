# Decisions

A running log of project decisions. Newest last. When a decision changes, add a new entry (don't rewrite history) and update `CLAUDE.md` in the same commit.

Format: **Decision** — why · alternatives considered.

## 2026-11-23

- **Server targets Windows, not macOS.** The server runs on a Windows 11 PC (current dev machine: i7-1355U laptop, 16 GB RAM, no discrete GPU), so everything must run well on CPU; CUDA is optional. · Alternatives: Mac Mini M4 Pro (original brief).
- **STT: faster-whisper, int8 on CPU, starting with `base.en`.** Fast on CPU via CTranslate2; model, device and compute type configurable. · Alternatives: mlx-whisper (Apple Silicon only), openai-whisper (much slower on CPU).
- **TTS: Windows SAPI voices first, then Piper as the default.** SAPI needs zero setup; Piper is local and runs on Windows. · Alternatives: macOS `say` (Mac only).
- **Computer control (M7) uses Windows mechanisms.** Spotify Web API, Win32 `SendInput` media keys, apps launched by full exe path. · Alternatives: AppleScript / Apple Shortcuts (Mac only).
- **src layout with a single `desk_buddy` package** (`server/src/desk_buddy/`). One import name avoids clashes with generic names like `audio` or `tools`, and tests can't pass against uninstalled files. · Alternatives: flat top-level packages (`server/brain/`), single package without `src/`.
- **uv installed via winget.** Standard Windows package manager; independent of the system Python. · Alternatives: astral.sh PowerShell installer, `pip install uv`.
- **ruff for lint and format; no type checker yet.** One fast dev-only tool. · Alternatives: ruff + mypy now (audio libraries have patchy type stubs, so it would be noisy early).
- **Code review: `python-reviewer` agent adapted from everything-claude-code (MIT), plus `/code-review`.** Gets a focused Python checklist plus Desk Buddy safety checks without the whole ECC plugin's many auto-triggering agents and hooks. Attribution in `THIRD_PARTY_NOTICES.md`. · Alternatives: install the full ECC plugin.
- **Text files stored with LF** (`.gitattributes`). · Alternatives: let Git use CRLF per platform.
