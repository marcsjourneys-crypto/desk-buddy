"""Duck the music while CARL listens, thinks and talks (MUSIC_DUCK, on by default).

Driven by the orchestrator's state: anything but idle ducks Spotify to DUCK_FACTOR of its
level if music is playing; idle restores it. Every way a turn ends (reply, interrupt,
error, reset, the farewell) goes back to idle, so the restore always comes.

Best effort: Spotify is called on one worker thread (never on the event loop, so
listening is never delayed), with a short request timeout; a failure is a logged warning
and never touches the turn. A failed restore is kept and tried again after the next turn.
"""

from __future__ import annotations

import logging
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
    def __init__(self, control: Any) -> None:
        self._control = control
        self._lock = threading.Lock()  # guards _want, _saved and _closed
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
            want = str(state) != "idle"
            with self._lock:
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


def _definitely_failed(exc: BaseException) -> bool:
    """Spotify answered with an error, so nothing changed. A timeout or dropped connection
    is not definite: the change may have landed."""
    from spotipy.exceptions import SpotifyException

    return isinstance(exc.__cause__, SpotifyException) or isinstance(exc, SpotifyException)
