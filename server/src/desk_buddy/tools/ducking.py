"""Duck the music while CARL listens, thinks and talks (MUSIC_DUCK, on by default):
MusicDucker lowers Spotify through its API, SessionDucker lowers every other app in the
Windows volume mixer.

Driven by the orchestrator's state: anything but idle ducks Spotify to DUCK_FACTOR of its
level if music is playing; idle restores it. Every way a turn ends (reply, interrupt,
error, reset, the farewell) goes back to idle, so the restore always comes.

Best effort: Spotify is called on one worker thread (never on the event loop, so
listening is never delayed), with a short request timeout; a failure is a logged warning
and never touches the turn. A failed restore is kept and tried again after the next turn.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DUCK_FACTOR = 0.2
DUCK_TIMEOUT_S = 2.0  # request timeout for the ducker's own Spotify client
CLOSE_WAIT_S = 3.0  # at shutdown, give a pending restore this long


class MusicDucker:
    def __init__(self, control: Any, *, enabled: bool = True) -> None:
        self._control = control
        self._enabled = enabled  # MUSIC_DUCK (and computer control) can change live
        self._lock = threading.Lock()  # guards _want, _saved, _enabled and _closed
        # Held for a whole duck or restore, and for a volume the user sets: they never
        # interleave, so the user's level always wins.
        self._volume_lock = threading.Lock()
        self._want = False  # what the latest state asks for
        self._saved: tuple[int, str] | None = None  # (volume before ducking, device id)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="duck")
        self._closed = False

    def on_state(self, state: Any) -> None:
        """Called with every state change, on the event loop. Never blocks or raises."""
        try:
            with self._lock:
                want = self._enabled and str(state) != "idle"
                if want == self._want or self._closed:
                    return
                self._want = want
            self._executor.submit(self._apply)
        except Exception:
            log.warning("Music duck failed to queue", exc_info=True)

    def set_enabled(self, enabled: bool, state: Any = "idle") -> None:
        """Turn ducking on or off live. Off restores the music now (through the same
        worker, so it can't race a duck in flight); on is primed with CARL's current
        state, so it ducks at once mid-turn. Never blocks or raises."""
        try:
            with self._lock:
                self._enabled = enabled
                want = enabled and str(state) != "idle"
                if want == self._want or self._closed:
                    return
                self._want = want
            self._executor.submit(self._apply)
        except Exception:
            log.warning("Music duck failed to queue", exc_info=True)

    def music_started(self) -> None:
        """A tool just started or resumed playback mid-turn: duck it now if CARL is still
        busy, so the reply isn't spoken over full-volume music. Never blocks or raises."""
        try:
            with self._lock:
                if not self._want or self._saved is not None or self._closed:
                    return
            self._executor.submit(self._apply, True)
        except Exception:
            log.warning("Music duck failed to queue", exc_info=True)

    def set_by_user(self, set_volume: Callable[[int | None], int]) -> int:
        """Run a volume change the user asked for. set_volume gets the level from before
        ducking (None if not ducked) and returns the level it set. Only once it succeeded
        is the restore dropped (it would undo the user's level); if it fails, the restore
        still comes."""
        with self._volume_lock:
            with self._lock:
                before = self._saved[0] if self._saved else None
            level = set_volume(before)
            with self._lock:
                self._saved = None
            return level

    def flush(self, timeout: float) -> bool:
        """Wait until queued duck/restore calls are done. False on timeout."""
        try:
            self._executor.submit(lambda: None).result(timeout=timeout)
        except (FutureTimeout, RuntimeError):
            return False
        return True

    def close(self) -> None:
        """Restore the music whatever state CARL was in (shutdown can come mid-turn, or
        after a farewell that timed out), wait for it (bounded), then stop the worker."""
        with self._lock:
            closed, self._closed = self._closed, True
            self._want = False
        if closed:
            return
        try:
            self._executor.submit(self._apply)
        except RuntimeError:
            pass
        self.flush(CLOSE_WAIT_S)
        self._executor.shutdown(wait=False)

    # --- on the worker thread ---

    def _apply(self, just_started: bool = False) -> None:
        with self._volume_lock:
            with self._lock:
                want, saved = self._want, self._saved
            try:
                if want and saved is None:
                    self._duck(just_started)
                elif not want and saved is not None:
                    self._control.call("volume", saved[0], device_id=saved[1])
                    with self._lock:
                        if self._saved == saved:
                            self._saved = None
            except Exception as exc:
                log.warning("Music duck failed: %s", exc)

    def _duck(self, just_started: bool) -> None:
        playback = self._control.call("current_playback") or {}
        device = playback.get("device") or {}
        volume = device.get("volume_percent")
        # Right after a tool started the music, Spotify may not report it as playing yet.
        playing = playback.get("is_playing") or just_started
        if not playing or not device.get("id") or not volume:
            return
        saved = (int(volume), device["id"])
        with self._lock:
            # Recorded first: a duck that lands but times out must still be restored.
            self._saved = saved
        try:
            self._control.call("volume", max(1, round(volume * DUCK_FACTOR)), device_id=saved[1])
        except Exception as exc:
            if _definitely_failed(exc):  # Spotify answered no: the volume didn't change
                with self._lock:
                    if self._saved == saved:
                        self._saved = None
            raise


LEVEL_MATCH = 0.005  # mixer levels this close count as unchanged (they're float32)
SPOTIFY = "spotify.exe"
MAX_RESTORE_TRIES = 3  # idles a failed (or app-gone) restore is tried at, then dropped
STARTED_CHECKS_S = (0.0, 1.5, 4.0)  # after a tool started something: look for new sessions
SESSION_ACTIVE = 1  # winaudio.SESSION_ACTIVE (not imported: no COM module needed here)


def ducked_apps_path() -> Path:
    """%LOCALAPPDATA%\\desk-buddy\\ducked_apps.json: what's ducked right now, if anything."""
    from desk_buddy.config import app_data_dir

    return app_data_dir() / "ducked_apps.json"


@dataclass
class _Ducked:
    """One app we ducked, by its session identifier and program (stable while it plays,
    and across a session restart): the (before, ducked) levels of each of its sessions."""

    session: str
    program: str
    levels: list[list[float]] = field(default_factory=list)
    tries: int = 0  # restores that failed so far

    def matches(self, level: float) -> float | None:
        """The level from before ducking, if `level` is one we set; else None."""
        return next((b for b, d in self.levels if abs(level - d) <= LEVEL_MATCH), None)


def _parse_ducked(raw: str) -> list[_Ducked]:
    """The saved file's entries; raises ValueError (or TypeError/KeyError) if it's bad."""
    entries = []
    for item in json.loads(raw)["apps"]:
        session, program = item["session"], item["program"]
        if not isinstance(session, str) or not isinstance(program, str):
            raise ValueError("bad entry")
        levels = [[float(b), float(d)] for b, d in item["levels"]]
        if not levels or not all(0.0 <= v <= 1.0 for pair in levels for v in pair):
            raise ValueError("bad levels")
        entries.append(_Ducked(session, program.lower(), levels))
    return entries


class SessionDucker:
    """Duck other apps (YouTube in the browser, a podcast) while CARL isn't idle: each
    active audio session in the Windows volume mixer, except CARL's own process (and
    Spotify while MusicDucker ducks it through its API), goes to DUCK_FACTOR of its level;
    at idle each is put back to its exact earlier level, unless the user changed it in
    the meantime (then it's left alone).

    - Apps are known by their session identifier and program, so a session that
      restarts mid-turn (a reloaded tab) is still put back.
    - What's ducked is kept in ducked_apps.json while it is, and put back at the next
      start after a crash.
    - started(): a tool just started something; new sessions are ducked too.
    - MUSIC_DUCK switches it (computer control isn't needed: ducking isn't an action);
      off restores at once. A session whose program can't be identified is left alone.

    Best effort, like MusicDucker: all Core Audio calls run on one COM worker thread,
    never on the event loop; on_state never blocks or raises; each kind of failure is
    logged once as a warning. A failed restore is tried again at the next idle (up to
    MAX_RESTORE_TRIES times).
    """

    def __init__(
        self,
        backend: Any = None,
        *,
        enabled: bool = True,
        own_pid: int | None = None,
        process_name: Callable[[int], str | None] | None = None,
        executor: Any = None,
        state_file: Path | None = None,
        started_checks: tuple[float, ...] = STARTED_CHECKS_S,
    ) -> None:
        """backend: a winaudio.SessionBackend (the default), or a fake with sessions().
        executor: where the calls run (default: a winaudio COM worker). state_file:
        default ducked_apps_path(). Puts back what a crash left ducked, first thing."""
        from desk_buddy.tools import winaudio

        self._backend = backend if backend is not None else winaudio.SessionBackend()
        self._process_name = process_name or winaudio.process_name
        self._executor = executor or winaudio.com_worker("duck-apps")
        self._state_file = state_file if state_file is not None else ducked_apps_path()
        self._started_checks = started_checks
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._enabled = enabled
        # Guards _want, _saved (and its entries), _enabled, _skip_spotify, _closed,
        # _timers and _warned.
        self._lock = threading.Lock()
        self._want = False
        self._skip_spotify = False
        self._saved: dict[tuple[str, str], _Ducked] = {}
        self._timers: list[threading.Timer] = []
        self._warned: set[tuple[str, str]] = set()
        self._closed = False
        try:
            self._executor.submit(self._recover)
        except Exception:
            log.warning("Ducking other apps couldn't check for a crash", exc_info=True)

    def on_state(self, state: Any) -> None:
        """Called with every state change, on the event loop. Never blocks or raises."""
        self._switch(lambda: self._enabled and str(state) != "idle")

    def set_enabled(self, enabled: bool, state: Any = "idle") -> None:
        """MUSIC_DUCK changed: off restores at once, on ducks at once mid-turn. Never
        blocks or raises."""

        def want() -> bool:
            self._enabled = enabled
            return enabled and str(state) != "idle"

        self._switch(want)

    def flush(self, timeout: float) -> bool:
        """Wait until queued ducks/restores are done. False on timeout."""
        try:
            self._executor.submit(lambda: None).result(timeout=timeout)
        except (FutureTimeout, RuntimeError):
            return False
        return True

    def close(self) -> None:
        """Restore every app whatever state CARL was in, wait for it (bounded), then stop
        the worker."""
        with self._lock:
            closed, self._closed = self._closed, True
            self._want = False
            timers, self._timers = self._timers, []
        if closed:
            return
        for timer in timers:
            timer.cancel()
        try:
            self._executor.submit(self._apply)
        except RuntimeError:
            pass
        self.flush(CLOSE_WAIT_S)
        self._executor.shutdown(wait=False)

    def _switch(self, decide: Callable[[], bool]) -> None:
        try:
            with self._lock:
                want = decide()
                if want == self._want or self._closed:
                    return
                self._want = want
            self._executor.submit(self._apply)
        except Exception:
            log.warning("Ducking other apps failed to queue", exc_info=True)

    def set_skip_spotify(self, skip: bool) -> None:
        """Leave Spotify's session alone: only while MusicDucker really ducks it through
        Spotify's API (so it's never lowered twice). Applies from the next duck."""
        with self._lock:
            self._skip_spotify = skip

    def started(self) -> None:
        """A tool just started something that may play (a video, a website, an app): if
        CARL is still busy, duck any new active session now and twice more shortly after
        (a browser takes a moment to start playing). Never blocks or raises."""
        try:
            with self._lock:
                if not self._want or self._closed:
                    return
                for delay in self._started_checks:
                    if delay > 0:
                        timer = threading.Timer(delay, self._recheck)
                        timer.daemon = True
                        self._timers = [t for t in self._timers if t.is_alive()] + [timer]
                        timer.start()
            if any(delay <= 0 for delay in self._started_checks):
                self._executor.submit(self._apply)
        except Exception:
            log.warning("Ducking other apps failed to queue", exc_info=True)

    def _recheck(self) -> None:
        try:
            with self._lock:
                if not self._want or self._closed:
                    return
            self._executor.submit(self._apply)
        except Exception:
            log.debug("Ducking other apps: a recheck failed to queue", exc_info=True)

    # --- on the COM worker thread ---

    def _apply(self) -> None:
        with self._lock:
            want, saved = self._want, bool(self._saved)
        try:
            if want:
                self._duck()
            elif saved:
                self._restore()
        except Exception as exc:
            self._warn("list", exc)

    def _duck(self) -> None:
        """Duck every active session that isn't already at a level we set. Several
        sessions of one app share a key: each one's levels are kept."""
        changed = False
        for session in self._backend.sessions():
            if session.state != SESSION_ACTIVE or session.pid == self._own_pid:
                continue
            program = self._program(session.pid)
            if program is None:
                continue
            key = (session.id, program)
            try:
                before = session.get()
                with self._lock:
                    if program == SPOTIFY and self._skip_spotify:
                        continue
                    entry = self._saved.get(key)
                    if entry is not None and entry.matches(before) is not None:
                        continue  # already ducked
                if before <= 0:
                    continue
                pair = [before, before * DUCK_FACTOR]
                with self._lock:  # recorded (and saved to disk) before anything changes
                    entry = self._saved.setdefault(key, _Ducked(session.id, program))
                    entry.levels.append(pair)
                changed = True
                self._save()
                session.set(pair[1])
                pair[1] = session.get()  # as the mixer stored it
            except Exception as exc:
                self._warn("duck", exc)
        if changed:
            self._save()

    def _program(self, pid: int) -> str | None:
        """The program's file name in lowercase; None (don't touch it) if unknown."""
        try:
            name = self._process_name(pid)
        except Exception as exc:
            self._warn("name", exc)
            return None
        return name.lower() if name else None

    def _restore(self) -> None:
        """Put each ducked app back, in every live session with its key, if its level is
        still one we set. An entry is done once its sessions were seen; one that failed,
        or whose app isn't playing now, is tried again at the next idle, up to
        MAX_RESTORE_TRIES times."""
        live: dict[tuple[str, str], list[Any]] = {}
        for session in self._backend.sessions():  # if this fails, all is kept for later
            if session.pid == self._own_pid:
                continue
            program = self._program(session.pid)
            if program is not None:
                live.setdefault((session.id, program), []).append(session)
        with self._lock:
            saved = dict(self._saved)
        done = []
        for key, entry in saved.items():
            failed = False
            for session in live.get(key, []):
                try:
                    before = entry.matches(session.get())
                    if before is not None:
                        session.set(before)
                    else:
                        log.info("%s's volume was changed while ducked: left as it is", key[1])
                except Exception as exc:
                    self._warn("restore", exc)
                    failed = True
            if key in live and not failed:
                done.append(key)
                continue
            entry.tries += 1
            if entry.tries >= MAX_RESTORE_TRIES:
                log.warning("Couldn't put %s's volume back: giving up", key[1])
                done.append(key)
        with self._lock:
            for key in done:
                if self._saved.get(key) is saved[key]:
                    del self._saved[key]
        self._save()

    def _save(self) -> None:
        """What's ducked, on disk (atomically), so a crash can be undone at the next
        start; the file is deleted once nothing is. Never fatal."""
        with self._lock:
            entries = [
                {"session": e.session, "program": e.program, "levels": e.levels}
                for e in self._saved.values()
            ]
        try:
            if not entries:
                self._state_file.unlink(missing_ok=True)
                return
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"apps": entries}), encoding="utf-8")
            os.replace(tmp, self._state_file)
        except Exception as exc:
            self._warn("save", exc)

    def _recover(self) -> None:
        """At startup: apps a crash left ducked are put back (if unchanged), then the file
        goes. Never fatal."""
        try:
            raw = self._state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except Exception as exc:
            self._warn("recover", exc)
            return
        try:
            loaded = _parse_ducked(raw)
        except (ValueError, TypeError, KeyError) as exc:
            self._warn("recover", exc)
            loaded = []
        with self._lock:
            for entry in loaded:
                self._saved.setdefault((entry.session, entry.program), entry)
        if loaded:
            log.info("Putting back the volume of %d app(s) left ducked last time", len(loaded))
        try:
            if loaded:
                self._restore()
            else:
                self._save()  # nothing usable in it: delete it
        except Exception as exc:
            self._warn("recover", exc)

    def _warn(self, what: str, exc: Exception) -> None:
        kind = (what, type(exc).__name__)
        with self._lock:
            first = kind not in self._warned
            self._warned.add(kind)
        if first:
            log.warning("Ducking other apps failed (%s): %s", what, exc)
        else:
            log.debug("Ducking other apps failed (%s): %s", what, exc)


def _definitely_failed(exc: BaseException) -> bool:
    """Spotify answered with an error, so nothing changed. A timeout or dropped connection
    is not definite: the change may have landed."""
    from spotipy.exceptions import SpotifyException

    return isinstance(exc.__cause__, SpotifyException) or isinstance(exc, SpotifyException)
