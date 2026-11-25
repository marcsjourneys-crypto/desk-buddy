"""Smoke tests: the package installs, and .env.example stays secret-free."""

import re
from pathlib import Path

import desk_buddy

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

REQUIRED_KEYS = {
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "STT_MODEL",
    "STT_DEVICE",
    "STT_COMPUTE_TYPE",
    "TTS_ENGINE",
    "MAX_SENTENCES",
    "PTT_KEY",
    "OBSIDIAN_VAULT_PATH",
    "OBSIDIAN_DAILY_FOLDER",
    "OBSIDIAN_IDEAS_FOLDER",
    "ROBOT_TOKEN",
    "SERVER_HOST",
    "SERVER_PORT",
    "COMPUTER_CONTROL",
    "SPOTIFY_CLIENT_ID",
    "MUSIC_DUCK",
}


# A commented-out assignment with a value, e.g. "# ANTHROPIC_API_KEY=sk-ant-...".
COMMENTED_ASSIGNMENT = re.compile(r"^#\s*[A-Z][A-Z0-9_]*\s*=\s*\S")


def _parse_env_example() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            assert not COMMENTED_ASSIGNMENT.match(line), (
                f"Commented-out value in .env.example (possible secret): {line.split('=')[0]}="
            )
            continue
        key, sep, value = line.partition("=")
        assert sep, f"Malformed line in .env.example: {line!r}"
        key = key.strip()
        assert key not in entries, f"Duplicate key in .env.example: {key}"
        entries[key] = value.strip()
    return entries


def test_package_has_version():
    assert desk_buddy.__version__


def test_env_example_lists_required_keys():
    missing = REQUIRED_KEYS - _parse_env_example().keys()
    assert not missing, f"Missing from .env.example: {sorted(missing)}"


def test_env_example_values_are_blank():
    filled = {k for k, v in _parse_env_example().items() if v}
    assert not filled, f".env.example must have blank values: {sorted(filled)}"
