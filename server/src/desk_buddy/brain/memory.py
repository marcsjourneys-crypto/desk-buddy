"""Conversation memory until the server restarts, and the spoken "forget" command.

Each turn keeps what the user said, the tool calls CARL made (as real API blocks: an
assistant message of tool_use blocks, then a user message of their tool_result blocks,
per round) and what the user heard. So on later turns Claude sees what it actually did.
A turn is kept or dropped whole, so trimming never leaves a tool_use without its result.
"""

from __future__ import annotations

import json
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
# Put at the start of the user's next message when CARL ran tools but was cut off (or
# failed) before saying anything: the actions happened, so Claude must know about them.
CUT_OFF_NOTE_START = "(I cut you off before you replied, after you ran:"
MAX_NOTE_RESULT = 160  # characters of each result in that note

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
    reply: str  # what the user heard
    interrupted: bool
    rounds: tuple[tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]], ...]
    note: str | None = None  # a cut-off note that went in front of this user message


class Conversation:
    """The last max_turns exchanges, oldest first: user text, tool calls, CARL's reply."""

    def __init__(self, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self._turns: deque[_Turn] = deque(maxlen=max_turns)
        self._pending: list[str] = []  # actions from cut-off turns, for the next message

    def add(
        self,
        user: str,
        reply: str | None,
        *,
        interrupted: bool = False,
        tool_rounds: Iterable[ToolRound] = (),
    ) -> None:
        """Keep one exchange. Tool results are capped at MAX_STORED_RESULT characters
        (an untrusted block stays well formed).

        A turn with no reply is never stored as such (a history must not end a turn on
        tool results, and made-up assistant text would be copied). Its tool calls, which
        did happen, become a note in front of the next user message instead."""
        rounds = tuple(
            (tuple(dict(u) for u in uses), tuple(_compact(r) for r in results))
            for uses, results in tool_rounds
        )
        if not reply:
            self._pending += [
                _action(use, res)
                for uses, results in rounds
                for use, res in zip(uses, results, strict=False)
            ]
            return
        note, self._pending = self._cut_off_note(), []
        self._turns.append(_Turn(user, reply, interrupted, rounds, note))

    def forget(self) -> None:
        self._turns.clear()
        self._pending = []

    def user_message(self, text: str) -> str:
        """The next user message as sent to Claude: noted if the last reply was cut off,
        or if CARL ran tools and was cut off before replying."""
        note = self._cut_off_note()
        if note:
            return f"{note} {text}"
        return _noted(text, bool(self._turns) and self._turns[-1].interrupted)

    def _cut_off_note(self) -> str | None:
        if not self._pending:
            return None
        return f"{CUT_OFF_NOTE_START} {'; '.join(self._pending)}.)"

    def messages(self) -> list[dict[str, Any]]:
        """A fresh list in Messages API shape: user, assistant, user, assistant, ...

        A turn with tools adds, per round, an assistant message of tool_use blocks and a
        user message of their tool_result blocks, before the reply. Every stored turn
        ends with the reply, so no tool call is ever left dangling.

        An interruption is noted on the user message that follows the cut-off reply,
        never in the reply itself: Claude copies markers it sees in its own replies.
        """
        out: list[dict[str, Any]] = []
        previous_cut = False
        for turn in self._turns:
            text = f"{turn.note} {turn.user}" if turn.note else _noted(turn.user, previous_cut)
            out.append({"role": "user", "content": text})
            for uses, results in turn.rounds:
                out.append({"role": "assistant", "content": [dict(u) for u in uses]})
                out.append({"role": "user", "content": [dict(r) for r in results]})
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


def _action(use: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """One tool call, compact: 'play_music {"query": "Queen"} -> Started playing ...'.
    The result keeps any untrusted block well formed."""
    args = json.dumps(use.get("input") or {}, ensure_ascii=False, sort_keys=True, default=str)
    if len(args) > MAX_NOTE_RESULT:
        args = args[:MAX_NOTE_RESULT] + "..."
    outcome = shorten(str(result.get("content", "")), MAX_NOTE_RESULT)
    if result.get("is_error"):
        outcome = f"failed: {outcome}"
    return f"{use.get('name', '?')} {args} -> {outcome}"


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
