"""Conversation memory until the server restarts, and the spoken "forget" command.

Each turn keeps what the user said, the tool calls CARL made (as real API blocks: an
assistant message of tool_use blocks, then a user message of their tool_result blocks,
per round) and what the user heard. So on later turns Claude sees what it actually did.
A turn is kept or dropped whole, so trimming never leaves a tool_use without its result.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from desk_buddy.tools.untrusted import shorten

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
# Put at the start of the user's next message when they cut CARL's reply off.
INTERRUPTED_NOTE = "(I interrupted you, so I only heard your reply up to that point.)"

# "karl": Whisper often spells the name that way.
_LEADING_FILLERS = frozenset(
    {"hey", "carl", "karl", "buddy", "okay", "ok", "please", "so", "um", "uh"}
)
_TRAILING_FILLERS = frozenset({"carl", "karl", "buddy", "please"})


MAX_STORED_RESULT = 300  # characters of each remembered tool result

# One round of tool calls: the tool_use blocks Claude sent, and their tool_result blocks.
ToolRound = tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class _Turn:
    user: str
    reply: str | None  # what the user heard; None if nothing (cut off, or failed)
    interrupted: bool
    rounds: tuple[tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]], ...]


class Conversation:
    """The last max_turns exchanges, oldest first: user text, tool calls, CARL's reply."""

    def __init__(self, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self._turns: deque[_Turn] = deque(maxlen=max_turns)

    def add(
        self,
        user: str,
        reply: str | None,
        *,
        interrupted: bool = False,
        tool_rounds: Iterable[ToolRound] = (),
    ) -> None:
        """Keep one exchange. Tool results are capped at MAX_STORED_RESULT characters
        (an untrusted block stays well formed)."""
        rounds = tuple(
            (tuple(dict(u) for u in uses), tuple(_compact(r) for r in results))
            for uses, results in tool_rounds
        )
        self._turns.append(_Turn(user, reply or None, interrupted, rounds))

    def forget(self) -> None:
        self._turns.clear()

    def user_message(self, text: str) -> str:
        """The next user message as sent to Claude: noted if the last reply was cut off."""
        return _noted(text, bool(self._turns) and self._turns[-1].interrupted)

    def messages(self) -> list[dict[str, Any]]:
        """A fresh list in Messages API shape: user, assistant, user, assistant, ...

        A turn with tools adds, per round, an assistant message of tool_use blocks and a
        user message of their tool_result blocks, before the reply. A turn with no reply
        ends on its tool results, which join the next user message (results first).

        An interruption is noted on the user message that follows the cut-off reply,
        never in the reply itself: Claude copies markers it sees in its own replies.
        """
        out: list[dict[str, Any]] = []
        previous_cut = False
        for turn in self._turns:
            out.append({"role": "user", "content": _noted(turn.user, previous_cut)})
            for uses, results in turn.rounds:
                out.append({"role": "assistant", "content": [dict(u) for u in uses]})
                out.append({"role": "user", "content": [dict(r) for r in results]})
            if turn.reply:
                out.append({"role": "assistant", "content": turn.reply})
            previous_cut = turn.interrupted
        return merge_consecutive(out)

    def __len__(self) -> int:
        return len(self._turns)


def merge_consecutive(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Join back-to-back messages with the same role into one, as content blocks in order
    (so a turn's leftover tool results come first in the next user message)."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if out and out[-1]["role"] == message["role"]:
            out[-1] = {
                "role": message["role"],
                "content": _blocks(out[-1]["content"]) + _blocks(message["content"]),
            }
        else:
            out.append(dict(message))
    return out


def _blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _compact(result: Mapping[str, Any]) -> dict[str, Any]:
    block = dict(result)
    if isinstance(block.get("content"), str):
        block["content"] = shorten(block["content"], MAX_STORED_RESULT)
    return block


def _noted(text: str, after_interruption: bool) -> str:
    return f"{INTERRUPTED_NOTE} {text}" if after_interruption else text


def is_forget_command(text: str) -> bool:
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLERS:
        words.pop()
    return " ".join(words) in FORGET_PHRASES
