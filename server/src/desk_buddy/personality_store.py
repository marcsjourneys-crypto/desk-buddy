"""CARL's editable personality, saved in %LOCALAPPDATA%\\desk-buddy\\personality.json.

Only the character paragraph, the signature lines and the About-you profile (`about`:
name, city, region, country, notes; all optional) can be changed; the technical rules
stay in brain/personality.py and are always in the prompt. Anything missing from the
file falls back to the code's default; a bad value in it is skipped with a warning; a
corrupt file is ignored and shown as a banner. Edits apply at the next question (the
greeting and farewell when they are next spoken).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.personality import (
    ABOUT_FIELDS,
    DEFAULT_CHARACTER,
    DEFAULT_LINES,
    LINE_NAMES,
    About,
    Line,
    Personality,
)
from desk_buddy.config import app_data_dir
from desk_buddy.settings_store import write_json_atomic

log = logging.getLogger(__name__)

MAX_CHARACTER = 3000
MAX_LINE = 200
ABOUT_LIMITS: Mapping[str, int] = MappingProxyType(
    {"name": 60, "city": 60, "region": 60, "country": 2, "notes": 1000}
)
assert tuple(ABOUT_LIMITS) == ABOUT_FIELDS


def personality_path() -> Path:
    return app_data_dir() / "personality.json"


def _check_character(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("The character must be text.")
    text = value.strip()
    if not text:
        raise ValueError("The character can't be empty.")
    if len(text) > MAX_CHARACTER:
        raise ValueError(f"The character can be at most {MAX_CHARACTER} characters.")
    if "{" in text or "}" in text:
        raise ValueError("The character can't contain { or }.")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ValueError("The character can't contain control characters.")
    return text


def _check_line(name: str, value: Any, base: Line) -> Line:
    if name not in DEFAULT_LINES:
        raise ValueError(f"There is no line called {name!r}.")
    if not isinstance(value, Mapping):
        raise ValueError("A line needs a text and an emotion.")
    text = value.get("text", base.text)
    emotion = value.get("emotion", base.emotion)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("A line can't be empty.")
    text = text.strip()
    if len(text) > MAX_LINE:
        raise ValueError(f"A line can be at most {MAX_LINE} characters.")
    if any(ord(c) < 32 for c in text):
        raise ValueError("A line must be one line of text.")
    if emotion not in EMOTIONS:
        raise ValueError(f"The emotion must be one of {', '.join(EMOTIONS)}.")
    return Line(emotion, text)


def _check_about(name: str, value: Any) -> str:
    """One About-you field: "" (blank) or the cleaned text. Only `notes` may span lines."""
    if name not in ABOUT_LIMITS:
        raise ValueError(f"There is no About-you field called {name!r}.")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("This must be text.")
    text = value.replace("\r\n", "\n").strip()
    limit = ABOUT_LIMITS[name]
    if name == "country":
        text = text.upper()
        if text and not (len(text) == 2 and text.isascii() and text.isalpha()):
            raise ValueError("The country must be a two-letter code like GB or US, or blank.")
        return text
    if len(text) > limit:
        raise ValueError(f"This can be at most {limit} characters.")
    allowed = "\n\t" if name == "notes" else ""
    if any((ord(c) < 32 or ord(c) == 127) and c not in allowed for c in text):
        if name == "notes":
            raise ValueError("This can't contain control characters.")
        raise ValueError("This must be one line of text.")
    return text


class PersonalityStore:
    """The saved overrides and the personality they make. Thread-safe."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or personality_path()
        self._lock = threading.Lock()
        self._character: str | None = None  # None: the default
        self._lines: dict[str, Line] = {}
        self._about: dict[str, str] = {}  # only the filled-in fields
        self.error: str | None = None
        self._read()

    def current(self) -> Personality:
        with self._lock:
            return self._make(self._character, self._lines, self._about)

    def view(self) -> dict[str, Any]:
        with self._lock:
            p = self._make(self._character, self._lines, self._about)
            custom_lines = set(self._lines)
            custom_character = self._character is not None
        return {
            "about": {name: getattr(p.about, name) for name in ABOUT_FIELDS},
            "character": {
                "value": p.character,
                "default": DEFAULT_CHARACTER,
                "custom": custom_character,
            },
            "lines": {
                name: {
                    "text": p.line(name).text,
                    "emotion": p.line(name).emotion,
                    "custom": name in custom_lines,
                    "default": {
                        "text": DEFAULT_LINES[name].text,
                        "emotion": DEFAULT_LINES[name].emotion,
                    },
                }
                for name in LINE_NAMES
            },
            "emotions": list(EMOTIONS),
            "limits": {
                "character": MAX_CHARACTER,
                "line": MAX_LINE,
                "about": dict(ABOUT_LIMITS),
            },
            "error": self.error,
        }

    def update(self, changes: Mapping[str, Any]) -> tuple[Personality | None, dict[str, str]]:
        """Partial update: {"character": text, "lines": {name: {"text", "emotion"}},
        "about": {field: text}}. A blank About-you field clears it. All or nothing;
        returns the new personality, or None and an error per field."""
        errors: dict[str, str] = {}
        with self._lock:
            character, lines, about = self._character, dict(self._lines), dict(self._about)
            for key, value in changes.items():
                if key == "character":
                    try:
                        character = _check_character(value)
                    except ValueError as exc:
                        errors["character"] = str(exc)
                elif key == "lines":
                    if not isinstance(value, Mapping):
                        errors["lines"] = "Lines must be an object of name: {text, emotion}."
                        continue
                    for name, line in value.items():
                        base = lines.get(name) or DEFAULT_LINES.get(name) or Line("neutral", "")
                        try:
                            lines[name] = _check_line(str(name), line, base)
                        except ValueError as exc:
                            errors[f"lines.{name}"] = str(exc)
                elif key == "about":
                    if not isinstance(value, Mapping):
                        errors["about"] = "About you must be an object of field: text."
                        continue
                    for name, text in value.items():
                        try:
                            checked = _check_about(str(name), text)
                        except ValueError as exc:
                            errors[f"about.{name}"] = str(exc)
                            continue
                        if checked:
                            about[name] = checked
                        else:
                            about.pop(name, None)
                else:
                    errors[str(key)] = "Only the character, the lines and About you can be changed."
            if errors:
                return None, errors
            self._save(character, lines, about)
            return self._make(character, lines, about), {}

    def reset(self, item: str) -> Personality:
        """Back to the default: "character", one line's name, or "about.<field>" (blank)."""
        with self._lock:
            character, lines, about = self._character, dict(self._lines), dict(self._about)
            if item == "character":
                character = None
            elif item in DEFAULT_LINES:
                lines.pop(item, None)
            elif item.startswith("about.") and item[len("about.") :] in ABOUT_LIMITS:
                about.pop(item[len("about.") :], None)
            else:
                raise KeyError(item)
            self._save(character, lines, about)
            return self._make(character, lines, about)

    # --- internals ---

    @staticmethod
    def _make(
        character: str | None, lines: Mapping[str, Line], about: Mapping[str, str]
    ) -> Personality:
        return Personality(
            character=character or DEFAULT_CHARACTER,
            lines={name: lines.get(name, DEFAULT_LINES[name]) for name in LINE_NAMES},
            about=About(**about),
        )

    def _save(
        self, character: str | None, lines: Mapping[str, Line], about: Mapping[str, str]
    ) -> None:
        data: dict[str, Any] = {}
        if character is not None:
            data["character"] = character
        if lines:
            data["lines"] = {n: {"text": ln.text, "emotion": ln.emotion} for n, ln in lines.items()}
        if about:
            data["about"] = {name: about[name] for name in ABOUT_FIELDS if name in about}
        write_json_atomic(self.path, data)
        self._character, self._lines, self._about = character, dict(lines), dict(about)
        self.error = None

    def _read(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._warn(
                f"{self.path.name} couldn't be read ({type(exc).__name__}); using the defaults"
            )
            return
        if not isinstance(raw, dict):
            self._warn(f"{self.path.name} isn't a personality object; using the defaults")
            return
        problems = []
        if "character" in raw:
            try:
                self._character = _check_character(raw["character"])
            except ValueError as exc:
                problems.append(f"character: {exc}")
        lines = raw.get("lines") or {}
        if isinstance(lines, dict):
            for name, line in lines.items():
                try:
                    base = DEFAULT_LINES.get(name) or Line("neutral", "")
                    self._lines[name] = _check_line(str(name), line, base)
                except ValueError as exc:
                    problems.append(f"{name}: {exc}")
        about = raw.get("about") or {}
        if isinstance(about, dict):
            for name, text in about.items():
                try:
                    checked = _check_about(str(name), text)
                except ValueError as exc:
                    problems.append(f"about.{name}: {exc}")
                    continue
                if checked:
                    self._about[str(name)] = checked
        else:
            problems.append("about: not an object")
        if problems:
            self._warn(f"{self.path.name}: skipped {'; '.join(problems)}")

    def _warn(self, message: str) -> None:
        self.error = message
        log.warning("Personality: %s", message)
