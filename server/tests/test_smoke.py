"""Smoke tests: the package installs, and .env.example stays secret-free."""

from pathlib import Path

import desk_buddy

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

REQUIRED_KEYS = {
    "ANTHROPIC_API_KEY",
    "LLM_MODEL",
    "STT_MODEL",
    "STT_DEVICE",
    "STT_COMPUTE_TYPE",
    "TTS_ENGINE",
    "MAX_SENTENCES",
    "OBSIDIAN_VAULT_PATH",
    "OBSIDIAN_DAILY_FOLDER",
    "OBSIDIAN_IDEAS_FOLDER",
    "ROBOT_TOKEN",
    "SERVER_HOST",
    "SERVER_PORT",
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
