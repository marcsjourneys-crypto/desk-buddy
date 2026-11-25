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

import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
SKIPPED_PROGRAMS = frozenset({"spotify.exe"})  # Spotify has MusicDucker (never twice)


class SessionDucker:
    """Duck other apps (YouTube in the browser, a podcast) while CARL isn't idle: each
    audio session in the Windows volume mixer, except CARL's own process and Spotify, goes
    to DUCK_FACTOR of its level; at idle each is put back to its exact earlier level,
    unless the user changed it in the meantime (then it's left alone).

    MUSIC_DUCK switches it (computer control isn't needed: ducking isn't an action); off
    restores at once. Sessions that start mid-turn aren't ducked. A session whose program
    can't be identified isn't ducked either.

    Best effort, like MusicDucker: all Core Audio calls run on one COM worker thread,
    never on the event loop; on_state never blocks or raises; each kind of failure is
    logged once as a warning. A failed restore is kept and tried again after the next turn.
    """

    def __init__(
        self,
        backend: Any = None,
        *,
        enabled: bool = True,
        own_pid: int | None = None,
        process_name: Callable[[int], str | None] | None = None,
        executor: Any = None,
    ) -> None:
        """backend: a winaudio.SessionBackend (the default), or a fake with sessions().
        executor: where the calls run (default: a winaudio COM worker)."""
        from desk_buddy.tools import winaudio

        self._backend = backend if backend is not None else winaudio.SessionBackend()
        self._process_name = process_name or winaudio.process_name
        self._executor = executor or winaudio.com_worker("duck-apps")
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._enabled = enabled
        self._lock = threading.Lock()  # guards _want, _saved, _enabled, _closed, _warned
        self._want = False
        self._saved: dict[str, tuple[float, float]] = {}  # session id: (before, ducked)
        self._warned: set[tuple[str, str]] = set()
        self._closed = False

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
        if closed:
            return
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

    # --- on the COM worker thread ---

    def _apply(self) -> None:
        with self._lock:
            want, saved = self._want, dict(self._saved)
        try:
            if want and not saved:
                self._duck()
            elif not want and saved:
                self._restore(saved)
        except Exception as exc:
            self._warn("list", exc)

    def _duck(self) -> None:
        for session in self._backend.sessions():
            if session.pid == self._own_pid or not self._may_duck(session.pid):
                continue
            try:
                before = session.get()
                if before <= 0:
                    continue
                session.set(before * DUCK_FACTOR)
                ducked = session.get()  # as the mixer stored it
            except Exception as exc:
                self._warn("duck", exc)
                continue
            with self._lock:
                self._saved[session.id] = (before, ducked)

    def _may_duck(self, pid: int) -> bool:
        try:
            name = self._process_name(pid)
        except Exception as exc:
            self._warn("name", exc)
            return False
        return name is not None and name.lower() not in SKIPPED_PROGRAMS

    def _restore(self, saved: dict[str, tuple[float, float]]) -> None:
        sessions = self._backend.sessions()  # if this fails, everything is kept for later
        for session in sessions:
            if session.id not in saved:
                continue
            before, ducked = saved[session.id]
            try:
                if abs(session.get() - ducked) <= LEVEL_MATCH:
                    session.set(before)
                else:
                    log.info("An app's volume was changed while ducked: left as it is")
            except Exception as exc:
                self._warn("restore", exc)
        with self._lock:
            for key in saved:  # done, or dropped: the session ended or failed to restore
                self._saved.pop(key, None)

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
