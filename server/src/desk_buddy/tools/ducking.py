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
        self._lock = threading.Lock()
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

    def take_saved(self) -> int | None:
        """For a volume the user sets while ducked: the level before ducking, and no
        restore afterwards (it would undo their change)."""
        with self._lock:
            saved, self._saved = self._saved, None
        return saved[0] if saved else None

    def flush(self, timeout: float) -> bool:
        """Wait until queued duck/restore calls are done. False on timeout."""
        try:
            self._executor.submit(lambda: None).result(timeout=timeout)
        except (FutureTimeout, RuntimeError):
            return False
        return True

    def close(self) -> None:
        """Let a pending restore finish (bounded), then stop the worker."""
        self.flush(CLOSE_WAIT_S)
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False)

    # --- on the worker thread ---

    def _apply(self) -> None:
        with self._lock:
            want, saved = self._want, self._saved
        try:
            if want and saved is None:
                self._duck()
            elif not want and saved is not None:
                self._control.call("volume", saved[0], device_id=saved[1])
                with self._lock:
                    if self._saved == saved:
                        self._saved = None
        except Exception as exc:
            log.warning("Music duck failed: %s", exc)

    def _duck(self) -> None:
        playback = self._control.call("current_playback") or {}
        device = playback.get("device") or {}
        volume = device.get("volume_percent")
        if not playback.get("is_playing") or not device.get("id") or not volume:
            return
        self._control.call("volume", max(1, round(volume * DUCK_FACTOR)), device_id=device["id"])
        with self._lock:
            self._saved = (int(volume), device["id"])
