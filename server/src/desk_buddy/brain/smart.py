"""When a question goes to the smart model: a spoken phrase, or the 2-minute stay after
a smart answer. The handover by Haiku itself lives in llm.py (the think_harder tool)."""

import re
import time
from collections.abc import Callable

STAY_SECONDS = 120.0  # after a smart answer, the next question within this stays smart

# Word sequences that ask for the smart model, anywhere in what the user said.
SMART_PHRASES: tuple[tuple[str, ...], ...] = (
    ("think", "hard"),
    ("think", "harder"),
    ("think", "carefully"),
    ("think", "deeply"),
    ("think", "it", "through"),
    ("big", "brain"),
)
_WORD = re.compile(r"[a-z0-9']+")

# Why a reply went to (or tried) the smart model, as shown on the Live tab.
ASKED = "asked"
HANDED_OVER = "handed over"
FOLLOW_ON = "follow-on"
UNAVAILABLE = "unavailable"


def wants_smart(text: str) -> bool:
    """True if one of SMART_PHRASES appears as whole words, in any case."""
    words = _WORD.findall(text.lower())
    return any(
        tuple(words[i : i + len(phrase)]) == phrase
        for phrase in SMART_PHRASES
        for i in range(len(words) - len(phrase) + 1)
    )


class SmartRouter:
    """Remembers the stay between turns. Owned by the loop (the chat is rebuilt per turn)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: float | None = None

    def route(self, text: str) -> str | None:
        """ASKED for a phrase, FOLLOW_ON within the stay, otherwise None (Haiku)."""
        if wants_smart(text):
            return ASKED
        if self._until is not None and self._clock() < self._until:
            return FOLLOW_ON
        return None

    def answered(self, smart: bool) -> None:
        """After a reply (also one cut off): a smart one restarts the stay, a Haiku one ends it."""
        self._until = self._clock() + STAY_SECONDS if smart else None

    def forget(self) -> None:
        self._until = None
