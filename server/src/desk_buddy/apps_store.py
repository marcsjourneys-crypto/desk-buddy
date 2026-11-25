"""The apps and websites CARL may open, chosen on the dashboard's Apps tab.

- Apps: %LOCALAPPDATA%\\desk-buddy\\apps.json, [{"app_id", "title", "names": [...]}].
  Every app ID must come from Windows' own list (Get-StartApps, run only when the page
  asks): the store keeps the last listing and refuses IDs that weren't in it (or already
  saved).
- Bookmarks: %LOCALAPPDATA%\\desk-buddy\\bookmarks.json, [{"name", "url"}].

Spoken names are 1-40 letters, digits, spaces, -, ' and ., stored lowercased and unique
within their list. Saves are atomic and all or nothing; a corrupt file is ignored with a
warning and shown as a banner, a bad entry in it is skipped. Changes reach CARL at the
next question (the chat is rebuilt when `version` changes).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desk_buddy.config import app_data_dir
from desk_buddy.settings_store import write_json_atomic
from desk_buddy.tools.urls import MAX_URL, check_url

log = logging.getLogger(__name__)

MAX_APPS = 50
MAX_BOOKMARKS = 50
MAX_NAMES = 3  # spoken names per app
MAX_NAME = 40
MAX_APP_ID = 300
MAX_TITLE = 100
LIST_TIMEOUT_S = 30.0
NAME_PUNCTUATION = frozenset(" -'.")
DIGITS = frozenset("0123456789")

SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
# By full path (never whatever powershell.exe comes first on PATH), and told to write
# UTF-8, so app names and IDs in any language come through intact.
POWERSHELL = os.path.join(SYSTEM_ROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
LIST_COMMAND = (
    POWERSHELL,
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress",
)

# Runs a command (an argument list, never a shell) and returns its standard output.
Runner = Callable[[Sequence[str], float], bytes]


class ListError(Exception):
    """Windows' app list couldn't be read. The message is shown on the page."""


@dataclass(frozen=True)
class AppEntry:
    app_id: str
    title: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class Bookmark:
    name: str
    url: str


def apps_path() -> Path:
    return app_data_dir() / "apps.json"


def bookmarks_path() -> Path:
    return app_data_dir() / "bookmarks.json"


# --- checks ---


def check_name(value: Any) -> str:
    """A spoken name, lowercased with its spaces tidied. Raises ValueError if it isn't one."""
    if not isinstance(value, str):
        raise ValueError("A name must be text.")
    name = " ".join(value.split()).lower()
    if not name:
        raise ValueError("A name can't be empty.")
    if len(name) > MAX_NAME:
        raise ValueError(f"A name can be at most {MAX_NAME} characters.")
    if not all(c.isalpha() or c in DIGITS or c in NAME_PUNCTUATION for c in name):
        raise ValueError("A name can only have letters, digits, spaces, - ' and .")
    if not any(c.isalpha() or c in DIGITS for c in name):
        raise ValueError("A name needs a letter or a digit.")
    return name


def check_app_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("An app ID must be text.")
    if len(value) > MAX_APP_ID:
        raise ValueError(f"An app ID can be at most {MAX_APP_ID} characters.")
    if any(ord(c) < 32 or ord(c) == 127 or c == '"' for c in value):
        raise ValueError("An app ID can't contain control characters or quotes.")
    return value


def clean_title(value: Any) -> str:
    """An app's name from Windows, as one short line of text."""
    text = " ".join(str(value or "").split())
    text = "".join(c for c in text if ord(c) >= 32 and ord(c) != 127)
    return text[:MAX_TITLE]


# --- listing Windows' apps ---


def run_command(args: Sequence[str], timeout: float) -> bytes:
    """Run an argument list (no shell) with a timeout, without a console window flashing
    up, and return its standard output. Raises ListError."""
    try:
        done = subprocess.run(
            list(args),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except FileNotFoundError as exc:
        raise ListError("PowerShell wasn't found.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ListError("Windows took too long to list the apps.") from exc
    except OSError as exc:
        raise ListError(f"Couldn't ask Windows for the apps ({type(exc).__name__}).") from exc
    if done.returncode != 0:
        raise ListError(f"Windows couldn't list the apps (exit code {done.returncode}).")
    return done.stdout


def _decode(out: bytes) -> str:
    """PowerShell's output: UTF-8 (the command asks for it), with or without a BOM."""
    try:
        return out.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ListError("Windows' app list wasn't readable.") from exc


def parse_listing(out: bytes | str) -> list[dict[str, str]]:
    """Get-StartApps as JSON: a list, a single object (one app) or nothing (no apps).
    Entries without a usable name and ID are skipped; IDs are unique; sorted by name."""
    text = _decode(out) if isinstance(out, bytes) else out
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ListError("Windows' app list wasn't readable.") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ListError("Windows' app list wasn't readable.")
    apps: dict[str, dict[str, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        title = clean_title(item.get("Name"))
        try:
            app_id = check_app_id(item.get("AppID"))
        except ValueError:
            continue
        if title and app_id not in apps:
            apps[app_id] = {"name": title, "app_id": app_id}
    return sorted(apps.values(), key=lambda a: (a["name"].casefold(), a["app_id"]))


# --- the store ---


class AppsStore:
    """The allowlisted apps and the bookmarks. Thread-safe. `version` goes up with every
    saved change, so CARL knows to rebuild its tools at the next question."""

    def __init__(
        self,
        apps_file: Path | None = None,
        bookmarks_file: Path | None = None,
        *,
        runner: Runner = run_command,
    ) -> None:
        self.apps_file = apps_file or apps_path()
        self.bookmarks_file = bookmarks_file or bookmarks_path()
        self._runner = runner
        self._lock = threading.Lock()
        self._listing_lock = threading.Lock()  # one PowerShell at a time
        self._listed: dict[str, str] = {}  # app_id -> title, from the last listing
        self._apps: tuple[AppEntry, ...] = ()
        self._bookmarks: tuple[Bookmark, ...] = ()
        self.version = 0
        self.apps_error: str | None = None
        self.bookmarks_error: str | None = None
        self._read_apps()
        self._read_bookmarks()

    @property
    def error(self) -> str | None:
        return " ".join(e for e in (self.apps_error, self.bookmarks_error) if e) or None

    # --- what CARL uses ---

    def snapshot(self) -> tuple[int, tuple[AppEntry, ...], tuple[Bookmark, ...]]:
        with self._lock:
            return self.version, self._apps, self._bookmarks

    # --- Windows' list ---

    def available(self) -> list[dict[str, str]]:
        """Ask Windows for its apps (Get-StartApps). Blocking: call it off the event
        loop, and only when the page asks, never during a turn. Raises ListError."""
        with self._listing_lock:
            listing = parse_listing(self._runner(LIST_COMMAND, LIST_TIMEOUT_S))
        with self._lock:
            self._listed = {a["app_id"]: a["name"] for a in listing}
        log.info("Apps: Windows listed %d apps", len(listing))
        return listing

    # --- the page's views and changes ---

    def apps_view(self) -> dict[str, Any]:
        with self._lock:
            apps = self._apps
        return {
            "apps": [{"app_id": a.app_id, "title": a.title, "names": list(a.names)} for a in apps],
            "limits": {"apps": MAX_APPS, "names": MAX_NAMES, "name": MAX_NAME},
            "error": self.error,
        }

    def bookmarks_view(self) -> dict[str, Any]:
        with self._lock:
            bookmarks = self._bookmarks
        return {
            "bookmarks": [{"name": b.name, "url": b.url} for b in bookmarks],
            "limits": {"bookmarks": MAX_BOOKMARKS, "name": MAX_NAME, "url": MAX_URL},
            "error": self.error,
        }

    def update_apps(self, value: Any) -> dict[str, str]:
        """Replace the whole list: [{"app_id", "names": [...]}] (a title sent with it is
        ignored: it comes from Windows' list or the saved entry). All or nothing; returns
        an error per entry ("apps", "apps.<i>"), or {} once saved."""
        if not isinstance(value, list):
            return {"apps": "Send a list of apps."}
        if len(value) > MAX_APPS:
            return {"apps": f"At most {MAX_APPS} apps."}
        errors: dict[str, str] = {}
        entries: list[AppEntry] = []
        seen_ids: set[str] = set()
        seen_names: dict[str, int] = {}
        with self._lock:
            known = {a.app_id: a.title for a in self._apps}
            listed = dict(self._listed)
            for i, item in enumerate(value):
                try:
                    entry = self._check_app(item, listed, known)
                except ValueError as exc:
                    errors[f"apps.{i}"] = str(exc)
                    continue
                if entry.app_id in seen_ids:
                    errors[f"apps.{i}"] = "This app is on the list twice."
                    continue
                seen_ids.add(entry.app_id)
                for name in entry.names:
                    if name in seen_names:
                        errors[f"apps.{i}"] = f"The name {name!r} is already used."
                        break
                    seen_names[name] = i
                entries.append(entry)
            if errors:
                return errors
            new = tuple(entries)
            data = [{"app_id": a.app_id, "title": a.title, "names": list(a.names)} for a in new]
            write_json_atomic(self.apps_file, data)
            if new != self._apps:
                self.version += 1
            self._apps, self.apps_error = new, None
        log.info("Dashboard: app list saved (%d apps)", len(new))
        return {}

    def update_bookmarks(self, value: Any) -> dict[str, str]:
        """Replace the whole list: [{"name", "url"}]. All or nothing; returns an error per
        entry ("bookmarks", "bookmarks.<i>"), or {} once saved."""
        if not isinstance(value, list):
            return {"bookmarks": "Send a list of bookmarks."}
        if len(value) > MAX_BOOKMARKS:
            return {"bookmarks": f"At most {MAX_BOOKMARKS} bookmarks."}
        errors: dict[str, str] = {}
        entries: list[Bookmark] = []
        seen: set[str] = set()
        for i, item in enumerate(value):
            try:
                entry = _check_bookmark(item)
            except ValueError as exc:
                errors[f"bookmarks.{i}"] = str(exc)
                continue
            if entry.name in seen:
                errors[f"bookmarks.{i}"] = f"The name {entry.name!r} is already used."
                continue
            seen.add(entry.name)
            entries.append(entry)
        if errors:
            return errors
        new = tuple(entries)
        with self._lock:
            write_json_atomic(self.bookmarks_file, [{"name": b.name, "url": b.url} for b in new])
            if new != self._bookmarks:
                self.version += 1
            self._bookmarks, self.bookmarks_error = new, None
        log.info("Dashboard: bookmarks saved (%d)", len(new))
        return {}

    # --- internals ---

    @staticmethod
    def _check_app(item: Any, listed: Mapping[str, str], known: Mapping[str, str]) -> AppEntry:
        if not isinstance(item, Mapping):
            raise ValueError("An app needs an app_id and names.")
        app_id = check_app_id(item.get("app_id"))
        if app_id in listed:
            title = listed[app_id]
        elif app_id in known:
            title = known[app_id]
        else:
            raise ValueError("That app isn't in Windows' list: use Find apps.")
        return AppEntry(app_id, title, _check_names(item.get("names")))

    def _read_apps(self) -> None:
        raw = self._load(self.apps_file)
        if raw is None:
            return
        if not isinstance(raw, list):
            self._warn_apps(f"{self.apps_file.name} isn't a list; no apps are allowed")
            return
        problems: list[str] = []
        apps: list[AppEntry] = []
        ids: set[str] = set()
        names: set[str] = set()
        for i, item in enumerate(raw[:MAX_APPS]):
            try:
                if not isinstance(item, Mapping):
                    raise ValueError("not an object")
                entry = AppEntry(
                    check_app_id(item.get("app_id")),
                    clean_title(item.get("title")) or str(item.get("app_id")),
                    _check_names(item.get("names")),
                )
                if entry.app_id in ids or names.intersection(entry.names):
                    raise ValueError("a repeated app or name")
            except ValueError as exc:
                problems.append(f"app {i + 1}: {exc}")
                continue
            ids.add(entry.app_id)
            names.update(entry.names)
            apps.append(entry)
        if len(raw) > MAX_APPS:
            problems.append(f"only the first {MAX_APPS} apps are used")
        self._apps = tuple(apps)
        if problems:
            self._warn_apps(f"{self.apps_file.name}: skipped {'; '.join(problems)}")

    def _read_bookmarks(self) -> None:
        raw = self._load(self.bookmarks_file)
        if raw is None:
            return
        if not isinstance(raw, list):
            self._warn_bookmarks(f"{self.bookmarks_file.name} isn't a list; no bookmarks")
            return
        problems: list[str] = []
        bookmarks: list[Bookmark] = []
        names: set[str] = set()
        for i, item in enumerate(raw[:MAX_BOOKMARKS]):
            try:
                entry = _check_bookmark(item)
                if entry.name in names:
                    raise ValueError("a repeated name")
            except ValueError as exc:
                problems.append(f"bookmark {i + 1}: {exc}")
                continue
            names.add(entry.name)
            bookmarks.append(entry)
        if len(raw) > MAX_BOOKMARKS:
            problems.append(f"only the first {MAX_BOOKMARKS} bookmarks are used")
        self._bookmarks = tuple(bookmarks)
        if problems:
            self._warn_bookmarks(f"{self.bookmarks_file.name}: skipped {'; '.join(problems)}")

    def _load(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            message = f"{path.name} couldn't be read ({type(exc).__name__}); it's ignored"
            if path == self.apps_file:
                self._warn_apps(message)
            else:
                self._warn_bookmarks(message)
            return None

    def _warn_apps(self, message: str) -> None:
        self.apps_error = message
        log.warning("Apps: %s", message)

    def _warn_bookmarks(self, message: str) -> None:
        self.bookmarks_error = message
        log.warning("Bookmarks: %s", message)


def _check_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Give 1 to {MAX_NAMES} spoken names.")
    if len(value) > MAX_NAMES:
        raise ValueError(f"At most {MAX_NAMES} spoken names.")
    names: list[str] = []
    for item in value:
        name = check_name(item)
        if name in names:
            raise ValueError(f"The name {name!r} is there twice.")
        names.append(name)
    return tuple(names)


def _check_bookmark(item: Any) -> Bookmark:
    if not isinstance(item, Mapping):
        raise ValueError("A bookmark needs a name and an address.")
    url = item.get("url")
    if isinstance(url, str):
        url = url.strip()
    return Bookmark(check_name(item.get("name")), check_url(url))
