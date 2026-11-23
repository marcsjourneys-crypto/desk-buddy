"""Settings loaded from server/.env, overridden by non-blank process environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    "MAX_SENTENCES",
    "PTT_KEY",
)

# The provider seam: each value maps to one Chat class in llm.create_chat().
# OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3.
LLM_PROVIDERS = ("anthropic",)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STT_DEVICES = ("cpu", "cuda")
TTS_ENGINES = ("sapi",)  # "piper" arrives later behind the same interface


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
    tts_engine: str = "sapi"
    max_sentences: int = 2
    ptt_key: str = "right_ctrl"


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

    raw_sentences = get("MAX_SENTENCES", str(Settings.max_sentences))
    try:
        max_sentences = int(raw_sentences)
    except ValueError:
        raise ConfigError(
            f"MAX_SENTENCES must be a whole number >= 1, got {raw_sentences!r}"
        ) from None
    if max_sentences < 1:
        raise ConfigError(f"MAX_SENTENCES must be a whole number >= 1, got {raw_sentences!r}")

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
        max_sentences=max_sentences,
        ptt_key=ptt_key,
    )


def _choice(
    get: Callable[[str, str], str], key: str, default: str, allowed: tuple[str, ...]
) -> str:
    raw = get(key, default)
    value = raw.lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}; got {raw!r}")
    return value
