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
- A skill whose manifest says `pages` prints one JSON object instead,
  {"text": str, "pages": [{"title": str, "url": str}, ...]}: the text goes to Claude as
  above, and the pages (checked here: at most MAX_PAGES, public http(s) addresses only)
  come back for SearchResults, so open_search_result can open them after a yes. Anything
  else is "failed (bad output)"; the log gets a short reason, never the output.
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
from desk_buddy.tools.urls import Page, check_url, is_public_host, plain_line, site_name

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
PAGES_READ_LIMIT = 64_000  # bytes of a pages skill's JSON at most; more is bad output
MAX_PAGES = 10  # pages a skill may hand back
MAX_PAGE_TITLE = 120  # characters of each page's title
STDERR_LOG_LIMIT = 500  # last characters of stderr logged on a failure
REAP_TIMEOUT_S = 5.0
KILL_TIMEOUT_S = 10.0
SYNC_TIMEOUT_S = 300.0
SYNC_OUTPUT_LIMIT = 1000  # last characters of a failed `uv sync` shown on the page
LOCK_FILE = "uv.lock"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

DONE = "Done."
REFUSED_BUSY = "refused: another skill is running"
TIMED_OUT = "failed: timed out"
NOT_STARTED = "failed: couldn't start"
INVALID = "failed: not a valid skill"
INSIDE_PROJECT = "failed: inside another uv project"
BAD_OUTPUT = "failed (bad output)"

OUTPUT_KEYS = frozenset({"text", "pages"})
PAGE_KEYS = frozenset({"title", "url"})

Popen = Callable[..., Any]
Killer = Callable[[int], None]


@dataclass(frozen=True)
class SkillResult:
    """`text` is for Claude; `summary` is short, for the log and the Live tab; `pages`
    (only from a skill with `pages` in its manifest, already checked) are what
    open_search_result may open afterwards."""

    ok: bool
    text: str
    summary: str
    pages: tuple[Page, ...] = ()


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
    the skill's folder. Check it before `uv sync` and before running a project skill.

    Both the folder as given and its real location (links and junctions resolved) are
    checked, so a junctioned SKILLS_DIR can't hide a parent project."""
    places = [Path(os.path.abspath(folder))]
    try:
        places.append(Path(folder).resolve())
    except (OSError, RuntimeError):
        pass
    for place in places:
        for parent in place.parents:
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


@dataclass(frozen=True)
class SyncResult:
    """`uv sync` for a skill: whether it worked, and (on a failure) the last
    SYNC_OUTPUT_LIMIT characters of its output for the page. `busy`: nothing was started
    because a skill (or another setup) was running."""

    ok: bool
    error: str = ""
    output: str = ""
    busy: bool = False


def run_sync(
    uv: str,
    folder: Path,
    env: Mapping[str, str],
    *,
    popen: Popen = subprocess.Popen,
    killer: Killer | None = None,
    timeout: float = SYNC_TIMEOUT_S,
    on_start: Callable[[Any], None] | None = None,
) -> SyncResult:
    """`uv sync` (sync_command) in the skill's folder with its clean environment, no
    window and no shell. Blocking: call it off the event loop, and only from the
    dashboard (never during a turn). Past `timeout` the whole tree is killed.
    `on_start(proc)` gets the live process (so shutdown can kill it), then None."""
    try:
        proc = popen(
            sync_command(uv),
            cwd=str(folder),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        return SyncResult(False, f"uv couldn't be started ({type(exc).__name__}).")
    if on_start is not None:
        on_start(proc)
    try:
        out, _err, timed_out = communicate(
            proc, None, timeout, killer or kill_tree, reap_timeout=KILL_TIMEOUT_S
        )
    finally:
        if on_start is not None:
            on_start(None)
    if timed_out:
        return SyncResult(False, f"The setup (uv sync) took over {timeout:.0f} s.", _tail(out))
    if proc.returncode != 0:
        return SyncResult(
            False, f"The setup (uv sync) failed (exit {proc.returncode}).", _tail(out)
        )
    return SyncResult(True)


def communicate(
    proc: Any, stdin: bytes | None, timeout: float, killer: Killer, *, reap_timeout: float
) -> tuple[bytes | None, bytes | None, bool]:
    """proc.communicate with a timeout: (stdout, stderr, timed_out). Past the timeout the
    whole tree is killed and what's left collected for `reap_timeout` at most; if even
    that hangs, the process is killed and its pipes closed."""
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
        return out, err, False
    except subprocess.TimeoutExpired:
        pass
    killer(proc.pid)
    try:
        out, err = proc.communicate(timeout=reap_timeout)
        return out, err, True
    except (subprocess.TimeoutExpired, OSError, ValueError):
        _force_close(proc)
        return None, None, True


def _force_close(proc: Any) -> None:
    """Last resort after the tree kill and the reap both timed out. Only kills the
    process: closing its pipes would block for as long as Popen's reader thread is stuck
    in read() (it holds the buffer's lock), e.g. while a grandchild that escaped the tree
    kill still holds stdout. Those reader threads are daemons and are left behind."""
    try:
        proc.kill()
    except OSError:
        pass


def _tail(data: bytes | str | None) -> str:
    if not data:
        return ""
    text = data if isinstance(data, str) else _decode(data)
    return text.replace("\r\n", "\n")[-SYNC_OUTPUT_LIMIT:].strip()


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
        self._lock = threading.Lock()  # one skill (run or setup) at a time
        self._live_lock = threading.Lock()
        self._live: Any = None  # the process running now, for stop()
        self._stopped = False

    def clean_env(self, slug: str) -> dict[str, str]:
        return clean_env(slug, self.app_dir)

    def command(self, skill: Skill) -> list[str]:
        return run_command(self.uv, project=has_project(skill.folder), python=self.python)

    def sync(
        self, folder: Path, slug: str, *, sync: Callable[..., SyncResult] = run_sync
    ) -> SyncResult:
        """`uv sync` for one skill (the dashboard's approval), holding the same lock as a
        run: refused (busy) while a skill runs. Blocking: call it on a worker thread."""
        if self._stopped or not self._lock.acquire(blocking=False):
            return SyncResult(
                False, "A skill is running right now; try again when it's done.", busy=True
            )
        try:
            return sync(
                self.uv,
                folder,
                self.clean_env(slug),
                popen=self._popen,
                killer=self._kill,
                on_start=self._track,
            )
        finally:
            self._lock.release()

    def stop(self) -> None:
        """At shutdown: kill whatever is running (a skill or a setup) with its whole
        tree, so nothing waits on it; nothing new starts after this."""
        with self._live_lock:
            self._stopped = True
            proc = self._live
        if proc is not None:
            self._log.info("Skills: stopping process %d for shutdown", proc.pid)
            self._kill(proc.pid)

    def _track(self, proc: Any) -> None:
        with self._live_lock:
            self._live = proc
            stopped = self._stopped
        if proc is not None and stopped:
            self._kill(proc.pid)  # started just as shutdown began

    def run(self, skill: Skill, inputs: Mapping[str, Any]) -> SkillResult:
        """Run `skill` with already-checked `inputs` (its choices and maybe "text")."""
        if self._stopped:
            return SkillResult(False, "CARL is shutting down; nothing was run.", REFUSED_BUSY)
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
        if has_project(skill.folder):
            # The runner is the single checkpoint: nothing starts inside another project.
            reason = outside_project(skill.folder)
            if reason is not None:
                self._log.warning("Skills: %s not run: %s", slug, reason)
                return SkillResult(False, "That skill isn't available.", INSIDE_PROJECT)
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
        self._track(proc)
        try:
            out, err, timed_out = communicate(
                proc, stdin, timeout, self._kill, reap_timeout=REAP_TIMEOUT_S
            )
        finally:
            self._track(None)
        if timed_out:
            self._log_stderr(slug, f"timed out after {timeout} s", err)
            return SkillResult(False, "The skill took too long and was stopped.", TIMED_OUT)
        code = proc.returncode
        if code != 0:
            summary = f"failed (exit {code})"
            self._log_stderr(slug, summary, err)
            return SkillResult(False, f"The skill {summary}.", summary)
        if skill.manifest.pages:
            return self._pages_result(slug, out)
        return SkillResult(True, self._result_text(slug, out), "ok")

    def _pages_result(self, slug: str, out: bytes | None) -> SkillResult:
        """A pages skill's JSON output: its text (capped and wrapped as for plain output)
        and its checked pages; or failed (bad output), with only a short reason logged."""
        try:
            text, raw_pages = parse_pages_output(out or b"")
        except BadOutput as exc:
            self._log.warning("Skills: %s gave bad output (%s)", slug, exc)
            return SkillResult(False, f"The skill {BAD_OUTPUT}.", BAD_OUTPUT)
        pages, dropped = check_pages(raw_pages)
        if dropped:
            self._log.warning("Skills: %s dropped %d pages", slug, dropped)
        return SkillResult(True, self._wrap_text(slug, text), "ok", pages)

    def _log_stderr(self, slug: str, what: str, err: bytes | None) -> None:
        tail = _decode(err)[-STDERR_LOG_LIMIT:].strip() if err else ""
        if tail:
            self._log.warning("Skills: %s %s; stderr: %s", slug, what, tail)
        else:
            self._log.warning("Skills: %s %s", slug, what)

    @staticmethod
    def _result_text(slug: str, out: bytes | None) -> str:
        return SkillRunner._wrap_text(slug, _decode((out or b"")[:STDOUT_READ_LIMIT]))

    @staticmethod
    def _wrap_text(slug: str, text: str) -> str:
        """Text for Claude: wrapped as untrusted and capped, or "Done." when empty."""
        text = text.replace("\r\n", "\n").strip()
        if not text:
            return DONE
        source = f"skill:{slug}"
        overhead = len(wrap(source, ""))
        # The cap is on the escaped body, so anything past STDOUT_LIMIT characters (or
        # past it once escaped) is cut and marked "(and more)".
        return wrap(source, text, limit=STDOUT_LIMIT + overhead)


class BadOutput(ValueError):
    """A pages skill's stdout isn't the JSON it must be; the message is a short reason for
    the log (never any of the output)."""


def parse_pages_output(out: bytes) -> tuple[str, list[dict[str, str]]]:
    """A pages skill's stdout: (text, [{"title", "url"}, ...]) as given, or BadOutput.
    The shape is strict: one object with exactly "text" (a string) and "pages" (a list,
    maybe empty, of objects with exactly a "title" and a "url" string)."""
    if len(out) > PAGES_READ_LIMIT:
        raise BadOutput(f"over {PAGES_READ_LIMIT} bytes")
    try:
        raw = json.loads(_decode(out).removeprefix("\ufeff"))
    except RecursionError:
        raise BadOutput("not valid JSON (nested too deeply)") from None
    except ValueError:
        raise BadOutput("not valid JSON") from None
    if not isinstance(raw, dict):
        raise BadOutput("not a JSON object")
    if set(raw) != OUTPUT_KEYS:
        raise BadOutput('needs exactly the keys "text" and "pages"')
    text, pages = raw["text"], raw["pages"]
    if not isinstance(text, str):
        raise BadOutput('"text" is not a string')
    if not isinstance(pages, list):
        raise BadOutput('"pages" is not a list')
    for page in pages:
        if not isinstance(page, dict) or set(page) != PAGE_KEYS:
            raise BadOutput('each page needs exactly "title" and "url"')
        if not isinstance(page["title"], str) or not isinstance(page["url"], str):
            raise BadOutput("a page's title and url must be strings")
    return text, pages


def check_pages(pages: list[dict[str, str]]) -> tuple[tuple[Page, ...], int]:
    """The pages that may be opened, in order, as (title, site, address), and how many
    were dropped: a bad or non-public address, one already listed, or any past MAX_PAGES.
    Titles are cleaned like search results' titles and cut to MAX_PAGE_TITLE."""
    kept: list[Page] = []
    seen: set[str] = set()
    for page in pages:
        url = page["url"]
        if len(kept) >= MAX_PAGES or url in seen:
            continue
        try:
            check_url(url)
        except ValueError:
            continue
        if not is_public_host(url):
            continue
        seen.add(url)
        title = plain_line(page["title"])[:MAX_PAGE_TITLE].rstrip()
        kept.append((title or "Untitled", site_name(url), url))
    return tuple(kept), len(pages) - len(kept)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
