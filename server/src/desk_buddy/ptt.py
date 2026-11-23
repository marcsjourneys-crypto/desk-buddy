"""Push-to-talk: a global hold-to-talk key read with Win32 GetAsyncKeyState."""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32")
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short

# Virtual-key codes: https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
VK_CODES: dict[str, int] = {
    "right_ctrl": 0xA3,
    "left_ctrl": 0xA2,
    "right_alt": 0xA5,
    "right_shift": 0xA1,
    "scroll_lock": 0x91,
    "pause": 0x13,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def parse_key(name: str) -> int:
    """Map a key name (e.g. 'right_ctrl', 'f8') or hex code (e.g. '0xA3') to a virtual-key code."""
    key = name.strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    if key.startswith("0x"):
        try:
            code = int(key, 16)
        except ValueError:
            pass
        else:
            if 0 < code < 0x100:
                return code
    names = ", ".join(sorted(VK_CODES))
    raise ValueError(f"unknown key {name!r}; use one of {names}, or a hex code like 0xA3")


def is_key_pressed(vk: int) -> bool:
    """True while the key is physically held, whichever window has focus."""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class EdgeDetector:
    """Turns a stream of pressed/not-pressed readings into 'down'/'up' events."""

    def __init__(self) -> None:
        self._pressed = False

    def update(self, pressed: bool) -> str | None:
        if pressed == self._pressed:
            return None
        self._pressed = pressed
        return "down" if pressed else "up"


class KeyWatcher:
    """Polls one key on a daemon thread and calls on_event('down' | 'up') on changes.

    on_event runs on the watcher thread; use loop.call_soon_threadsafe to reach asyncio.
    """

    def __init__(
        self,
        vk: int,
        on_event: Callable[[str], None],
        *,
        read_key: Callable[[int], bool] = is_key_pressed,
        interval: float = 0.02,
    ) -> None:
        self._vk = vk
        self._on_event = on_event
        self._read_key = read_key
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ptt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _run(self) -> None:
        detector = EdgeDetector()
        while not self._stop.wait(self._interval):
            try:
                event = detector.update(self._read_key(self._vk))
                if event:
                    self._on_event(event)
            except Exception:
                log.exception("push-to-talk poll failed")
