"""One robot connection: its mic, its speaker and the link that owns them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import WebSocketDisconnect

from desk_buddy.audio import BYTES_PER_SECOND, PcmBuffer
from desk_buddy.link import protocol

log = logging.getLogger(__name__)

MAX_RECORD_SECONDS = 30.0
MAX_SLEEP = 0.02  # so stop() takes effect within one chunk
# Speech goes to the robot in 100 ms frames (3200 bytes), not 20 ms ones. The ESP32's TCP
# stack queues at most 6 received segments per socket (CONFIG_LWIP_TCP_RECVMBOX_SIZE) and
# retries a full queue on a 250 ms timer, so 640-byte frames (one small segment each)
# capped it at ~31 frames/s, below real time. Full-size segments hit the receive window
# first instead. Measured on the first robot voice check, 2026-11-24.
ROBOT_CHUNK_BYTES = BYTES_PER_SECOND // 10
CLOSE_TIMEOUT = 2.0  # seconds close() waits for the socket to close
MAX_OUTBOX = 128  # about 2.5 s of 20 ms audio chunks; more means the robot isn't reading

Send = Callable[[Any], None]  # thread-safe; takes a protocol dict or PCM bytes


class RobotMic:
    """Audio frames from the robot. The link calls begin()/end() on button down/up, so no
    frame is lost while the orchestrator catches up; start()/stop() are the Mic protocol.

    Recording is driven by begin()/end() only: stop() takes the buffer but leaves recording
    alone, so a fast re-press (the link has already begun the next clip when the
    orchestrator handles the previous "up") keeps the start of the next clip.
    begin(), end(), feed() and stop() are all called on the event-loop thread.
    """

    def __init__(self, max_seconds: float = MAX_RECORD_SECONDS) -> None:
        self._buffer = PcmBuffer(max_seconds)
        self._recording = False

    def begin(self) -> None:
        self._buffer.take()
        self._recording = True

    def end(self) -> None:
        self._recording = False

    def feed(self, data: bytes) -> None:
        if not self._recording:
            return
        if len(data) % 2:
            log.debug("Dropped odd-length audio frame (%d bytes)", len(data))
            return
        self._buffer.append(data)

    def start(self) -> None:
        """No-op: recording already began when the button went down."""

    def stop(self) -> bytes:
        return self._buffer.take()


class RobotPlayer:
    """Sends TTS audio to the robot at real time plus `lead` seconds, so the robot's buffer
    stays small and stop() can still cut a sentence short. Same token scheme as
    audio.Speaker: stop() bumps a generation; play() only sends while its token is current.
    """

    def __init__(
        self,
        send: Send,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        lead: float = 0.2,
        chunk_bytes: int = ROBOT_CHUNK_BYTES,
    ) -> None:
        self._send = send
        self._chunk = chunk_bytes
        self._clock = clock
        self._sleep = sleep
        self._lead = lead
        self._generation = 0
        self._closed = False
        self._lock = threading.Lock()

    def current_token(self) -> int:
        return self._generation

    def _live(self, token: int) -> bool:
        return not self._closed and token == self._generation

    def play(self, pcm: bytes, token: int) -> None:
        pcm = pcm[: len(pcm) & ~1]
        with self._lock:
            if not self._live(token):
                return
            self._send(protocol.audio_start())
        start = self._clock()
        sent = 0.0
        for i in range(0, len(pcm), self._chunk):
            chunk = pcm[i : i + self._chunk]
            with self._lock:
                if not self._live(token):
                    return
                self._send(chunk)
            sent += len(chunk) / BYTES_PER_SECOND
            if not self._wait_until(start + sent - self._lead, token):
                return
        with self._lock:
            if not self._live(token):
                return
            self._send(protocol.audio_end())
        self._wait_until(start + sent - self._lead, token)

    def _wait_until(self, deadline: float, token: int) -> bool:
        while (left := deadline - self._clock()) > 0:
            if not self._live(token):
                return False
            self._sleep(min(left, MAX_SLEEP))
        return self._live(token)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            if self._closed:
                return
            try:
                self._send(protocol.audio_stop())
            except Exception as exc:  # never abort the orchestrator's interrupt
                log.debug("Couldn't send audio_stop: %s", exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1


class RobotLink:
    """One connected robot. All sends go through one outbox and one sender task, so frames
    keep their order and never interleave."""

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop, fw_version: str = "") -> None:
        self._ws = ws
        self._loop = loop
        self._outbox: asyncio.Queue[Any] = asyncio.Queue()
        self.fw_version = fw_version
        self.closed = False
        self.mic = RobotMic()
        self.player = RobotPlayer(self.send)
        self._overflowed = False
        self._overflow_close: asyncio.Task[None] | None = None  # kept so it isn't GC'd

    def send(self, item: Any) -> None:
        """Queue a protocol dict or PCM bytes. Safe from any thread and never blocks: it is
        called under RobotPlayer's lock. Does nothing once the link is closed."""
        if self.closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, item)
        except RuntimeError:  # loop already closed during shutdown
            pass

    def _enqueue(self, item: Any) -> None:
        """On the event loop. A full outbox means the robot stopped reading: close the link
        (1011) so it reconnects, and drop the item."""
        if self.closed:
            return
        if self._outbox.qsize() >= MAX_OUTBOX:
            if not self._overflowed:
                self._overflowed = True
                log.warning("Robot send queue full (%d items): closing the link", MAX_OUTBOX)
                self._overflow_close = self._loop.create_task(self.close(1011))
            return
        self._outbox.put_nowait(item)

    async def run_sender(self) -> None:
        """Runs until close(). A failed send closes the link (treated as a disconnect)."""
        try:
            while (item := await self._outbox.get()) is not None:
                if self.closed:
                    return
                if isinstance(item, bytes):
                    await self._ws.send_bytes(item)
                else:
                    await self._ws.send_json(item)
        except WebSocketDisconnect as exc:  # the robot went away: a normal disconnect
            log.debug("Send to robot failed, robot disconnected (%s)", exc.code)
            await self.close()
        except Exception as exc:
            if self.closed:  # the socket is closing anyway
                log.debug("Send to robot failed while closing: %s", exc)
            else:
                log.warning("Send to robot failed: %s", exc)
                await self.close(1011)

    async def close(self, code: int = 1000) -> None:
        """Stop the player (halting any play() in a worker thread), end the sender and close
        the socket. Safe to call more than once."""
        if self.closed:
            return
        self.closed = True
        self.player.close()
        self._outbox.put_nowait(None)
        with contextlib.suppress(Exception):  # TimeoutError too: a stalled peer can't hold us
            await asyncio.wait_for(self._ws.close(code=code), CLOSE_TIMEOUT)
