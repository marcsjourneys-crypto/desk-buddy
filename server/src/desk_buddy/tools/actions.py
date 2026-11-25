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

log = logging.getLogger(__name__)

MAX_PER_QUESTION = 3
MAX_PER_MINUTE = 8
WINDOW_SECONDS = 60.0
LOG_PREVIEW = 120

OFF_MESSAGE = "Computer control is off. It can be turned on in the settings."
LIMIT_MESSAGE = "That's enough actions for one go."


def _one_line(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= LOG_PREVIEW else text[:LOG_PREVIEW] + "..."


class ActionGuard:
    """Thread-safe: tools run in worker threads. The loop calls new_question() per turn."""

    def __init__(
        self,
        enabled: bool,
        *,
        per_question: int = MAX_PER_QUESTION,
        per_minute: int = MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
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
        described = f"{name} {json.dumps(dict(args), ensure_ascii=False, sort_keys=True)}"
        if not self.enabled:
            log.info("Action: %s -> refused: off", described)
            raise ToolError(OFF_MESSAGE)
        with self._lock:
            now = self._clock()
            while self._recent and self._recent[0] <= now - WINDOW_SECONDS:
                self._recent.popleft()
            if self._this_question >= self._per_question or len(self._recent) >= self._per_minute:
                log.info("Action: %s -> refused: rate limit", described)
                raise ToolError(LIMIT_MESSAGE)
            self._this_question += 1
            self._recent.append(now)
        try:
            result = fn()
        except ToolError as exc:
            log.info("Action: %s -> failed: %s", described, _one_line(str(exc)))
            raise
        except Exception as exc:
            log.warning("Action: %s -> crashed: %s", described, _one_line(repr(exc)))
            raise
        log.info("Action: %s -> %s", described, _one_line(result))
        return result
