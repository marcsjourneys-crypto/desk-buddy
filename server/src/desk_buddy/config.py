"""Settings loaded from server/.env, overridden by non-blank process environment variables."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from desk_buddy.ptt import parse_key

log = logging.getLogger(__name__)

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
    "COMPUTER_CONTROL",
    "SPOTIFY_CLIENT_ID",
    "MUSIC_DUCK",
    "DASHBOARD_PORT",
    "WEB_SEARCH",
    "WEB_SEARCH_MAX",
    "YOUTUBE_API_KEY",
    "SKILLS_DIR",
    "WAKE_WORD",
    "WAKE_THRESHOLD",
    "FOLLOW_UP_SECONDS",
    "WAKE_END_SILENCE",
)

# The keys the dashboard may set (never secrets, hosts or ports). See settings_store.
DASHBOARD_KEYS = frozenset(
    {
        "COMPUTER_CONTROL",
        "MUSIC_DUCK",
        "MAX_SENTENCES",
        "HISTORY_TURNS",
        "TTS_SPEED",
        "TTS_VOICE",
        "OBSIDIAN_VAULT_PATH",
        "OBSIDIAN_DAILY_FOLDER",
        "OBSIDIAN_IDEAS_FOLDER",
        "LLM_MODEL",
        "LLM_EFFORT",
        "STT_MODEL",
        "STT_DEVICE",
        "STT_COMPUTE_TYPE",
        "PTT_KEY",
        "WEB_SEARCH",
        "WEB_SEARCH_MAX",
        "SKILLS_DIR",
        "WAKE_WORD",
        "WAKE_THRESHOLD",
        "FOLLOW_UP_SECONDS",
        "WAKE_END_SILENCE",
    }
)

# The provider seam: each value maps to one Chat class in llm.create_chat().
# OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3.
LLM_PROVIDERS = ("anthropic",)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STT_DEVICES = ("cpu", "cuda")
STT_COMPUTE_TYPES = ("default", "int8", "int8_float16", "int16", "float16", "float32")
_STT_MODEL = re.compile(r"[A-Za-z0-9._/-]+")
_LLM_MODEL = re.compile(r"[\w.:-]+")
MAX_SENTENCES = 10
MAX_HISTORY = 50
MAX_WEB_SEARCHES = 5  # per question
TTS_ENGINES = ("sapi", "piper")
MIN_TTS_SPEED, MAX_TTS_SPEED = 0.5, 2.0
# A Piper voice name: <language>_<REGION>-<name>-<quality>, e.g.
# en_GB-northern_english_male-medium. ASCII letters, digits and "_" only, so a name can
# never be a path ("/", "\", ".."). Use with fullmatch.
PIPER_VOICE_NAME = re.compile(r"[A-Za-z]+_[A-Za-z]+-[A-Za-z0-9_]+-[A-Za-z0-9_]+", re.ASCII)
MIN_TOKEN_CHARS = 16
# The wake word (on the robot) and hands-free listening (on the server).
MIN_WAKE_THRESHOLD, MAX_WAKE_THRESHOLD = 0.5, 0.99  # as protocol.wake_config
MAX_FOLLOW_UP_SECONDS = 15.0
MIN_END_SILENCE, MAX_END_SILENCE = 0.4, 2.0


def app_data_dir() -> Path:
    """%LOCALAPPDATA%\\desk-buddy (falls back to the usual place under home): downloads and
    logins that don't belong in the repo."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "desk-buddy"


def default_voices_dir() -> str:
    """%LOCALAPPDATA%\\desk-buddy\\voices."""
    return str(app_data_dir() / "voices")


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
    computer_control: bool = False  # off by default: COMPUTER_CONTROL=on enables it
    spotify_client_id: str = ""  # blank = music tools off
    music_duck: bool = True  # lower Spotify while CARL listens and talks
    dashboard_port: int = 8766  # the dashboard, on 127.0.0.1 only
    web_search: bool = True  # Anthropic's web search tool (runs on Anthropic's side)
    web_search_max: int = 2  # searches per question, 1-MAX_WEB_SEARCHES
    # A secret (never on the dashboard, never logged). Blank = play_youtube off.
    youtube_api_key: str = field(default="", repr=False)
    skills_dir: str = ""  # the user's skills (skills_store); blank = skills off
    wake_word: bool = True  # off: the robot doesn't listen for it, wakes are ignored
    wake_threshold: float | None = None  # blank: the robot's own (NVS) threshold
    follow_up_seconds: float = 5.0  # listening after a hands-free reply; 0 = off
    wake_end_silence: float = 0.8  # silence that ends a hands-free question


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


def read_env(
    env_path: Path | None = DEFAULT_ENV_PATH, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The raw values from .env, overridden by non-blank process environment variables."""
    merged: dict[str, str] = {}
    if env_path is not None and env_path.is_file():
        merged.update(parse_env_file(env_path.read_text(encoding="utf-8-sig")))
    env = os.environ if environ is None else environ
    merged.update({k: env[k] for k in ENV_KEYS if env.get(k, "").strip()})
    return merged


def dashboard_settings_path() -> Path:
    return app_data_dir() / "settings.json"


def read_dashboard_values(path: Path) -> tuple[dict[str, str], str | None]:
    """The dashboard's saved settings ({KEY: text}, editable keys only) and, if the file
    couldn't be used, why. A missing file is fine; a corrupt one is ignored."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, f"{path.name} couldn't be read ({type(exc).__name__}); using .env and defaults"
    if not isinstance(raw, dict):
        return {}, f"{path.name} isn't a settings object; using .env and defaults"
    values = {
        k: str(v)
        for k, v in raw.items()
        if k in DASHBOARD_KEYS and isinstance(v, (str, int, float, bool))
    }
    return values, None


def load_settings(
    env_path: Path | None = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
    dashboard: Mapping[str, str] | None = None,
) -> Settings:
    """Settings: the dashboard over .env (and the process environment) over defaults.

    Only DASHBOARD_KEYS can come from the dashboard; a blank one means the default. With
    `dashboard` left out, the real startup (default .env, real environment) reads the
    dashboard's file and skips, with a warning, any value of it that no longer validates:
    a dashboard setting must never stop CARL from starting. Any other call (tests, tools)
    leaves the file alone."""
    if dashboard is None:
        if env_path == DEFAULT_ENV_PATH and environ is None:
            values, error = read_dashboard_values(dashboard_settings_path())
            if error:
                log.warning("Dashboard settings: %s", error)
            return _load_skipping_bad(env_path, environ, values)
        dashboard = {}
    return _load(env_path, environ, dashboard)


def _load_skipping_bad(
    env_path: Path | None, environ: Mapping[str, str] | None, dashboard: Mapping[str, str]
) -> Settings:
    try:
        return _load(env_path, environ, dashboard)
    except ConfigError:
        if not dashboard:
            raise
    bad = []
    for key, value in dashboard.items():
        try:
            _load(env_path, environ, {key: value})
        except ConfigError as exc:
            log.warning("Dashboard setting %s ignored: %s", key, exc)
            bad.append(key)
    return _load(env_path, environ, {k: v for k, v in dashboard.items() if k not in bad})


def _load(
    env_path: Path | None, environ: Mapping[str, str] | None, dashboard: Mapping[str, str]
) -> Settings:
    merged = read_env(env_path, environ)
    merged.update({k: str(v) for k, v in dashboard.items() if k in DASHBOARD_KEYS})

    def get(key: str, default: str) -> str:
        return merged.get(key, "").strip() or default

    api_key = get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Copy server/.env.example to server/.env and fill it in."
        )

    max_sentences = _int_between(get, "MAX_SENTENCES", Settings.max_sentences, 1, MAX_SENTENCES)
    history_turns = _int_between(get, "HISTORY_TURNS", Settings.history_turns, 1, MAX_HISTORY)

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
        llm_model=_matching(get, "LLM_MODEL", Settings.llm_model, _LLM_MODEL),
        llm_effort=effort,
        stt_model=_stt_model(get),
        stt_device=device,
        stt_compute_type=_choice(
            get, "STT_COMPUTE_TYPE", Settings.stt_compute_type, STT_COMPUTE_TYPES
        ),
        tts_engine=engine,
        # Checked only for Piper, so TTS_ENGINE=sapi always works as the fallback.
        tts_voice=_tts_voice(get) if engine == "piper" else Settings.tts_voice,
        tts_speed=_tts_speed(get) if engine == "piper" else Settings.tts_speed,
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
        computer_control=_switch(get, "COMPUTER_CONTROL", Settings.computer_control),
        spotify_client_id=get("SPOTIFY_CLIENT_ID", ""),
        music_duck=_switch(get, "MUSIC_DUCK", Settings.music_duck),
        dashboard_port=_port(get, "DASHBOARD_PORT", Settings.dashboard_port),
        web_search=_switch(get, "WEB_SEARCH", Settings.web_search),
        web_search_max=_int_between(
            get, "WEB_SEARCH_MAX", Settings.web_search_max, 1, MAX_WEB_SEARCHES
        ),
        youtube_api_key=get("YOUTUBE_API_KEY", ""),
        skills_dir=_own_folder(get, "SKILLS_DIR"),
        wake_word=_switch(get, "WAKE_WORD", Settings.wake_word),
        wake_threshold=_wake_threshold(get),
        follow_up_seconds=_float_between(
            get, "FOLLOW_UP_SECONDS", Settings.follow_up_seconds, 0.0, MAX_FOLLOW_UP_SECONDS
        ),
        wake_end_silence=_float_between(
            get, "WAKE_END_SILENCE", Settings.wake_end_silence, MIN_END_SILENCE, MAX_END_SILENCE
        ),
    )


_ON = ("on", "true", "yes", "1")
_OFF = ("off", "false", "no", "0")


def _switch(get: Callable[[str, str], str], key: str, default: bool) -> bool:
    raw = get(key, "on" if default else "off")
    value = raw.lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise ConfigError(f"{key} must be on or off, got {raw!r}")


def _tts_voice(get: Callable[[str, str], str]) -> str:
    raw = get("TTS_VOICE", Settings.tts_voice)
    if raw.lower().endswith(".onnx"):
        if not Path(raw).is_file():
            raise ConfigError(f"TTS_VOICE is not an existing .onnx file: {raw!r}")
        return raw
    if not PIPER_VOICE_NAME.fullmatch(raw):
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


def _float_between(
    get: Callable[[str, str], str], key: str, default: float, low: float, high: float
) -> float:
    raw = get(key, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not low <= value <= high:  # also rejects nan and infinities
        raise ConfigError(f"{key} must be a number {low:g}-{high:g}, got {raw!r}")
    return value


def _wake_threshold(get: Callable[[str, str], str]) -> float | None:
    """Blank: None (the robot keeps its own threshold)."""
    if not get("WAKE_THRESHOLD", ""):
        return None
    return _float_between(get, "WAKE_THRESHOLD", 0.0, MIN_WAKE_THRESHOLD, MAX_WAKE_THRESHOLD)


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
    return _own_folder(get, "OBSIDIAN_VAULT_PATH")


def _own_folder(get: Callable[[str, str], str], key: str) -> str:
    """Blank (off), or an existing folder of its own: not a drive or the home folder."""
    raw = get(key, "")
    if not raw:
        return raw
    if not Path(raw).is_dir():
        raise ConfigError(f"{key} is not an existing folder: {raw!r}")
    path = Path(raw).resolve()
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ConfigError(f"{key} must be a folder of its own, not a drive or your home folder")
    return raw


def _int_between(
    get: Callable[[str, str], str], key: str, default: int, low: int, high: int
) -> int:
    raw = get(key, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise ConfigError(f"{key} must be a whole number {low}-{high}, got {raw!r}")
    return value


def _matching(
    get: Callable[[str, str], str], key: str, default: str, pattern: re.Pattern[str]
) -> str:
    raw = get(key, default)
    if not pattern.fullmatch(raw):
        raise ConfigError(f"{key} doesn't look like a model name: {raw!r}")
    return raw


def _stt_model(get: Callable[[str, str], str]) -> str:
    """A Whisper size (base.en) or a Hugging Face repo id (Systran/faster-whisper-small);
    never a path that climbs out with ".."."""
    raw = get("STT_MODEL", Settings.stt_model)
    if not _STT_MODEL.fullmatch(raw) or ".." in raw:
        raise ConfigError(f"STT_MODEL doesn't look like a Whisper model name: {raw!r}")
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


def _choice(
    get: Callable[[str, str], str], key: str, default: str, allowed: tuple[str, ...]
) -> str:
    raw = get(key, default)
    value = raw.lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}; got {raw!r}")
    return value
