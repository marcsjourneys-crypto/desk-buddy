"""The spoken "yes": powerful actions wait for the user to confirm them out loud.

1. **Proposed:** a tool that needs a yes doesn't run when Claude calls it. The ActionGuard
   counts and logs the attempt, then hands the checked action here (one at a time: a new
   one replaces the old), stamped with the question it was proposed in (QUESTION, a
   context variable the loop sets for each turn, which tool threads inherit).
2. **Asked:** the chat ends the turn on the waiting tool result, and the loop itself
   (never the model) speaks "Shall I open <it>?". The action is armed as soon as that
   line starts playing, so the user may cut it off to answer. If the reply was cut off
   or failed before the question started, the action is cancelled.
3. **Answered:** at the very next question with speech, before Claude sees it, the loop
   passes the transcript to answer(). A yes within EXPIRY_S of the question runs it,
   anything else cancels it, a late answer finds it expired.
4. **Told:** whenever an action ends without Claude knowing (cancelled before the user
   was asked, replaced, dropped by forget or computer control going off), a system note
   says nothing was opened, once, with the next answer(). A bare yes with nothing
   waiting gets a note too.

Only the user's voice can confirm: there is no confirm tool, and nothing Claude, a tool
result, a web page or a note says can approve an action. The confirmed run isn't counted
by the guard again. Each step is logged ("Action: <tool> <args> -> ...") and shown on the
dashboard (waiting, then ok, failed, cancelled or expired). Claude learns what happened
from a system note on the user's message.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from desk_buddy.tools.actions import publish_action
from desk_buddy.tools.registry import ToolError

log = logging.getLogger(__name__)

EXPIRY_S = 60.0
MAX_YES_WORDS = 6
YES_WORDS = frozenset({"yes", "yeah", "yep", "yup", "sure", "ok", "okay"})
YES_PHRASES = (("go", "ahead"), ("do", "it"), ("open", "it"), ("please", "do"))
# Words a yes may also hold. Every word of a yes must be a yes word or one of these, so
# "yeah, nah", "okay, never mind" and "sure, hang on" are not a yes.
FILLER_WORDS = frozenset(
    {
        "please",
        "carl",
        "karl",  # Whisper's spelling of the name
        "thanks",
        "thank",
        "you",
        "it",
        "go",
        "ahead",
        "do",
        "open",
        "that",
        "alright",
        "right",
        "oh",
    }
)
NO_WORDS = frozenset({"no", "nope", "don't", "dont", "not", "stop", "cancel", "wait"})

# The question (a number from the loop) the current turn answers. Set by the loop in each
# turn's task; tool threads started from it inherit it, so a late proposal is stamped
# with the question it came from, not whichever one is running when it lands.
QUESTION: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "desk_buddy_question", default=None
)


def is_yes(text: str) -> bool:
    """A yes: at most MAX_YES_WORDS words (lowercased, punctuation removed), at least one
    yes word or phrase, every word a yes word or a filler, and no "no" word."""
    plain = text.lower().replace("’", "'").replace("‘", "'")
    words = re.sub(r"[^\w\s]", "", plain).split()
    if not words or len(words) > MAX_YES_WORDS:
        return False
    if any(w in NO_WORDS for w in words):
        return False
    if not all(w in YES_WORDS or w in FILLER_WORDS for w in words):
        return False
    if any(w in YES_WORDS for w in words):
        return True
    pairs = set(zip(words, words[1:], strict=False))
    return any(phrase in pairs for phrase in YES_PHRASES)


@dataclass(frozen=True)
class PendingAction:
    """An action that has passed every check and waits for a yes."""

    tool: str
    log_args: str  # for the log line
    public_args: str  # for the dashboard
    description: str  # what it does, to say and note: "the page on bbc.co.uk"
    run: Callable[[], str]  # raises ToolError if it fails
    question: int | None = None  # the question it was proposed in
    armed: bool = False  # the server's question about it has started playing
    at: float = 0.0  # when it was armed (Confirmations' clock)

    @property
    def ask(self) -> str:
        """The question the server speaks: "Shall I open the page on bbc.co.uk?"."""
        return f"Shall I open {self.description}?"


class Confirmations:
    """Holds at most one pending action. Thread-safe: actions are proposed from tool
    threads and asked about and answered from the loop."""

    def __init__(
        self,
        *,
        bus: Any = None,
        clock: Callable[[], float] = time.monotonic,
        expiry: float = EXPIRY_S,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._expiry = expiry
        self._lock = threading.Lock()
        self._pending: PendingAction | None = None
        # Notes for Claude about actions that ended without it being told yet (cancelled
        # before the user was asked, replaced, dropped): delivered once, with the next
        # answer(). Claude was told each one was waiting, so it must learn it never ran.
        self._unreported: list[str] = []

    @property
    def pending(self) -> PendingAction | None:
        with self._lock:
            return self._pending

    def propose(self, action: PendingAction) -> None:
        """Hold `action` (unarmed), stamped with the current QUESTION. A new one replaces
        (cancels) the old."""
        stamped = dataclasses.replace(action, question=QUESTION.get(), armed=False, at=0.0)
        with self._lock:
            old, self._pending = self._pending, stamped
        if old is not None:
            self._outcome(
                old, "cancelled", "Replaced by a newer request.", "was replaced by a newer one"
            )

    def to_ask(self, question: int) -> PendingAction | None:
        """The action proposed in `question` that the server should now ask about, if
        any. An unarmed one from another question (a late tool thread) is cancelled."""
        with self._lock:
            action = self._pending
            if action is None or action.armed:
                return None
            if action.question == question:
                return action
            self._pending = None
        self._outcome(action, "cancelled", "It was never asked about.", NOT_ASKED)
        return None

    def arm(self, action: PendingAction, question: int) -> bool:
        """The question about `action` has started playing: from now, only the answer at
        question + 1, within the expiry time from now, counts (also if the user cuts the
        question off to answer). False if it was replaced meanwhile."""
        with self._lock:
            if self._pending is not action:
                return False
            self._pending = dataclasses.replace(
                action, armed=True, question=question, at=self._clock()
            )
        return True

    def cancel_unarmed(self, reason: str) -> None:
        """Cancel an action that was never asked about (the reply was interrupted or
        failed before the question started). An armed one is left alone."""
        with self._lock:
            action = self._pending
            if action is None or action.armed:
                return
            self._pending = None
        self._outcome(action, "cancelled", reason, NOT_ASKED)

    def answer(self, text: str, question: int) -> str | None:
        """The user's words at `question` (the transcript, nothing else): run, cancel or
        expire the pending action. Returns the system notes for Claude (any not yet
        reported first), or None. Blocking (the action may open a page): call it off the
        event loop."""
        with self._lock:
            action, self._pending = self._pending, None
            earlier, self._unreported = self._unreported, []
        note = self._answer(action, text, question)
        return " ".join(n for n in (*earlier, note) if n) or None

    def _answer(self, action: PendingAction | None, text: str, question: int) -> str | None:
        if action is None:
            if is_yes(text):
                return "(System note: nothing was waiting for a yes, so nothing was opened.)"
            return None
        what = action.description
        if not action.armed:
            self._outcome(action, "cancelled", "It was never asked about.")
            return cancelled_note(what, NOT_ASKED)
        if action.question != question - 1 or self._clock() - action.at > self._expiry:
            self._outcome(action, "expired", "The user didn't answer in time.")
            return (
                f"(System note: the pending action, opening {what}, expired before the "
                "user answered; nothing was opened.)"
            )
        if not is_yes(text):
            self._outcome(action, "cancelled", "The user didn't say yes.")
            return (
                f"(System note: the pending action, opening {what}, was cancelled; the "
                "user didn't say yes.)"
            )
        try:
            result = action.run()
        except ToolError as exc:
            self._outcome(action, "failed", str(exc))
            return failed_note(what)
        except Exception:
            log.exception("Confirmed action %s crashed", action.tool)
            self._outcome(action, "failed", "The action failed unexpectedly.")
            return failed_note(what)
        self._outcome(action, "ok", result)
        return f"(System note: the server asked and the user said yes; {what} was opened.)"

    def expire(self, reason: str, why: str = "was dropped") -> None:
        """Drop the pending action (a forget command, computer control turned off).
        `why` finishes the note Claude gets: "the request to open … <why>; nothing was
        opened"."""
        with self._lock:
            action, self._pending = self._pending, None
        if action is not None:
            self._outcome(action, "expired", reason, why)

    def _outcome(
        self, action: PendingAction, outcome: str, result: str, unreported: str | None = None
    ) -> None:
        """Log and show what happened. `unreported`: Claude hasn't been told; the note
        (cancelled_note with this cause) is delivered with the next answer()."""
        line = " ".join(str(result).split())
        log.info("Action: %s %s -> %s: %s", action.tool, action.log_args, outcome, line)
        publish_action(self._bus, action.tool, action.public_args, result, outcome)
        if unreported is not None:
            with self._lock:
                self._unreported.append(cancelled_note(action.description, unreported))


NOT_ASKED = "was cancelled before the user was asked"


def cancelled_note(what: str, why: str) -> str:
    """(System note: the request to open <what> <why>; nothing was opened.)"""
    return f"(System note: the request to open {what} {why}; nothing was opened.)"


def failed_note(what: str) -> str:
    return f"(System note: the server asked and the user said yes, but {what} couldn't be opened.)"
