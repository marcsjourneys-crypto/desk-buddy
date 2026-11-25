"""Live events for the dashboard: what CARL hears, says and does.

publish() is called from the voice loop and from tool worker threads. It never blocks and
never raises: the dashboard must never be able to stall or break CARL. The last HISTORY
events are kept in memory (nothing is written to disk) for a page that has just opened;
each subscriber has a bounded queue, and a slow page loses events instead.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

HISTORY = 100
QUEUE_SIZE = 200
MAX_TEXT = 2000  # characters of any one text field


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "..."
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _bounded(str(value))


class EventBus:
    def __init__(
        self,
        *,
        history: int = HISTORY,
        queue_size: int = QUEUE_SIZE,
        now: Callable[[], str] = _now,
    ) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=history)
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._now = now
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def publish(self, type_: str, **fields: Any) -> None:
        """Record an event and send it to every subscriber. Safe from any thread."""
        try:
            event = {k: _bounded(v) for k, v in fields.items()}
            with self._lock:
                event.update(type=type_, ts=self._now(), id=next(self._ids))
                self._ring.append(event)
                loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._deliver(event)
            else:
                loop.call_soon_threadsafe(self._deliver, event)
        except Exception:
            log.debug("Event %s not published", type_, exc_info=True)

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """On the event loop: a queue of live events and the recent history (events in
        both share an id, so a page can skip the overlap)."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._queue_size)
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._subs.add(queue)
            return queue, list(self._ring)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subs.discard(queue)

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ring)

    def _deliver(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for queue in subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a slow page loses events; CARL never waits for it


class _NullBus:
    def publish(self, type_: str, **fields: Any) -> None:
        pass

    def recent(self) -> list[dict[str, Any]]:
        return []


NULL_BUS: Any = _NullBus()
