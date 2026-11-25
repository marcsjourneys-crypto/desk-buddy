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
from typing import Any

from desk_buddy.tools.registry import ToolError
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

    def run(self, name: str, args: Mapping[str, Any], fn: Callable[[], str]) -> str:
        """Run one action if allowed; raise ToolError (which CARL reads out) if not."""
        try:
            shown = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):  # e.g. keys that can't be sorted
            shown = repr(dict(args))
        shown = _one_line(shown, ARGS_PREVIEW)
        described = f"{name} {shown}"
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
        log.info("Action: %s -> %s", described, _one_line(result))
        self._publish(name, shown, result, "ok")
        return result

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
