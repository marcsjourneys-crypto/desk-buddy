"""Settings edited on the dashboard, saved in %LOCALAPPDATA%\\desk-buddy\\settings.json.

Precedence: the dashboard over .env (and the process environment) over the defaults.
Only non-secret settings are editable (config.DASHBOARD_KEYS); secrets, hosts and ports
stay in .env. Every change is validated by config's own rules (by loading the settings
with it), saved atomically, and applied live where it can be:

- settings that shape a turn apply at the next question (LiveSettings.apply, called when
  a turn starts);
- the TTS speed applies at the next sentence;
- the rest (speech recognition, the key, the voice) after a restart.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desk_buddy.config import (
    DASHBOARD_KEYS,
    DEFAULT_ENV_PATH,
    EFFORT_LEVELS,
    STT_DEVICES,
    ConfigError,
    Settings,
    dashboard_settings_path,
    load_settings,
    read_dashboard_values,
    read_env,
)

log = logging.getLogger(__name__)

MAX_VALUE = 300  # characters of any one setting

NEXT_QUESTION = "next question"
NEXT_SENTENCE = "next sentence"
AFTER_RESTART = "after restart"
AT_ONCE = "at once"
NEXT_WAKE = "next wake"


@dataclass(frozen=True)
class Editable:
    key: str
    group: str
    applies: str
    kind: str  # switch, int, float, choice, text, path
    label: str
    choices: tuple[str, ...] = ()

    @property
    def attr(self) -> str:
        return self.key.lower()


EDITABLE: tuple[Editable, ...] = (
    Editable("COMPUTER_CONTROL", "Computer control", NEXT_QUESTION, "switch", "Computer control"),
    Editable(
        "MUSIC_DUCK", "Computer control", NEXT_QUESTION, "switch", "Lower music while CARL talks"
    ),
    Editable("MAX_SENTENCES", "Conversation", NEXT_QUESTION, "int", "Max sentences per reply"),
    Editable("HISTORY_TURNS", "Conversation", NEXT_QUESTION, "int", "Exchanges remembered"),
    Editable("TTS_SPEED", "Voice", NEXT_SENTENCE, "float", "Speaking speed (0.5-2.0)"),
    Editable("TTS_VOICE", "Voice", AFTER_RESTART, "text", "Piper voice"),
    Editable("OBSIDIAN_VAULT_PATH", "Notes", NEXT_QUESTION, "path", "Vault folder (blank: off)"),
    Editable("OBSIDIAN_DAILY_FOLDER", "Notes", NEXT_QUESTION, "text", "Daily notes folder"),
    Editable("OBSIDIAN_IDEAS_FOLDER", "Notes", NEXT_QUESTION, "text", "Ideas folder"),
    Editable("LLM_MODEL", "Brain", NEXT_QUESTION, "text", "Claude model"),
    Editable("LLM_EFFORT", "Brain", NEXT_QUESTION, "choice", "Thinking effort", EFFORT_LEVELS),
    Editable("WEB_SEARCH", "Web", NEXT_QUESTION, "switch", "Web search"),
    Editable("WEB_SEARCH_MAX", "Web", NEXT_QUESTION, "int", "Searches per question (1-5)"),
    Editable("SKILLS_DIR", "Skills", NEXT_QUESTION, "path", "Skills folder (blank: off)"),
    Editable("WAKE_WORD", "Wake word", AT_ONCE, "switch", 'Listen for "Hey CARL"'),
    Editable(
        "WAKE_THRESHOLD",
        "Wake word",
        AT_ONCE,
        "float",
        "Detection threshold (0.50-0.99; blank: the robot's own, after it restarts)",
    ),
    Editable(
        "FOLLOW_UP_SECONDS",
        "Wake word",
        NEXT_WAKE,
        "float",
        "Follow-up listening, seconds (0-15; 0: off)",
    ),
    Editable(
        "WAKE_END_SILENCE",
        "Wake word",
        NEXT_WAKE,
        "float",
        "Silence that ends a question, seconds (0.4-2.0)",
    ),
    Editable("STT_MODEL", "Speech recognition and keys", AFTER_RESTART, "text", "Whisper model"),
    Editable(
        "STT_DEVICE", "Speech recognition and keys", AFTER_RESTART, "choice", "Device", STT_DEVICES
    ),
    Editable(
        "STT_COMPUTE_TYPE", "Speech recognition and keys", AFTER_RESTART, "text", "Compute type"
    ),
    Editable("PTT_KEY", "Speech recognition and keys", AFTER_RESTART, "text", "Talk key"),
)
_BY_KEY = {e.key: e for e in EDITABLE}
assert set(_BY_KEY) == DASHBOARD_KEYS, "EDITABLE and config.DASHBOARD_KEYS must match"


def _as_text(value: Any) -> str:
    """A setting from the page as .env-style text. Raises ValueError if it isn't one."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if len(text) > MAX_VALUE:
            raise ValueError(f"must be at most {MAX_VALUE} characters")
        if any(ord(c) < 32 for c in text):
            raise ValueError("must be one line of text")
        return text
    raise ValueError("must be text, a number or on/off")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to a temp file next to `path`, then swap it in: a crash never leaves a
    half-written file, and a failed save leaves the old one intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SettingsStore:
    """The dashboard's saved values, with the .env they sit on. Thread-safe."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        env_path: Path | None = DEFAULT_ENV_PATH,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path or dashboard_settings_path()
        self.env_path = env_path
        self._environ = environ
        self._lock = threading.Lock()
        self.values, self.error = read_dashboard_values(self.path)
        if self.error:
            log.warning("Dashboard settings: %s", self.error)

    def settings(self) -> Settings:
        """The settings with the saved values that still validate (as at startup)."""
        with self._lock:
            return self._load(self._good(self.values)[0])

    def view(self) -> list[dict[str, Any]]:
        """Every editable setting: its value, where it comes from and when it applies.
        Never a secret: only editable keys are in it. A saved value that no longer
        validates shows source "invalid" with why, and the value CARL falls back to."""
        with self._lock:
            values = dict(self.values)
        good, bad = self._good(values)
        settings = self._load(good)
        env = read_env(self.env_path, self._environ)
        out = []
        for item in EDITABLE:
            if item.key in bad:
                source = "invalid"
            elif item.key in good:
                source = "dashboard"
            elif env.get(item.key, "").strip():
                source = "env"
            else:
                source = "default"
            entry: dict[str, Any] = {
                "key": item.key,
                "label": item.label,
                "group": item.group,
                "kind": item.kind,
                "choices": list(item.choices),
                "value": getattr(settings, item.attr),
                "source": source,
                "applies": item.applies,
            }
            if item.key in bad:
                entry["error"] = bad[item.key]
            out.append(entry)
        return out

    def update(self, changes: Mapping[str, Any]) -> tuple[Settings | None, dict[str, str]]:
        """Validate every change (all or nothing) against the saved values that are still
        good, then save. Returns the new settings, or None and an error per field. A bad
        saved value never blocks other edits; it stays in the file until fixed or reset."""
        with self._lock:
            good, _ = self._good(self.values)
            errors: dict[str, str] = {}
            accepted: dict[str, str] = {}
            for key, value in changes.items():
                if key not in _BY_KEY:
                    errors[key] = "This setting can't be changed here."
                    continue
                try:
                    text = _as_text(value)
                    self._load({**good, key: text})
                except (ValueError, ConfigError) as exc:
                    errors[key] = str(exc)
                    continue
                accepted[key] = text
            if not errors:
                try:
                    settings = self._load({**good, **accepted})
                except ConfigError as exc:  # each was fine alone, not together
                    errors = {key: str(exc) for key in changes}
            if errors:
                return None, errors
            new = {**self.values, **accepted}
            write_json_atomic(self.path, new)
            self.values, self.error = new, None
            return settings, {}

    def reset(self, key: str) -> Settings:
        """Drop the dashboard's value: back to .env, or the default."""
        if key not in _BY_KEY:
            raise KeyError(key)
        with self._lock:
            new = {k: v for k, v in self.values.items() if k != key}
            settings = self._load(self._good(new)[0])
            write_json_atomic(self.path, new)
            self.values, self.error = new, None
            return settings

    def _load(self, values: Mapping[str, str]) -> Settings:
        return load_settings(self.env_path, self._environ, dashboard=values)

    def _good(self, values: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """Split saved values into those that validate and those that don't (with why),
        the way startup skips bad ones."""
        try:
            self._load(values)
            return dict(values), {}
        except ConfigError:
            pass
        good: dict[str, str] = {}
        bad: dict[str, str] = {}
        for key, value in values.items():
            try:
                self._load({key: value})
                good[key] = value
            except ConfigError as exc:
                bad[key] = str(exc)
        return good, bad


Hook = Callable[[Settings, Settings], None]


class LiveSettings:
    """The settings CARL runs with, and changes waiting for the next question.

    propose() runs the "now" hooks at once (TTS speed); apply(), called on the event loop
    when a turn starts, swaps the settings in and runs the "question" hooks. A failing
    hook is logged and never raises into the voice loop."""

    def __init__(self, settings: Settings) -> None:
        self._current = settings
        self._pending: Settings | None = None
        self._now_hooks: list[Hook] = []
        self._question_hooks: list[Hook] = []
        self._lock = threading.Lock()

    @property
    def current(self) -> Settings:
        return self._current

    @property
    def latest(self) -> Settings:
        """The newest settings, including a change still waiting for the next question
        (for the dashboard, which shows what was just saved)."""
        with self._lock:
            return self._pending or self._current

    def on_now(self, hook: Hook) -> None:
        self._now_hooks.append(hook)

    def on_question(self, hook: Hook) -> None:
        self._question_hooks.append(hook)

    def propose(self, settings: Settings) -> None:
        with self._lock:
            old = self._pending or self._current
            self._pending = settings
        self._run(self._now_hooks, old, settings)

    def apply(self) -> None:
        with self._lock:
            new, self._pending = self._pending, None
            if new is None:
                return
            old, self._current = self._current, new
        self._run(self._question_hooks, old, new)

    @staticmethod
    def _run(hooks: list[Hook], old: Settings, new: Settings) -> None:
        for hook in hooks:
            try:
                hook(old, new)
            except Exception:
                log.warning("Live setting hook failed", exc_info=True)
