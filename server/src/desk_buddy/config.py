"""Settings loaded from server/.env, overridden by non-blank process environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from desk_buddy.ptt import parse_key

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Keys this module reads. Other keys in .env.example belong to later milestones.
ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "STT_MODEL",
    "STT_DEVICE",
    "STT_COMPUTE_TYPE",
    "TTS_ENGINE",
    "TTS_VOICE",
    "TTS_SPEED",
    "VOICES_DIR",
    "MAX_SENTENCES",
    "HISTORY_TURNS",
    "PTT_KEY",
    "OBSIDIAN_VAULT_PATH",
    "OBSIDIAN_DAILY_FOLDER",
    "OBSIDIAN_IDEAS_FOLDER",
    "ROBOT_TOKEN",
    "SERVER_HOST",
    "SERVER_PORT",
)

# The provider seam: each value maps to one Chat class in llm.create_chat().
# OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3.
LLM_PROVIDERS = ("anthropic",)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STT_DEVICES = ("cpu", "cuda")
TTS_ENGINES = ("sapi", "piper")
MIN_TTS_SPEED, MAX_TTS_SPEED = 0.5, 2.0
# A Piper voice name: <language>_<REGION>-<name>-<quality>, as piper.download_voices
# expects it (e.g. en_GB-northern_english_male-medium).
_PIPER_VOICE_NAME = re.compile(r"^[^-_\s]+_[^-\s]+-[^-\s]+-[^-\s]+$")
MIN_TOKEN_CHARS = 16


def default_voices_dir() -> str:
    """%LOCALAPPDATA%\\desk-buddy\\voices (falls back to the usual place under home)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return str(Path(base) / "desk-buddy" / "voices")


_INLINE_COMMENT = re.compile(r"\s#")


class ConfigError(Exception):
    """A setting is missing or invalid. The message names the key."""


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = field(repr=False)
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "low"
    stt_model: str = "base.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    tts_engine: str = "piper"  # "sapi" is the Windows fallback
    tts_voice: str = "en_GB-northern_english_male-medium"  # Piper name or .onnx path
    tts_speed: float = 1.0
    voices_dir: str = field(default_factory=default_voices_dir)
    max_sentences: int = 2
    history_turns: int = 20
    ptt_key: str = "right_ctrl"
    obsidian_vault_path: str = ""  # blank = note tools off
    obsidian_daily_folder: str = "Daily"
    obsidian_ideas_folder: str = "Ideas"
    robot_token: str = field(default="", repr=False)  # blank = robot link off
    server_host: str = "127.0.0.1"
    server_port: int = 8765


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines.

    Ignores blank lines, comment lines, lines without "=" and lines with an empty key.
    Strips a leading "export " from the key. A value wrapped in matching quotes is
    unquoted and kept as is; an unquoted value is cut at whitespace followed by "#"
    (an inline comment).
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = _INLINE_COMMENT.split(value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_settings(
    env_path: Path | None = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    merged: dict[str, str] = {}
    if env_path is not None and env_path.is_file():
        merged.update(parse_env_file(env_path.read_text(encoding="utf-8-sig")))
    env = os.environ if environ is None else environ
    merged.update({k: env[k] for k in ENV_KEYS if env.get(k, "").strip()})

    def get(key: str, default: str) -> str:
        return merged.get(key, "").strip() or default

    api_key = get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Copy server/.env.example to server/.env and fill it in."
        )

    max_sentences = _positive_int(get, "MAX_SENTENCES", Settings.max_sentences)
    history_turns = _positive_int(get, "HISTORY_TURNS", Settings.history_turns)

    provider = _choice(get, "LLM_PROVIDER", Settings.llm_provider, LLM_PROVIDERS)
    effort = _choice(get, "LLM_EFFORT", Settings.llm_effort, EFFORT_LEVELS)
    device = _choice(get, "STT_DEVICE", Settings.stt_device, STT_DEVICES)
    engine = _choice(get, "TTS_ENGINE", Settings.tts_engine, TTS_ENGINES)

    ptt_key = get("PTT_KEY", Settings.ptt_key)
    try:
        parse_key(ptt_key)
    except ValueError as exc:
        raise ConfigError(f"PTT_KEY: {exc}") from exc

    return Settings(
        anthropic_api_key=api_key,
        llm_provider=provider,
        llm_model=get("LLM_MODEL", Settings.llm_model),
        llm_effort=effort,
        stt_model=get("STT_MODEL", Settings.stt_model),
        stt_device=device,
        stt_compute_type=get("STT_COMPUTE_TYPE", Settings.stt_compute_type),
        tts_engine=engine,
        tts_voice=_tts_voice(get),
        tts_speed=_tts_speed(get),
        voices_dir=get("VOICES_DIR", default_voices_dir()),
        max_sentences=max_sentences,
        history_turns=history_turns,
        ptt_key=ptt_key,
        obsidian_vault_path=_vault_path(get),
        obsidian_daily_folder=_vault_folder(
            get, "OBSIDIAN_DAILY_FOLDER", Settings.obsidian_daily_folder
        ),
        obsidian_ideas_folder=_vault_folder(
            get, "OBSIDIAN_IDEAS_FOLDER", Settings.obsidian_ideas_folder
        ),
        robot_token=_robot_token(get),
        server_host=get("SERVER_HOST", Settings.server_host),
        server_port=_port(get, "SERVER_PORT", Settings.server_port),
    )


def _tts_voice(get: Callable[[str, str], str]) -> str:
    raw = get("TTS_VOICE", Settings.tts_voice)
    if raw.lower().endswith(".onnx"):
        if not Path(raw).is_file():
            raise ConfigError(f"TTS_VOICE is not an existing .onnx file: {raw!r}")
        return raw
    if not _PIPER_VOICE_NAME.match(raw):
        raise ConfigError(
            "TTS_VOICE must be a Piper voice name like en_GB-northern_english_male-medium, "
            f"or a path to an .onnx file; got {raw!r}"
        )
    return raw


def _tts_speed(get: Callable[[str, str], str]) -> float:
    raw = get("TTS_SPEED", str(Settings.tts_speed))
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not MIN_TTS_SPEED <= value <= MAX_TTS_SPEED:  # also rejects nan
        raise ConfigError(
            f"TTS_SPEED must be a number {MIN_TTS_SPEED}-{MAX_TTS_SPEED}, got {raw!r}"
        )
    return value


def _robot_token(get: Callable[[str, str], str]) -> str:
    """Never put the token itself in an error message."""
    token = get("ROBOT_TOKEN", "")
    if token and len(token) < MIN_TOKEN_CHARS:
        raise ConfigError(f"ROBOT_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
    return token


def _port(get: Callable[[str, str], str], key: str, default: int) -> int:
    raw = get(key, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= 65535:
        raise ConfigError(f"{key} must be a port number 1-65535, got {raw!r}")
    return value


def _vault_path(get: Callable[[str, str], str]) -> str:
    raw = get("OBSIDIAN_VAULT_PATH", "")
    if raw and not Path(raw).is_dir():
        raise ConfigError(f"OBSIDIAN_VAULT_PATH is not an existing folder: {raw!r}")
    return raw


def _vault_folder(get: Callable[[str, str], str], key: str, default: str) -> str:
    """A relative folder inside the vault: no drive (C:, C:rel, UNC), no root, no "..".

    Parsed as a Windows path on every OS, so "/" and "\\" both count as separators.
    """
    raw = get(key, default)
    path = PureWindowsPath(raw)
    if path.drive or path.root or ".." in path.parts:
        raise ConfigError(f"{key} must be a folder name inside the vault, got {raw!r}")
    return raw


def _positive_int(get: Callable[[str, str], str], key: str, default: int) -> int:
    raw = get(key, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{key} must be a whole number >= 1, got {raw!r}")
    return value


def _choice(
    get: Callable[[str, str], str], key: str, default: str, allowed: tuple[str, ...]
) -> str:
    raw = get(key, default)
    value = raw.lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}; got {raw!r}")
    return value
