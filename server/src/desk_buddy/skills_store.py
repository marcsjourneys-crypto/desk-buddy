"""The user's own skills: folders in SKILLS_DIR, and which ones are approved.

- A skill is a subfolder of SKILLS_DIR. Its name is the slug ([a-z0-9-]{1,40}), and its
  tool will be skill_<slug> with "-" becoming "_". It holds skill.json (the manifest,
  checked here) and main.py; .env (the skill's own secrets) is never read.
- The fingerprint is a SHA-256 over every regular file in the folder (recursively,
  skipping .venv, __pycache__ and dot-folders), except the top-level .env: a change to
  any of them switches the skill off until it is approved again. A link (symlink,
  junction or other reparse point) anywhere in the folder makes the skill invalid.
- Approvals: %LOCALAPPDATA%\\desk-buddy\\skills.json, [{"slug", "fingerprint"}], at most
  MAX_APPROVED. Saves are atomic; a corrupt file is ignored with a warning and shown as a
  banner, a bad entry in it is skipped.

Every problem is reported per skill as (field, reason), so the page can show it next to
the skill. Scanning only reads files: nothing here runs a skill or uv.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from desk_buddy.apps_store import AppEntry, Bookmark, check_names
from desk_buddy.config import app_data_dir
from desk_buddy.personality_store import LINE_BREAKERS
from desk_buddy.settings_store import write_json_atomic

log = logging.getLogger(__name__)

MANIFEST = "skill.json"
ENTRY = "main.py"
MAX_APPROVED = 20  # enabled skills
MAX_MANIFEST_BYTES = 16 * 1024
MAX_FILES = 200  # fingerprinted files per skill
MAX_TOTAL_BYTES = 5 * 1024 * 1024  # fingerprinted bytes per skill
MAX_SKILL_NAME = 40
MAX_DESCRIPTION = 500
MAX_CHOICES = 4
MIN_OPTIONS, MAX_OPTIONS = 2, 10
MAX_OPTION = 40
MAX_INPUT_DESCRIPTION = 200
MAX_TEXT_LENGTH, DEFAULT_TEXT_LENGTH = 500, 300
MAX_TIMEOUT_S, DEFAULT_TIMEOUT_S = 120, 30
TOOL_PREFIX = "skill_"
# The name of the free-text value in a run's stdin JSON, so no choice may use it.
TEXT_KEY = "text"

SLUG = re.compile(r"[a-z0-9-]{1,40}")
CHOICE_NAME = re.compile(r"[a-z_]{1,20}")
OPTION = re.compile(r"[a-z0-9 _-]{1,40}")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")

# Folders the fingerprint (and so a skill) never looks into, besides dot-folders.
SKIPPED_DIRS = frozenset({".venv", "__pycache__"})
# The skill's own secrets, at the top of its folder: the one file never read or hashed.
SECRETS_FILE = ".env"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
LINKS_REFUSED = "Links aren't allowed in a skill folder."
TOO_BIG = f"The files add up to more than {MAX_TOTAL_BYTES // 2**20} MB."
# Names Windows keeps for devices: a folder can't usefully have one.
RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

MANIFEST_KEYS = frozenset(
    {"name", "spoken_names", "description", "inputs", "needs_yes", "timeout_seconds"}
)
INPUTS_KEYS = frozenset({"choices", "text"})
CHOICE_KEYS = frozenset({"options", "description", "default"})
TEXT_KEYS = frozenset({"description", "max_length", "required"})

INVALID = "invalid"
AVAILABLE = "available"  # valid, not approved
APPROVED = "approved"  # valid, approved with this fingerprint
CHANGED = "changed"  # valid, approved with another fingerprint: re-approve


# --- what a skill is ---


@dataclass(frozen=True)
class SkillError:
    """One problem with a skill: the manifest field (e.g. "inputs.choices.format.default",
    or "folder", "skill.json", "main.py", "files") and why, for the page."""

    field: str
    reason: str


@dataclass(frozen=True)
class Choice:
    name: str
    options: tuple[str, ...]
    description: str = ""
    default: str | None = None  # None: the choice is required

    @property
    def required(self) -> bool:
        return self.default is None


@dataclass(frozen=True)
class TextInput:
    description: str
    max_length: int = DEFAULT_TEXT_LENGTH
    required: bool = False


@dataclass(frozen=True)
class Manifest:
    name: str
    spoken_names: tuple[str, ...]
    description: str
    choices: tuple[Choice, ...] = ()
    text: TextInput | None = None
    needs_yes: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Skill:
    """One subfolder of SKILLS_DIR, as scanned. `manifest` is None unless it is valid;
    `fingerprint` is None if the files couldn't be read or are over the limits."""

    slug: str
    folder: Path
    manifest: Manifest | None
    fingerprint: str | None
    errors: tuple[SkillError, ...] = ()
    status: str = AVAILABLE
    approved: bool = False  # an approval is saved for this slug (it may be out of date)

    @property
    def tool_name(self) -> str:
        return tool_name(self.slug)

    @property
    def valid(self) -> bool:
        return not self.errors and self.manifest is not None and self.fingerprint is not None


@dataclass(frozen=True)
class Scan:
    """SKILLS_DIR's skills, sorted by slug. `error` says why there are none at all (a
    missing or unreadable folder); blank SKILLS_DIR is simply off, with no error."""

    skills: tuple[Skill, ...] = ()
    error: str | None = None
    enabled: bool = False  # SKILLS_DIR is set
    problems: tuple[str, ...] = field(default=(), repr=False)  # one line per bad skill

    def get(self, slug: str) -> Skill | None:
        return next((s for s in self.skills if s.slug == slug), None)

    @property
    def usable(self) -> tuple[Skill, ...]:
        """The skills CARL may offer: valid and approved with their current files."""
        return tuple(s for s in self.skills if s.status == APPROVED)


def tool_name(slug: str) -> str:
    return TOOL_PREFIX + slug.replace("-", "_")


def check_slug(value: Any) -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError(
            "The folder name must be 1-40 lowercase letters, digits and -, like order-check."
        )
    if value in RESERVED_NAMES:
        raise ValueError(f"{value!r} is a name Windows keeps for devices.")
    return value


def skills_path() -> Path:
    return app_data_dir() / "skills.json"


def taken_names(apps: Iterable[AppEntry], bookmarks: Iterable[Bookmark]) -> frozenset[str]:
    """The spoken names apps and bookmarks already use (AppsStore.snapshot())."""
    names = {name for app in apps for name in app.names}
    names.update(b.name for b in bookmarks)
    return frozenset(names)


# --- the manifest ---


class _Problems:
    def __init__(self) -> None:
        self.errors: list[SkillError] = []

    def add(self, where: str, reason: str) -> None:
        self.errors.append(SkillError(where, reason))


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"the key {key!r} is there twice")
        out[key] = value
    return out


def parse_manifest(data: bytes) -> tuple[Manifest | None, tuple[SkillError, ...]]:
    """skill.json's bytes as a Manifest, or None and every problem found."""
    if len(data) > MAX_MANIFEST_BYTES:
        return None, (SkillError(MANIFEST, f"It can be at most {MAX_MANIFEST_BYTES} bytes."),)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, (SkillError(MANIFEST, "It must be UTF-8 text."),)
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicates)
    except RecursionError:
        return None, (SkillError(MANIFEST, "It isn't valid JSON (nested too deeply)."),)
    except ValueError as exc:
        return None, (SkillError(MANIFEST, f"It isn't valid JSON ({exc})."),)
    if not isinstance(raw, dict):
        return None, (SkillError(MANIFEST, "It must be a JSON object."),)
    return check_manifest(raw)


def check_manifest(raw: Mapping[str, Any]) -> tuple[Manifest | None, tuple[SkillError, ...]]:
    p = _Problems()
    _unknown_keys(p, raw, MANIFEST_KEYS, "")
    name = _one_line(p, raw.get("name"), "name", MAX_SKILL_NAME, required=True)
    spoken: tuple[str, ...] = ()
    if "spoken_names" not in raw:
        p.add("spoken_names", "Required: 1 to 3 spoken names.")
    else:
        try:
            spoken = check_names(raw["spoken_names"])
        except ValueError as exc:
            p.add("spoken_names", str(exc))
    description = _description(p, raw.get("description"))
    choices, text = _inputs(p, raw.get("inputs", {}))
    needs_yes = _bool(p, raw.get("needs_yes", False), "needs_yes")
    timeout = _int(
        p, raw.get("timeout_seconds", DEFAULT_TIMEOUT_S), "timeout_seconds", 1, MAX_TIMEOUT_S
    )
    if p.errors:
        return None, tuple(p.errors)
    manifest = Manifest(
        name=name,
        spoken_names=spoken,
        description=description,
        choices=choices,
        text=text,
        needs_yes=needs_yes,
        timeout_seconds=timeout,
    )
    return manifest, ()


def _unknown_keys(p: _Problems, raw: Mapping[str, Any], allowed: frozenset[str], at: str) -> None:
    for key in raw:
        if key not in allowed:
            where = f"{at}.{key}" if at else str(key)
            p.add(where, f"Unknown key; allowed: {', '.join(sorted(allowed))}.")


def _one_line(p: _Problems, value: Any, where: str, limit: int, *, required: bool) -> str:
    if value is None:
        if required:
            p.add(where, f"Required: 1 to {limit} characters.")
        return ""
    if not isinstance(value, str):
        p.add(where, "This must be text.")
        return ""
    text = value.strip()
    if not text:
        p.add(where, f"This can't be empty (1 to {limit} characters).")
    elif len(text) > limit:
        p.add(where, f"This can be at most {limit} characters.")
    elif any(ord(c) < 32 or ord(c) == 127 or c in LINE_BREAKERS for c in text):
        p.add(where, "This must be one line of text, without control characters.")
    return text


def _description(p: _Problems, value: Any) -> str:
    """The skill's description: up to MAX_DESCRIPTION characters; new lines are allowed
    (it goes into the tool description verbatim), other control characters aren't."""
    where = "description"
    if value is None:
        p.add(where, f"Required: 1 to {MAX_DESCRIPTION} characters.")
        return ""
    if not isinstance(value, str):
        p.add(where, "This must be text.")
        return ""
    text = value.replace("\r\n", "\n").strip()
    if not text:
        p.add(where, f"This can't be empty (1 to {MAX_DESCRIPTION} characters).")
    elif len(text) > MAX_DESCRIPTION:
        p.add(where, f"This can be at most {MAX_DESCRIPTION} characters.")
    elif any((ord(c) < 32 or ord(c) == 127) and c not in "\n\t" for c in text):
        p.add(where, "This can't contain control characters.")
    return text


def _bool(p: _Problems, value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        p.add(where, "This must be true or false.")
        return False
    return value


def _int(p: _Problems, value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        p.add(where, f"This must be a whole number {low}-{high}.")
        return low
    return value


def _inputs(p: _Problems, value: Any) -> tuple[tuple[Choice, ...], TextInput | None]:
    if not isinstance(value, Mapping):
        p.add("inputs", 'This must be an object: {"choices": {...}, "text": {...}}.')
        return (), None
    _unknown_keys(p, value, INPUTS_KEYS, "inputs")
    choices = _choices(p, value.get("choices", {}))
    text = _text_input(p, value["text"]) if "text" in value else None
    return choices, text


def _choices(p: _Problems, value: Any) -> tuple[Choice, ...]:
    where = "inputs.choices"
    if not isinstance(value, Mapping):
        p.add(where, 'This must be an object of name: {"options": [...]}.')
        return ()
    if len(value) > MAX_CHOICES:
        p.add(where, f"At most {MAX_CHOICES} choices.")
        return ()
    choices: list[Choice] = []
    for name, spec in value.items():
        at = f"{where}.{name}"
        if not CHOICE_NAME.fullmatch(name):
            p.add(at, "A choice's name must be 1-20 lowercase letters and _.")
            continue
        if name == TEXT_KEY:
            p.add(at, f"{TEXT_KEY!r} is kept for the text input; use another name.")
            continue
        choice = _choice(p, name, spec, at)
        if choice is not None:
            choices.append(choice)
    return tuple(choices)


def _choice(p: _Problems, name: str, spec: Any, at: str) -> Choice | None:
    if not isinstance(spec, Mapping):
        p.add(at, 'This must be an object: {"options": [...]}.')
        return None
    before = len(p.errors)
    _unknown_keys(p, spec, CHOICE_KEYS, at)
    options = spec.get("options")
    good: list[str] = []
    if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        p.add(f"{at}.options", f"Give {MIN_OPTIONS} to {MAX_OPTIONS} options.")
    else:
        for i, option in enumerate(options):
            if (
                not isinstance(option, str)
                or not OPTION.fullmatch(option)
                or option.strip() != option
            ):
                p.add(
                    f"{at}.options.{i}",
                    "An option must be 1-40 lowercase letters, digits, spaces, _ and -, "
                    "with no space at either end.",
                )
            elif option in good:
                p.add(f"{at}.options.{i}", f"The option {option!r} is there twice.")
            else:
                good.append(option)
    description = ""
    if "description" in spec:
        description = _one_line(
            p, spec["description"], f"{at}.description", MAX_INPUT_DESCRIPTION, required=True
        )
    default = spec.get("default")
    if "default" in spec and default not in good:
        p.add(f"{at}.default", "The default must be one of the options.")
    if len(p.errors) > before:
        return None
    return Choice(name, tuple(good), description, default)


def _text_input(p: _Problems, value: Any) -> TextInput | None:
    at = "inputs.text"
    if not isinstance(value, Mapping):
        p.add(at, 'This must be an object: {"description": "..."}.')
        return None
    before = len(p.errors)
    _unknown_keys(p, value, TEXT_KEYS, at)
    description = _one_line(
        p, value.get("description"), f"{at}.description", MAX_INPUT_DESCRIPTION, required=True
    )
    max_length = _int(
        p, value.get("max_length", DEFAULT_TEXT_LENGTH), f"{at}.max_length", 1, MAX_TEXT_LENGTH
    )
    required = _bool(p, value.get("required", False), f"{at}.required")
    if len(p.errors) > before:
        return None
    return TextInput(description, max_length, required)


# --- the fingerprint ---


def is_link(path: str | os.PathLike[str] | os.DirEntry[str]) -> bool:
    """A symlink, a junction or any other reparse point: never followed or hashed."""
    if isinstance(path, os.DirEntry):
        if path.is_symlink() or path.is_junction():
            return True
        st = path.stat(follow_symlinks=False)
    else:
        target = Path(path)
        if target.is_symlink() or target.is_junction():
            return True
        st = os.lstat(target)
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def covered_files(folder: Path) -> list[tuple[str, Path, int]]:
    """The files the fingerprint covers, as (relative path with "/", path, size), sorted:
    every regular file except the top-level .env, skipping .venv, __pycache__ and
    dot-folders (dot-files count). Raises ValueError for a link anywhere, past MAX_FILES,
    or once the sizes add up past MAX_TOTAL_BYTES (nothing is read here); OSError if a
    folder can't be read."""
    files: list[tuple[str, Path, int]] = []
    total = 0
    pending: list[tuple[str, str]] = [(str(folder), "")]
    while pending:
        here, prefix = pending.pop()
        with os.scandir(here) as entries:
            for entry in entries:
                if is_link(entry):
                    raise ValueError(LINKS_REFUSED)
                rel = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIPPED_DIRS and not entry.name.startswith("."):
                        pending.append((entry.path, rel + "/"))
                    continue
                if not entry.is_file(follow_symlinks=False) or rel == SECRETS_FILE:
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError(TOO_BIG)
                files.append((rel, Path(entry.path), size))
                if len(files) > MAX_FILES:
                    raise ValueError(f"More than {MAX_FILES} files to check.")
    return sorted(files)


def fingerprint(folder: Path, manifest_bytes: bytes | None = None) -> str:
    """SHA-256 over the covered files' relative paths and contents. With `manifest_bytes`,
    skill.json is hashed from those exact bytes (the ones that were parsed) instead of
    being read again. Raises ValueError for a link or past the limits (MAX_FILES,
    MAX_TOTAL_BYTES), OSError if a file can't be read."""
    if is_link(folder):
        raise ValueError(LINKS_REFUSED)
    digest = hashlib.sha256()
    total = 0
    for rel, path, _size in covered_files(folder):
        if rel.lower() == MANIFEST and manifest_bytes is not None:
            content = manifest_bytes
        else:
            with path.open("rb") as f:
                # Never more than the budget left, even if the file grew since its size
                # was checked.
                content = f.read(MAX_TOTAL_BYTES - total + 1)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(TOO_BIG)
        name = rel.encode("utf-8")
        # Lengths first, so no two different sets of files can hash the same bytes.
        digest.update(b"%d:%d:" % (len(name), len(content)))
        digest.update(name)
        digest.update(content)
    return digest.hexdigest()


# --- scanning one skill ---


def inspect_skill(folder: Path) -> Skill:
    """One skill folder as it is now: its manifest, fingerprint and problems. The status is
    INVALID or AVAILABLE (approvals are SkillsStore's)."""
    slug = folder.name
    errors: list[SkillError] = []
    try:
        check_slug(slug)
    except ValueError as exc:
        errors.append(SkillError("folder", str(exc)))
    manifest: Manifest | None = None
    data: bytes | None = None
    manifest_file = folder / MANIFEST
    try:
        with manifest_file.open("rb") as f:
            data = f.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        errors.append(SkillError(MANIFEST, "The skill needs a skill.json."))
    except OSError as exc:
        errors.append(SkillError(MANIFEST, f"It couldn't be read ({type(exc).__name__})."))
    else:
        manifest, problems = parse_manifest(data)
        errors.extend(problems)
    if not (folder / ENTRY).is_file():
        errors.append(SkillError(ENTRY, "The skill needs a main.py."))
    fp = None
    try:
        fp = fingerprint(folder, manifest_bytes=data)
    except ValueError as exc:
        errors.append(SkillError("files", str(exc)))
    except OSError as exc:
        errors.append(SkillError("files", f"A file couldn't be read ({type(exc).__name__})."))
    status = INVALID if errors else AVAILABLE
    return Skill(slug, folder, manifest, fp, tuple(errors), status)


def find_clashes(
    skills: Iterable[Skill], other_names: Iterable[str] = ()
) -> dict[str, list[SkillError]]:
    """Spoken names a skill shares with another skill, an app or a bookmark, and tool
    names two skills would share: {slug: [errors]}. Every skill in a clash gets one."""
    other = frozenset(other_names)
    # Only skills that are otherwise fine: a broken one can't take a name from a good one.
    skills = [s for s in skills if s.manifest is not None and not s.errors]
    users: dict[str, list[str]] = {}
    tools: dict[str, list[str]] = {}
    for s in skills:
        assert s.manifest is not None
        for name in s.manifest.spoken_names:
            users.setdefault(name, []).append(s.slug)
        tools.setdefault(s.tool_name, []).append(s.slug)
    clashes: dict[str, list[SkillError]] = {}
    for s in skills:
        assert s.manifest is not None
        for name in s.manifest.spoken_names:
            if name in other:
                reason = f"The name {name!r} is already used by an app or bookmark."
                clashes.setdefault(s.slug, []).append(SkillError("spoken_names", reason))
            sharing = [slug for slug in users[name] if slug != s.slug]
            if sharing:
                reason = f"The name {name!r} is also used by the skill {sharing[0]!r}."
                clashes.setdefault(s.slug, []).append(SkillError("spoken_names", reason))
        sharing = [slug for slug in tools[s.tool_name] if slug != s.slug]
        if sharing:
            reason = f"Its tool name {s.tool_name} is also the skill {sharing[0]!r}'s."
            clashes.setdefault(s.slug, []).append(SkillError("folder", reason))
    return clashes


# --- the store ---


class SkillsStore:
    """The approved skills (slug and fingerprint) and scans of SKILLS_DIR. Thread-safe.
    `version` goes up with every saved change, so CARL knows to rebuild its tools."""

    def __init__(self, approvals_file: Path | None = None) -> None:
        self.approvals_file = approvals_file or skills_path()
        self._lock = threading.Lock()
        self._approved: dict[str, str] = {}  # slug -> fingerprint, in saved order
        self._logged: frozenset[str] = frozenset()  # the last scan's problems, logged
        self.version = 0
        self.error: str | None = None
        self._read()

    # --- approvals ---

    def approvals(self) -> dict[str, str]:
        with self._lock:
            return dict(self._approved)

    def approve(self, slug: str, fp: str, present: Iterable[str] | None = None) -> None:
        """Save `slug` as approved with this fingerprint (computed after any uv sync).
        With `present` (the slugs in SKILLS_DIR now), only approvals of present skills
        count toward MAX_APPROVED: orphans (a folder that's gone, or another SKILLS_DIR's)
        are dropped, oldest first, when the file would pass the limit. Saving the same
        approval again still bumps `version`, so CARL rescans. Raises ValueError for a bad
        slug or fingerprint, or past MAX_APPROVED."""
        check_slug(slug)
        if not isinstance(fp, str) or not FINGERPRINT.fullmatch(fp):
            raise ValueError("That isn't a fingerprint.")
        here = None if present is None else frozenset(present)
        with self._lock:
            new = dict(self._approved)
            if slug not in new:
                counted = [k for k in new if here is None or k in here]
                if len(counted) >= MAX_APPROVED:
                    raise ValueError(f"At most {MAX_APPROVED} skills can be on.")
                orphans = [k for k in new if here is not None and k not in here]
                while len(new) >= MAX_APPROVED and orphans:
                    del new[orphans.pop(0)]
            new[slug] = fp
            self._save(new, bump=True)
        log.info("Skills: %s approved", slug)

    def touch(self) -> None:
        """Bump `version` without a change, so CARL rescans the skills."""
        with self._lock:
            self.version += 1

    def disable(self, slug: str) -> bool:
        """Forget the approval of `slug`. False if it wasn't approved."""
        with self._lock:
            if slug not in self._approved:
                return False
            new = {k: v for k, v in self._approved.items() if k != slug}
            self._save(new)
        log.info("Skills: %s turned off", slug)
        return True

    # --- scans ---

    def scan(self, skills_dir: str | Path | None, other_names: Iterable[str] = ()) -> Scan:
        """Every subfolder of `skills_dir` (dot-folders and __pycache__ skipped) with its
        status, given the approvals and the names apps and bookmarks use (taken_names).
        Blocking file reads: call it between turns or off the event loop."""
        if not skills_dir:
            return Scan()
        root = Path(skills_dir)
        try:
            folders = sorted(
                p
                for p in root.iterdir()
                if p.is_dir() and not p.name.startswith(".") and p.name not in SKIPPED_DIRS
            )
        except OSError as exc:
            message = (
                f"SKILLS_DIR isn't an existing folder: {str(root)!r}"
                if isinstance(exc, (FileNotFoundError, NotADirectoryError))
                else f"SKILLS_DIR couldn't be read ({type(exc).__name__})"
            )
            self._log_new((message,))
            return Scan(error=message, enabled=True)
        found = [inspect_skill(folder) for folder in folders]
        clashes = find_clashes(found, other_names)
        approvals = self.approvals()
        skills: list[Skill] = []
        problems: list[str] = []
        for s in found:
            errors = s.errors + tuple(clashes.get(s.slug, ()))
            approved_fp = approvals.get(s.slug)
            if errors:
                status = INVALID
                reasons = "; ".join(f"{e.field}: {e.reason}" for e in errors)
                problems.append(f"{s.slug}: {reasons}")
            elif approved_fp is None:
                status = AVAILABLE
            elif approved_fp == s.fingerprint:
                status = APPROVED
            else:
                status = CHANGED
                problems.append(f"{s.slug}: its files changed; approve it again")
            skills.append(
                Skill(
                    s.slug,
                    s.folder,
                    s.manifest if not errors else None,
                    s.fingerprint,
                    errors,
                    status,
                    approved_fp is not None,
                )
            )
        self._log_new(problems)
        return Scan(tuple(skills), None, True, tuple(problems))

    def check(self, skills_dir: str | Path, slug: str) -> Skill | None:
        """One skill as it is right now (just before a run): its status from its current
        files and the approvals, without the clash checks. None if there's no such folder."""
        try:
            check_slug(slug)
        except ValueError:
            return None
        folder = Path(skills_dir) / slug
        if not folder.is_dir():
            return None
        s = inspect_skill(folder)
        approved_fp = self.approvals().get(slug)
        if s.errors:
            status = INVALID
        elif approved_fp is None:
            status = AVAILABLE
        else:
            status = APPROVED if approved_fp == s.fingerprint else CHANGED
        manifest = s.manifest if not s.errors else None
        return Skill(
            slug, folder, manifest, s.fingerprint, s.errors, status, approved_fp is not None
        )

    # --- internals ---

    def _save(self, new: dict[str, str], *, bump: bool = False) -> None:
        """Called with the lock held."""
        data = [{"slug": slug, "fingerprint": fp} for slug, fp in new.items()]
        write_json_atomic(self.approvals_file, data)
        if bump or new != self._approved:
            self.version += 1
        self._approved, self.error = new, None

    def _read(self) -> None:
        try:
            raw = json.loads(self.approvals_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError) as exc:
            name = self.approvals_file.name
            self._warn(f"{name} couldn't be read ({type(exc).__name__}); no skill is on")
            return
        if not isinstance(raw, list):
            self._warn(f"{self.approvals_file.name} isn't a list; no skill is on")
            return
        problems: list[str] = []
        approved: dict[str, str] = {}
        for i, item in enumerate(raw[:MAX_APPROVED]):
            try:
                if not isinstance(item, Mapping):
                    raise ValueError("not an object")
                slug = check_slug(item.get("slug"))
                fp = item.get("fingerprint")
                if not isinstance(fp, str) or not FINGERPRINT.fullmatch(fp):
                    raise ValueError("not a fingerprint")
                if slug in approved:
                    raise ValueError("a repeated skill")
            except ValueError as exc:
                problems.append(f"skill {i + 1}: {exc}")
                continue
            approved[slug] = fp
        if len(raw) > MAX_APPROVED:
            problems.append(f"only the first {MAX_APPROVED} skills are used")
        self._approved = approved
        if problems:
            self._warn(f"{self.approvals_file.name}: skipped {'; '.join(problems)}")

    def _warn(self, message: str) -> None:
        self.error = message
        log.warning("Skills: %s", message)

    def _log_new(self, problems: Iterable[str]) -> None:
        """Log the problems the last scan didn't have: one that was fixed and came back is
        logged again."""
        current = frozenset(problems)
        with self._lock:
            new = current - self._logged
            self._logged = current
        for message in sorted(new):
            log.warning("Skills: %s", message)
