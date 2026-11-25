"""A stand-in robot: a WebSocket client for tests, and a voice tool.

    uv run python -m desk_buddy.fake_robot

Run Buddy (`uv run python -m desk_buddy`, with ROBOT_TOKEN set) in one terminal and this in
another. It uses the PC mic and speaker, but talks to Buddy over the robot link, exactly like
the real robot: it streams the mic while Buddy's state is `listening`. The talk key is read
by Buddy itself, which drives whichever robot is connected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import sys
import threading
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from desk_buddy.audio import CHUNK_BYTES, AudioDeviceError, Microphone, Speaker
from desk_buddy.config import ConfigError, Settings, load_settings

log = logging.getLogger("desk_buddy.fake_robot")

STREAM_SECONDS = 0.02  # send mic audio every 20 ms while Buddy is listening

# Close codes from the server (see server.py) and what to tell the user.
_CLOSE_REASONS = {
    4001: ("Refused: bad ROBOT_TOKEN (it must match Buddy's)", 2),
    4002: ("Refused: no hello in time", 1),
    4003: ("Disconnected: replaced by another robot", 1),
}
_NORMAL_CLOSES = (None, 1000, 1001, 1012)  # Buddy quit or restarted
_WILDCARD_HOSTS = ("", "0.0.0.0", "::")


class FakeRobot:
    def __init__(self, url: str, token: str, fw_version: str = "fake-robot") -> None:
        self.url, self.token, self.fw_version = url, token, fw_version
        self.ws: ClientConnection | None = None
        self.close_code: int | None = None

    async def connect(self, *, hello: bool = True) -> None:
        self.ws = await connect(self.url, max_size=2**16)
        if hello:
            await self.send({"type": "hello", "token": self.token, "fw_version": self.fw_version})

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))

    async def press(self) -> None:
        await self.send({"type": "button", "action": "down"})

    async def release(self) -> None:
        await self.send({"type": "button", "action": "up"})

    async def send_audio(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), CHUNK_BYTES):
            await self.ws.send(pcm[i : i + CHUNK_BYTES])

    async def recv(self, timeout: float | None = 5.0) -> dict[str, Any] | bytes | None:
        """The next frame: a dict for JSON text, bytes for audio, None once closed.
        Raises TimeoutError if nothing arrives within `timeout` seconds (None: wait)."""
        while True:
            try:
                frame = await asyncio.wait_for(self.ws.recv(), timeout)
            except ConnectionClosed as exc:
                self.close_code = exc.rcvd.code if exc.rcvd else None
                return None
            if isinstance(frame, bytes):
                return frame
            try:
                return json.loads(frame)
            except ValueError:
                log.warning("Ignored a bad frame from Buddy (not JSON)")

    async def recv_until(self, kind: str, timeout: float = 5.0) -> list[Any]:
        """Frames up to and including the first {"type": kind}. Raises if closed first."""
        frames = []
        while True:
            frame = await self.recv(timeout)
            if frame is None:
                raise ConnectionError(f"closed ({self.close_code}) before {kind!r}")
            frames.append(frame)
            if isinstance(frame, dict) and frame.get("type") == kind:
                return frames

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


# --- The voice tool: pure helpers ---


def robot_url(settings: Settings) -> str:
    """Where Buddy listens. A blank or wildcard host means "this PC" for a client."""
    host = settings.server_host
    if host in _WILDCARD_HOSTS:
        host = "127.0.0.1"
    return f"ws://{host}:{settings.server_port}/robot"


def close_reason(code: int | None) -> tuple[str, int]:
    """(message, exit code) for the code the server closed the connection with."""
    if code in _CLOSE_REASONS:
        return _CLOSE_REASONS[code]
    if code in _NORMAL_CLOSES:
        return (f"Buddy closed the connection (code {code})" if code else "Connection closed"), 0
    return f"Buddy closed the connection (code {code})", 1


class _Playback(Protocol):
    def feed(self, chunk: bytes) -> None: ...
    def stop(self) -> None: ...


def handle_frame(frame: dict[str, Any] | bytes, playback: _Playback) -> None:
    """Do what the robot would with one frame from Buddy."""
    if isinstance(frame, bytes):
        playback.feed(frame)
        return
    kind = frame.get("type")
    if kind == "state":
        log.info("Robot state: %s", frame.get("name"))
    elif kind == "emotion":
        log.info("Robot emotion: %s", frame.get("name"))
    elif kind == "audio_stop":
        playback.stop()
    elif kind == "welcome":
        log.info("Buddy says welcome")
    else:
        log.debug("Frame %s", kind)


class Playback:
    """Plays received audio on its own thread, so receiving never waits on the speaker.
    Each chunk carries the speaker token from when it arrived, so stop() also silences
    anything still queued."""

    def __init__(self, speaker: Any, join_timeout: float = 2.0) -> None:
        self._speaker = speaker
        self._join_timeout = join_timeout
        self._queue: queue.Queue[tuple[bytes, int] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="fake-robot-playback", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def feed(self, chunk: bytes) -> None:
        self._queue.put((chunk, self._speaker.current_token()))

    def stop(self) -> None:
        self._speaker.stop()
        with contextlib.suppress(queue.Empty):
            while True:
                self._queue.get_nowait()

    def close(self) -> None:
        self.stop()
        self._queue.put(None)
        self._thread.join(timeout=self._join_timeout)
        if self._thread.is_alive():
            log.warning("Playback thread did not stop within %.1f s", self._join_timeout)

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            chunk, token = item
            try:
                self._speaker.play(chunk, token)
            except Exception:
                log.exception("Playback failed")


# --- The voice tool: wiring ---


class MicStreamer:
    """Streams the mic to Buddy every 20 ms while Buddy's last state is `listening`, like
    the real robot. on_state() is fed each state frame; run() is one long-lived task."""

    def __init__(self, robot: Any, mic: Any) -> None:
        self._robot, self._mic = robot, mic
        self._listening = asyncio.Event()

    def on_state(self, name: str | None) -> None:
        if name == "listening":
            self._listening.set()
        else:
            self._listening.clear()

    async def run(self) -> None:
        while True:
            await self._listening.wait()
            self._mic.start()
            try:
                while True:
                    await asyncio.sleep(STREAM_SECONDS)
                    if not self._listening.is_set():
                        break
                    if pcm := self._mic.drain():
                        await self._robot.send_audio(pcm)
            finally:
                self._mic.stop()


def _watch_stream(task: asyncio.Task[None], robot: FakeRobot, failed: list[str]) -> None:
    """Report a mic streaming failure at once, and end the tool by closing the connection."""
    closing: set[asyncio.Task[None]] = set()  # keep a reference until it finishes

    def on_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or isinstance(exc, ConnectionClosed):
            return  # a disconnect: the receive loop reports it
        log.error("Mic streaming failed: %s", exc, exc_info=exc)
        failed.append(str(exc))
        close = asyncio.ensure_future(robot.close())
        closing.add(close)
        close.add_done_callback(closing.discard)

    task.add_done_callback(on_done)


async def run(settings: Settings) -> int:
    """Connect and act as the robot until Buddy closes the connection. Returns the exit code."""
    url = robot_url(settings)
    async with contextlib.AsyncExitStack() as stack:
        mic = Microphone()
        stack.callback(mic.close)
        speaker = Speaker()
        stack.callback(speaker.close)
        playback = Playback(speaker)
        stack.callback(playback.close)
        robot = FakeRobot(url, settings.robot_token)
        try:
            await robot.connect()
        except (OSError, InvalidHandshake) as exc:
            print(f"Can't connect to {url}: {exc}", file=sys.stderr)
            print("Is Buddy running, with the same ROBOT_TOKEN?", file=sys.stderr)
            return 1
        stack.push_async_callback(robot.close)
        streamer = MicStreamer(robot, mic)
        stream_task = asyncio.create_task(streamer.run())
        failed: list[str] = []
        _watch_stream(stream_task, robot, failed)
        stack.push_async_callback(_cancel, stream_task)
        log.info("Connected to %s. Ctrl+C to quit.", url)
        log.info("Hold %s in the Buddy window to talk", settings.ptt_key)

        while (frame := await robot.recv(timeout=None)) is not None:
            handle_frame(frame, playback)
            if isinstance(frame, dict) and frame.get("type") == "state":
                streamer.on_state(frame.get("name"))
        if failed:
            log.error("Stopped: mic streaming failed (%s)", failed[0])
            return 1
        message, code = close_reason(robot.close_code)
        (log.info if code == 0 else log.error)(message)
        return code


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    # Its failures were already reported by _watch_stream; don't raise them again on exit.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Can't start: {exc}", file=sys.stderr)
        return 2
    if not settings.robot_token:
        print(
            "Can't start: ROBOT_TOKEN is not set in server/.env (Buddy needs the same one).",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(run(settings))
    except AudioDeviceError as exc:
        print(f"Can't start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("Bye")
        return 0


if __name__ == "__main__":
    sys.exit(main())
