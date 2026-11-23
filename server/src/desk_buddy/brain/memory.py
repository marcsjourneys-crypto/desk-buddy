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
_LEADING_FILLERS = frozenset({"hey", "buddy", "okay", "ok", "please", "so", "um", "uh"})
_TRAILING_FILLERS = frozenset({"buddy", "please"})


class Conversation:
    """The last max_turns exchanges of (user text, Buddy's reply), oldest first."""

    def __init__(self, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self._turns: deque[tuple[str, str]] = deque(maxlen=max_turns)

    def add(self, user: str, reply: str) -> None:
        self._turns.append((user, reply))

    def forget(self) -> None:
        self._turns.clear()

    def messages(self) -> list[dict[str, str]]:
        """A fresh list in Messages API shape: user, assistant, user, assistant, ..."""
        out: list[dict[str, str]] = []
        for user, reply in self._turns:
            out.append({"role": "user", "content": user})
            out.append({"role": "assistant", "content": reply})
        return out

    def __len__(self) -> int:
        return len(self._turns)


def is_forget_command(text: str) -> bool:
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLERS:
        words.pop()
    return " ".join(words) in FORGET_PHRASES
