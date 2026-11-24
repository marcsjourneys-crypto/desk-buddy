"""Conversation memory until the server restarts, and the spoken "forget" command."""

from __future__ import annotations

import re
from collections import deque

# Matched after normalising: lowercase, punctuation removed ("let's" -> "lets"),
# leading/trailing fillers dropped. Whisper's spelling varies, so extend here.
FORGET_PHRASES = frozenset(
    {
        "forget that",
        "forget it",
        "forget everything",
        "new topic",
        "start over",
        "lets start over",
        "clear your memory",
        "change the subject",
    }
)
# Put at the start of the user's next message when they cut Buddy's reply off.
INTERRUPTED_NOTE = "(I interrupted you, so I only heard your reply up to that point.)"

_LEADING_FILLERS = frozenset({"hey", "buddy", "okay", "ok", "please", "so", "um", "uh"})
_TRAILING_FILLERS = frozenset({"buddy", "please"})


class Conversation:
    """The last max_turns exchanges of (user text, Buddy's reply), oldest first."""

    def __init__(self, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        # (user text, Buddy's reply as heard, whether the user cut that reply off)
        self._turns: deque[tuple[str, str, bool]] = deque(maxlen=max_turns)

    def add(self, user: str, reply: str, *, interrupted: bool = False) -> None:
        self._turns.append((user, reply, interrupted))

    def forget(self) -> None:
        self._turns.clear()

    def user_message(self, text: str) -> str:
        """The next user message as sent to Claude: noted if the last reply was cut off."""
        return _noted(text, bool(self._turns) and self._turns[-1][2])

    def messages(self) -> list[dict[str, str]]:
        """A fresh list in Messages API shape: user, assistant, user, assistant, ...

        An interruption is noted on the user message that follows the cut-off reply,
        never in the reply itself: Claude copies markers it sees in its own replies.
        """
        out: list[dict[str, str]] = []
        previous_cut = False
        for user, reply, cut in self._turns:
            out.append({"role": "user", "content": _noted(user, previous_cut)})
            out.append({"role": "assistant", "content": reply})
            previous_cut = cut
        return out

    def __len__(self) -> int:
        return len(self._turns)


def _noted(text: str, after_interruption: bool) -> str:
    return f"{INTERRUPTED_NOTE} {text}" if after_interruption else text


def is_forget_command(text: str) -> bool:
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLERS:
        words.pop()
    return " ".join(words) in FORGET_PHRASES
