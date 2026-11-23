"""Emotion tags. Every reply starts with one, e.g. "[happy] Hi!"; it drives the eyes."""

from __future__ import annotations

import re

EMOTIONS = ("neutral", "happy", "sad", "surprised", "sleepy", "thinking", "curious")
DEFAULT_EMOTION = "neutral"

_TAG = re.compile(r"\[(" + "|".join(EMOTIONS) + r")\]", re.IGNORECASE)
_HOLD = max(len(e) for e in EMOTIONS) + 2  # length of the longest tag, "[surprised]"


class EmotionParser:
    """Splits a streamed reply into its emotion and the text to speak.

    The leading tag (after optional whitespace) sets `emotion` and is removed together
    with the whitespace after it. No tag, or an unknown one, means DEFAULT_EMOTION and
    the text passes through. Known tags later in the reply are removed so they are never
    spoken; other bracketed text such as "[1]" is kept. Text that could still turn into
    a tag is held back until it is decided. The output does not depend on chunking.
    """

    def __init__(self) -> None:
        self.emotion: str | None = None  # None until the leading tag is decided
        self._buf = ""
        self._skip_space = False

    def feed(self, text: str) -> str:
        self._buf += text
        if self.emotion is None and not self._decide_lead(final=False):
            return ""
        return self._release(final=False)

    def flush(self) -> str:
        if self.emotion is None:
            self._decide_lead(final=True)
        return self._release(final=True)

    def _decide_lead(self, *, final: bool) -> bool:
        head = self._buf.lstrip()
        if not final:
            if not head:
                return False
            if head.startswith("[") and "]" not in head and len(head) < _HOLD:
                return False  # could still become a tag
        match = _TAG.match(head)
        if match:
            self.emotion = match.group(1).lower()
            self._buf = head[match.end() :]
            self._skip_space = True
        else:
            self.emotion = DEFAULT_EMOTION
            self._buf = head
        return True

    def _release(self, *, final: bool) -> str:
        if self._skip_space:
            self._buf = self._buf.lstrip()
            self._skip_space = not self._buf
        text, hold = self._buf, ""
        if not final:
            i = text.rfind("[")
            if i != -1 and "]" not in text[i:] and len(text) - i < _HOLD:
                text, hold = text[:i], text[i:]
        self._buf = hold
        return _TAG.sub("", text)
