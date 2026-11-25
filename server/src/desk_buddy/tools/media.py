"""media_control: the keyboard's media keys (play/pause, next, previous, stop) for
whatever is playing, like a video in the browser or a podcast app.

The model picks an action from an enum; the server maps it to a fixed virtual-key code
and presses it with SendInput (ctypes, Windows only): one key down, one key up, both
flagged as extended keys, as a real media keyboard sends them. The sender is injectable,
so tests never press a real key. Every call goes through the ActionGuard.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable, Sequence
from ctypes import wintypes
from typing import Any

from desk_buddy.tools.registry import Tool, ToolError

log = logging.getLogger(__name__)

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

KEYS = {
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "stop": VK_MEDIA_STOP,
}
ACTIONS = tuple(KEYS)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

DONE = "Done."  # never a claim about whether something is now playing or paused
FAILED = "I couldn't press that key."
BAD_ACTION = f"action must be one of {', '.join(ACTIONS)}."

# One key event: (virtual-key code, flags).
KeyEvent = tuple[int, int]
# Sends key events in order; raises OSError if Windows didn't take them all.
Sender = Callable[[Sequence[KeyEvent]], None]


# --- SendInput's structures (winuser.h). The union needs its largest member, MOUSEINPUT,
# for INPUT to have the size SendInput checks (40 bytes on 64-bit Windows, 28 on 32-bit).


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def key_press(vk: int) -> list[KeyEvent]:
    """One press of a media key: down, then up, both extended."""
    return [(vk, KEYEVENTF_EXTENDEDKEY), (vk, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)]


def make_inputs(events: Sequence[KeyEvent]) -> ctypes.Array[INPUT]:
    """The INPUT array SendInput takes for these key events."""
    inputs = (INPUT * len(events))()
    for item, (vk, flags) in zip(inputs, events, strict=True):
        item.type = INPUT_KEYBOARD
        item.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inputs


def send_input(events: Sequence[KeyEvent]) -> None:
    """Press keys for real with user32's SendInput (Windows only)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    send = user32.SendInput
    send.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    send.restype = wintypes.UINT
    inputs = make_inputs(events)
    sent = send(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent} of {len(inputs)} key events")


class MediaKeys:
    """The media_control tool, through the ActionGuard."""

    def __init__(self, guard: Any, *, sender: Sender | None = None) -> None:
        """`sender`: None means send_input, looked up at each press."""
        self._guard = guard
        self._sender = sender

    def media_control(self, action: str) -> str:
        if action not in KEYS:
            self._guard.refuse("media_control", {"action": str(action)[:40]}, BAD_ACTION)
        return self._guard.run("media_control", {"action": action}, lambda: self._press(action))

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="media_control",
                description=(
                    "Press a media key on the user's computer for whatever is playing "
                    "there (a video in the browser, a podcast app, any player): play_pause "
                    "(one toggle: it pauses what plays and resumes what's paused), next, "
                    "previous or stop."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"action": {"type": "string", "enum": list(ACTIONS)}},
                    "required": ["action"],
                    "additionalProperties": False,
                },
                func=self.media_control,
            )
        ]

    def _press(self, action: str) -> str:
        try:
            (self._sender or send_input)(key_press(KEYS[action]))
        except Exception as exc:  # any failure to send, not only OSError
            log.warning("Couldn't press the %s media key: %s", action, exc)
            raise ToolError(FAILED) from exc
        return DONE
