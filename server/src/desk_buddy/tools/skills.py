"""Running one of the user's skills (main.py in its folder) in a clean environment.

- The command is an argument list built here, never a shell. The inputs go in as JSON on
  stdin, never on the command line.
- Nothing unhashed in the skill's folder gets executed:
  - a skill with a pyproject.toml runs with `uv run` in its own environment outside the
    folder (UV_PROJECT_ENVIRONMENT = <app data>\\skill-envs\\<slug>, set for `uv sync`
    too), so a .venv in the folder is never used. It must not sit inside another uv
    project (outside_project), or a parent workspace could pull in code from elsewhere;
    `uv sync --no-editable` copies path dependencies in, frozen at approval;
  - a skill without one runs without uv, as `<CARL's base python> -s main.py`: uv would
    pick up a .venv in the folder or above it, or build an environment from a PEP 723
    header during a turn;
  - PYTHONPYCACHEPREFIX points at <app data>\\skill-cache\\<slug>, so a planted
    __pycache__ .pyc is never loaded;
  - uv gets --no-config (no uv.toml in the folder or its parents counts), and `uv run`
    gets --no-python-downloads and --offline.
- The environment is built from an allowlist (CLEAN_ENV_NAMES): none of CARL's keys, no
  UV_* variable but the one CARL sets itself, no user site-packages, and no PATH entry
  inside CARL's own environment.
- One skill at a time; past its timeout the whole process tree is killed.
- Exit 0: stdout (capped) as untrusted data for Claude. Otherwise Claude gets only the exit
  code; stderr only ever goes to the server log, on a failure.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desk_buddy.skills_store import ENTRY, Skill, check_slug
from desk_buddy.tools.untrusted import wrap

log = logging.getLogger(__name__)

# The only variables a skill inherits from CARL's environment (Windows names are
# case-insensitive, so they're matched that way).
CLEAN_ENV_NAMES = (
    "SystemRoot",
    "SystemDrive",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "COMPUTERNAME",
    "USERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "COMSPEC",
)
ENVS_DIR = "skill-envs"
CACHE_DIR = "skill-cache"
PROJECT_FILE = "pyproject.toml"

STDOUT_LIMIT = 1000  # characters of the result Claude gets
STDOUT_READ_LIMIT = 16_000  # bytes decoded at most (a skill can print a lot)
STDERR_LOG_LIMIT = 500  # last characters of stderr logged on a failure
REAP_TIMEOUT_S = 5.0
KILL_TIMEOUT_S = 10.0
SYNC_TIMEOUT_S = 300.0

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

DONE = "Done."
REFUSED_BUSY = "refused: another skill is running"
TIMED_OUT = "failed: timed out"
NOT_STARTED = "failed: couldn't start"
INVALID = "failed: not a valid skill"

Popen = Callable[..., Any]
Killer = Callable[[int], None]


@dataclass(frozen=True)
class SkillResult:
    """`text` is for Claude; `summary` is short, for the log and the Live tab."""

    ok: bool
    text: str
    summary: str


# --- the commands and the environment ---


def find_uv(path: str | None = None) -> str | None:
    """uv.exe's full path from PATH (default: this process's), found once at startup, or
    None. Only absolute PATH entries count and only uv.exe is accepted: unlike
    shutil.which, the current folder is never searched and no .bat/.cmd is taken."""
    search = os.environ.get("PATH", "") if path is None else path
    for entry in search.split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, "uv.exe")
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def default_python() -> str:
    """CARL's own base interpreter (a uv-managed Python), for skills without a project."""
    return os.path.join(sys.base_prefix, "python.exe")


def python_ok(python: str) -> bool:
    """The interpreter for skills without a project exists (warn at startup if not)."""
    return os.path.isfile(python)


def has_project(folder: Path) -> bool:
    """The skill brings its own uv project (and so gets `uv sync` on approval)."""
    return (folder / PROJECT_FILE).is_file()


def outside_project(folder: Path) -> str | None:
    """None if no folder above `folder` (up to the drive root) holds a pyproject.toml;
    otherwise why the skill can't be synced or run: uv would treat that project (maybe a
    workspace naming this skill as a member) as the one to install, with code from outside
    the skill's folder. Check it before `uv sync` and before running a project skill."""
    for parent in Path(os.path.abspath(folder)).parents:
        if (parent / PROJECT_FILE).is_file():
            return f"The skill is inside another uv project ({parent})."
    return None


def sync_command(uv: str) -> list[str]:
    """`uv sync` for a skill with a project; run it in the skill's folder with clean_env.
    --no-editable copies path dependencies in, so they're frozen at approval."""
    return [uv, "sync", "--no-config", "--no-editable"]


def run_command(uv: str, *, project: bool, python: str | None = None) -> list[str]:
    """`uv run main.py` for a skill with a project. Without one, uv would use any .venv in
    the folder or above it (or build an environment from a PEP 723 header), so the skill
    runs directly with CARL's base interpreter, without the user site-packages."""
    if project:
        return [uv, "run", "--no-sync", "--no-config", "--no-python-downloads", "--offline", ENTRY]
    return [python or default_python(), "-s", ENTRY]


def clean_env(
    slug: str,
    app_dir: Path,
    environ: Mapping[str, str] | None = None,
    carl_prefix: str | None = None,
) -> dict[str, str]:
    """The environment a skill (and its `uv sync`) gets: the allowlisted variables from
    `environ` (default os.environ), with PATH entries inside CARL's own environment
    (`carl_prefix`, default sys.prefix: its venv's Scripts) dropped, plus the ones CARL
    sets. Raises ValueError for a bad slug."""
    check_slug(slug)
    source = os.environ if environ is None else environ
    by_upper = {name.upper(): value for name, value in source.items()}
    env = {name: by_upper[name.upper()] for name in CLEAN_ENV_NAMES if name.upper() in by_upper}
    if "PATH" in env:
        env["PATH"] = _without_prefix(env["PATH"], carl_prefix or sys.prefix)
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        UV_PROJECT_ENVIRONMENT=str(app_dir / ENVS_DIR / slug),
        PYTHONPYCACHEPREFIX=str(app_dir / CACHE_DIR / slug),
    )
    return env


def _without_prefix(path: str, prefix: str) -> str:
    root = os.path.normcase(os.path.abspath(prefix)).rstrip("\\/")
    kept = []
    for entry in path.split(os.pathsep):
        bare = entry.strip().strip('"')
        if not bare:
            continue
        if os.path.isabs(bare):
            here = os.path.normcase(os.path.abspath(bare)).rstrip("\\/")
            if here == root or here.startswith(root + os.sep):
                continue
        kept.append(entry)
    return os.pathsep.join(kept)


def kill_tree(pid: int) -> None:
    """Kill a process and everything it started, with Windows' own taskkill."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    taskkill = os.path.join(root, "System32", "taskkill.exe")
    try:
        subprocess.run(
            [taskkill, "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=KILL_TIMEOUT_S,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Skills: couldn't kill process %d (%s)", pid, type(exc).__name__)


# --- the runner ---


class SkillRunner:
    """Runs one skill at a time. `run` blocks: call it on a worker thread."""

    def __init__(
        self,
        uv: str,
        app_dir: Path,
        *,
        python: str | None = None,
        popen: Popen = subprocess.Popen,
        killer: Killer = kill_tree,
        logger: logging.Logger | None = None,
    ) -> None:
        self.uv = uv
        self.app_dir = Path(app_dir)
        self.python = python or default_python()
        self._popen = popen
        self._kill = killer
        self._log = logger or log
        self._lock = threading.Lock()

    def clean_env(self, slug: str) -> dict[str, str]:
        return clean_env(slug, self.app_dir)

    def command(self, skill: Skill) -> list[str]:
        return run_command(self.uv, project=has_project(skill.folder), python=self.python)

    def run(self, skill: Skill, inputs: Mapping[str, Any]) -> SkillResult:
        """Run `skill` with already-checked `inputs` (its choices and maybe "text")."""
        if not self._lock.acquire(blocking=False):
            return SkillResult(
                False, "Another skill is still running; nothing was run.", REFUSED_BUSY
            )
        try:
            return self._run(skill, inputs)
        finally:
            self._lock.release()

    def _run(self, skill: Skill, inputs: Mapping[str, Any]) -> SkillResult:
        slug = skill.slug
        if skill.manifest is None:
            return SkillResult(False, "That skill isn't available.", INVALID)
        try:
            env = self.clean_env(slug)
        except ValueError:
            return SkillResult(False, "That skill isn't available.", INVALID)
        stdin = json.dumps(dict(inputs), ensure_ascii=False).encode("utf-8")
        try:
            proc = self._popen(
                self.command(skill),
                cwd=str(skill.folder),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            self._log.warning("Skills: %s couldn't start (%s)", slug, type(exc).__name__)
            return SkillResult(False, "The skill couldn't be started.", NOT_STARTED)
        timeout = skill.manifest.timeout_seconds
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc.pid)
            err = self._reap(proc)
            self._log_stderr(slug, f"timed out after {timeout} s", err)
            return SkillResult(False, "The skill took too long and was stopped.", TIMED_OUT)
        code = proc.returncode
        if code != 0:
            summary = f"failed (exit {code})"
            self._log_stderr(slug, summary, err)
            return SkillResult(False, f"The skill {summary}.", summary)
        return SkillResult(True, self._result_text(slug, out), "ok")

    def _reap(self, proc: Any) -> bytes | None:
        """After the kill: collect what's left, without waiting long."""
        try:
            _out, err = proc.communicate(timeout=REAP_TIMEOUT_S)
            return err
        except (subprocess.TimeoutExpired, OSError, ValueError):
            try:
                proc.kill()
            except OSError:
                pass
            return None

    def _log_stderr(self, slug: str, what: str, err: bytes | None) -> None:
        tail = _decode(err)[-STDERR_LOG_LIMIT:].strip() if err else ""
        if tail:
            self._log.warning("Skills: %s %s; stderr: %s", slug, what, tail)
        else:
            self._log.warning("Skills: %s %s", slug, what)

    @staticmethod
    def _result_text(slug: str, out: bytes | None) -> str:
        text = _decode((out or b"")[:STDOUT_READ_LIMIT]).replace("\r\n", "\n").strip()
        if not text:
            return DONE
        source = f"skill:{slug}"
        overhead = len(wrap(source, ""))
        # The cap is on the escaped body, so anything past STDOUT_LIMIT characters (or
        # past it once escaped) is cut and marked "(and more)".
        return wrap(source, text, limit=STDOUT_LIMIT + overhead)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
