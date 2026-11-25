"""The spoken "yes": powerful actions wait for the user to confirm them out loud.

A tool that needs a yes doesn't run when Claude calls it. The ActionGuard counts and logs
the attempt, then hands the checked action here, where it waits (one at a time: a new one
replaces the old). At the start of the next question, before Claude sees it, the loop
passes the transcript to answer():

- a yes (a short answer with a yes word and no "no" word) within EXPIRY_S runs it;
- anything else cancels it; a late answer finds it expired.

Only the user's voice can confirm: there is no confirm tool, and nothing Claude, a tool
result, a web page or a note says can approve an action. The confirmed run isn't counted
by the guard again. Each step is logged ("Action: <tool> <args> -> ...") and shown on the
dashboard (waiting, then ok, failed, cancelled or expired). Claude learns what happened
from a system note on the user's message.
"""

from __future__ import annotations

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
NO_WORDS = frozenset({"no", "nope", "don't", "dont", "not", "stop", "cancel", "wait"})


def is_yes(text: str) -> bool:
    """A yes: at most MAX_YES_WORDS words (lowercased, punctuation removed), a yes word or
    phrase, and no "no" word. "Yes but no", "no", "wait" and long answers aren't yes."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    if not words or len(words) > MAX_YES_WORDS:
        return False
    if any(w in NO_WORDS for w in words):
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
    at: float = 0.0  # when it was proposed (Confirmations' clock)


class Confirmations:
    """Holds at most one pending action. Thread-safe: actions are proposed from tool
    threads and answered from the loop."""

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

    @property
    def pending(self) -> PendingAction | None:
        with self._lock:
            return self._pending

    def propose(self, action: PendingAction) -> None:
        """Hold `action` until the next question. A new one replaces (cancels) the old."""
        stamped = PendingAction(
            action.tool,
            action.log_args,
            action.public_args,
            action.description,
            action.run,
            self._clock(),
        )
        with self._lock:
            old, self._pending = self._pending, stamped
        if old is not None:
            self._outcome(old, "cancelled", "Replaced by a newer request.")

    def answer(self, text: str) -> str | None:
        """The user's next words (the transcript, nothing else): run, cancel or expire the
        pending action. Returns the system note for Claude, or None if nothing waited.
        Blocking (the action may open a page): call it off the event loop."""
        with self._lock:
            action, self._pending = self._pending, None
        if action is None:
            return None
        what = action.description
        if self._clock() - action.at > self._expiry:
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
            return f"(System note: you asked and the user said yes, but {what} couldn't be opened.)"
        except Exception:
            log.exception("Confirmed action %s crashed", action.tool)
            self._outcome(action, "failed", "The action failed unexpectedly.")
            return f"(System note: you asked and the user said yes, but {what} couldn't be opened.)"
        self._outcome(action, "ok", result)
        return f"(System note: you asked and the user said yes; {what} was opened.)"

    def expire(self, reason: str) -> None:
        """Drop the pending action (a forget command, computer control turned off)."""
        with self._lock:
            action, self._pending = self._pending, None
        if action is not None:
            self._outcome(action, "expired", reason)

    def _outcome(self, action: PendingAction, outcome: str, result: str) -> None:
        line = " ".join(str(result).split())
        log.info("Action: %s %s -> %s: %s", action.tool, action.log_args, outcome, line)
        publish_action(self._bus, action.tool, action.public_args, result, outcome)
