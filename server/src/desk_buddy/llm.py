"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence ends at . ! or ? (plus any closing quotes or brackets) followed by whitespace.
    Text at the end of the buffer is held until more arrives or flush() is called.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        sentences: list[str] = []
        while (cut := self._find_boundary()) is not None:
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _find_boundary(self) -> int | None:
        for match in _SENTENCE_END.finditer(self._buf):
            end = match.end()
            if end >= len(self._buf):
                return None  # wait for the next chunk to confirm
            if not self._buf[end].isspace():
                continue  # "3.5", "e.g" mid-token, "example.com"
            if match.group() == "." and self._last_word(match.start()) in _ABBREVIATIONS:
                continue
            return end
        return None

    def _last_word(self, pos: int) -> str:
        words = self._buf[:pos].split()
        return words[-1].lower().lstrip("\"'([") if words else ""
