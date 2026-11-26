"""The gate every computer-control tool goes through (CLAUDE.md's safety rules, in code).

- Off by default: COMPUTER_CONTROL=on turns the tools on.
- At most MAX_PER_QUESTION actions per question and MAX_PER_MINUTE per rolling minute.
  An attempt counts even if it fails; a refused one doesn't.
- Every action is logged on one line: "Action: <tool> <args> -> <result>".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

from desk_buddy.tools.registry import NotDone, ToolError
from desk_buddy.tools.untrusted import shorten

log = logging.getLogger(__name__)

MAX_PER_QUESTION = 3
MAX_PER_MINUTE = 8
WINDOW_SECONDS = 60.0
LOG_PREVIEW = 120  # characters of a result in the log
ARGS_PREVIEW = 200  # characters of the arguments in the log
RESULT_PREVIEW = 500  # characters of a result on the dashboard

OFF_MESSAGE = "Computer control is off. It can be turned on in the settings."
LIMIT_MESSAGE = "That's enough actions for one go."


def _bounded(text: str, limit: int) -> str:
    """Cut to `limit` characters, keeping an untrusted block well formed."""
    return shorten(text, limit)


def _one_line(text: str, limit: int = LOG_PREVIEW) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _shown(args: Mapping[str, Any]) -> str:
    """The arguments as one bounded line of JSON (for the log and the dashboard)."""
    try:
        shown = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # e.g. keys that can't be sorted
        shown = repr(dict(args))
    return _one_line(shown, ARGS_PREVIEW)


class ActionGuard:
    """Thread-safe: tools run in worker threads. The loop calls new_question() per turn."""

    def __init__(
        self,
        enabled: bool,
        *,
        per_question: int = MAX_PER_QUESTION,
        per_minute: int = MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        bus: Any = None,
    ) -> None:
        self.enabled = enabled
        self._bus = bus  # events.EventBus: each action is shown on the dashboard
        self._per_question = per_question
        self._per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._this_question = 0
        self._recent: deque[float] = deque()

    def new_question(self) -> None:
        with self._lock:
            self._this_question = 0

    def run(
        self,
        name: str,
        args: Mapping[str, Any],
        fn: Callable[[], str],
        *,
        shown_result: Callable[[str], str] | None = None,
    ) -> str:
        """Run one action if allowed; raise ToolError (which CARL reads out) if not.
        `shown_result` maps the result to what the log line and the dashboard show (a
        skill's output is never shown, only "ok")."""
        shown = _shown(args)
        described = f"{name} {shown}"
        self._admit(name, described, shown)
        try:
            result = fn()
        except ToolError as exc:
            log.info("Action: %s -> failed: %s", described, _one_line(str(exc)))
            self._publish(name, shown, str(exc), "failed")
            raise
        except Exception as exc:
            log.warning("Action: %s -> crashed: %s", described, _one_line(repr(exc)))
            self._publish(name, shown, "The tool failed unexpectedly.", "failed")
            raise
        public = shown_result(result) if shown_result is not None else result
        log.info("Action: %s -> %s", described, _one_line(public))
        self._publish(name, shown, public, "ok")
        return result

    def run_after_yes(
        self,
        name: str,
        args: Mapping[str, Any],
        fn: Callable[[], str],
        *,
        confirmations: Any,
        description: str,
        public_args: Mapping[str, Any] | None = None,
        verb: Any = None,
        ok_note: Callable[[str], str] | None = None,
        failed_note: Callable[[ToolError], str] | None = None,
        shown_result: Callable[[str], str] | None = None,
        title: str | None = None,
    ) -> str:
        """A powerful action: checked and counted now like run(), but not run. It waits
        in `confirmations` (tools.confirm.Confirmations) for the user's spoken yes at the
        next question, and runs then without being counted again (and only if computer
        control is still on). Returns what Claude is told (NotDone: not a success yet);
        raises ToolError if refused. `public_args` replace `args` on the dashboard (the
        log shows `args`). `verb` (a confirm.Verb, default OPEN) names the action in the
        question and the notes; `ok_note`/`failed_note` let the action write the note
        Claude gets after a yes; `shown_result` is as in run(); `title` names it in
        the notes when it's shorter than the description."""
        from desk_buddy.tools.confirm import OPEN, PendingAction  # it imports this module

        verb = verb or OPEN

        shown = _shown(args)
        public = _shown(public_args) if public_args is not None else shown
        self._admit(name, f"{name} {shown}", public)

        def confirmed() -> str:
            if not self.enabled:
                raise ToolError(OFF_MESSAGE)
            return fn()

        # First, so a request this one replaces is shown as cancelled before it waits.
        confirmations.propose(
            PendingAction(
                name,
                shown,
                public,
                description,
                confirmed,
                verb=verb,
                ok_note=ok_note,
                failed_note=failed_note,
                shown=shown_result,
                title=title,
            )
        )
        log.info("Action: %s %s -> waiting for a yes", name, shown)
        self._publish(name, public, f"Waiting for the user to say yes to {description}.", "waiting")
        # The chat ends the turn on this result; the robot then asks the question.
        return NotDone(
            f"Not done yet: the robot is now asking the user whether to {verb.base} "
            f"{description}; it {verb.third} only if their next words are a yes. Never say "
            f"it's {verb.state} unless a system note says it was {verb.done}."
        )

    def refuse(
        self,
        name: str,
        args: Mapping[str, Any],
        message: str,
        *,
        public_args: Mapping[str, Any] | None = None,
    ) -> NoReturn:
        """Refuse a call the tool itself rejected (an unknown name, an address that isn't
        allowed): counted toward the limits like any attempt, logged, shown on the
        dashboard as refused, and raised as ToolError(message) for Claude. If computer
        control is off or a limit is hit, that refusal is raised instead."""
        shown = _shown(args)
        public = _shown(public_args) if public_args is not None else shown
        self._admit(name, f"{name} {shown}", public)
        log.info("Action: %s %s -> refused: %s", name, shown, _one_line(message))
        self._publish(name, public, message, "refused")
        raise ToolError(message)

    def _admit(self, name: str, described: str, shown: str) -> None:
        """Refuse (log, show and raise) if off or over a limit; otherwise count it."""
        if not self.enabled:
            log.info("Action: %s -> refused: off", described)
            self._publish(name, shown, OFF_MESSAGE, "refused")
            raise ToolError(OFF_MESSAGE)
        with self._lock:
            now = self._clock()
            while self._recent and self._recent[0] <= now - WINDOW_SECONDS:
                self._recent.popleft()
            if self._this_question >= self._per_question or len(self._recent) >= self._per_minute:
                log.info("Action: %s -> refused: rate limit", described)
                refused = True
            else:
                refused = False
                self._this_question += 1
                self._recent.append(now)
        if refused:
            self._publish(name, shown, LIMIT_MESSAGE, "refused")
            raise ToolError(LIMIT_MESSAGE)

    def _publish(self, tool: str, args: str, result: str, outcome: str) -> None:
        publish_action(self._bus, tool, args, result, outcome)


def publish_action(bus: Any, tool: str, args: str, result: str, outcome: str) -> None:
    """An action event for the dashboard (the result can hold untrusted text: the page
    shows it as plain text). Never raises into the caller."""
    if bus is None:
        return
    try:
        bus.publish(
            "action",
            tool=tool,
            args=args,
            result=_bounded(str(result), RESULT_PREVIEW),
            outcome=outcome,
        )
    except Exception:
        log.debug("Dashboard action event failed", exc_info=True)
