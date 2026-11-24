"""The FastAPI app (robot WebSocket now, dashboard in M8) and an in-loop uvicorn runner."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import socket
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, WebSocket

from desk_buddy.link import protocol
from desk_buddy.link.protocol import ProtocolError
from desk_buddy.link.robot import RobotLink
from desk_buddy.link.switch import DeviceSwitch

log = logging.getLogger(__name__)

CLOSE_BAD_TOKEN = 4001
CLOSE_NO_HELLO = 4002
CLOSE_REPLACED = 4003
MAX_FRAME_BYTES = 64 * 1024
PING_SECONDS = 20.0
SHUTDOWN_SECONDS = 5  # bound on uvicorn's graceful shutdown
# Hardening for the LAN: anyone on the Wi-Fi can reach the port.
MAX_AUTH_FAILURES = 5  # bad tokens from one IP within AUTH_WINDOW...
AUTH_WINDOW = 60.0
BLOCK_SECONDS = 60.0  # ...block that IP for this long
MAX_PENDING_HELLOS = 4  # connections waiting for their hello at once
BUTTON_RATE = 10.0  # button messages per second, with a burst of BUTTON_BURST
BUTTON_BURST = 10
BUTTON_QUIET_SECONDS = 1.0  # a flood is logged again after this long without drops


class ServerStartError(Exception):
    """The server could not listen (port in use, bad host)."""


class _AuthThrottle:
    """Bad-token failures per client IP, in memory only."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, ip: str) -> bool:
        self._prune()
        return ip in self._blocked_until

    def failed(self, ip: str) -> bool:
        """Record a bad token. True if this failure starts a block."""
        now = self._clock()
        failures = self._failures.setdefault(ip, deque())
        failures.append(now)
        self._prune()
        if len(failures) < MAX_AUTH_FAILURES:
            return False
        failures.clear()
        self._blocked_until[ip] = now + BLOCK_SECONDS
        self._prune()
        return True

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def _prune(self) -> None:
        """Forget old failures and expired blocks, so the dicts can't grow without bound."""
        now = self._clock()
        for ip, until in list(self._blocked_until.items()):
            if until <= now:
                del self._blocked_until[ip]
        for ip, failures in list(self._failures.items()):
            while failures and failures[0] <= now - AUTH_WINDOW:
                failures.popleft()
            if not failures:
                del self._failures[ip]


class _ButtonLimiter:
    """A token bucket for button messages: BUTTON_RATE per second, bursts of BUTTON_BURST.
    Logs once per flood, and again only after BUTTON_QUIET_SECONDS without drops."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._tokens = float(BUTTON_BURST)
        self._last = clock()
        self._last_drop: float | None = None

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(BUTTON_BURST, self._tokens + (now - self._last) * BUTTON_RATE)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        if self._last_drop is None or now - self._last_drop >= BUTTON_QUIET_SECONDS:
            log.warning("Robot button flood: dropping")
        self._last_drop = now
        return False


def create_app(
    *,
    token: str,
    switch: DeviceSwitch,
    events: asyncio.Queue[str],
    current_state: Callable[[], str],
    hello_timeout: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    app = FastAPI(title="Desk Buddy", docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.encode()
    throttle = _AuthThrottle(clock)
    pending_hellos = 0

    @app.websocket("/robot")
    async def robot(ws: WebSocket) -> None:
        nonlocal pending_hellos
        ip = ws.client.host if ws.client else "unknown"
        if throttle.blocked(ip):
            await ws.accept()
            await _close(ws, CLOSE_BAD_TOKEN)
            return
        if pending_hellos >= MAX_PENDING_HELLOS:
            await ws.accept()
            await _close(ws, CLOSE_NO_HELLO)
            log.warning("Robot refused: too many connections waiting for their hello")
            return
        # Counted from before accept(), so a client that sees the handshake is counted.
        pending_hellos += 1
        try:
            await ws.accept()
            hello = await _read_hello(ws, hello_timeout)
        finally:
            pending_hellos -= 1
        if hello is None:
            await _close(ws, CLOSE_NO_HELLO)
            log.warning("Robot refused: no hello within %.0f s", hello_timeout)
            return
        # Bytes, so a non-ASCII token can't make compare_digest raise TypeError.
        if not expected or not hmac.compare_digest(hello.data["token"].encode(), expected):
            await _close(ws, CLOSE_BAD_TOKEN)
            log.warning("Robot refused: bad token")
            if throttle.failed(ip):
                log.warning("Robot refused: %s blocked after repeated bad tokens", ip)
            return
        throttle.succeeded(ip)
        link = RobotLink(ws, asyncio.get_running_loop(), hello.data["fw_version"])
        # Queue welcome and state before attaching, so no state or emotion from the
        # orchestrator can reach the robot ahead of them.
        link.send(protocol.welcome())
        link.send(protocol.state(current_state()))
        sender = asyncio.create_task(link.run_sender())
        old = switch.attach(link)
        events.put_nowait("reset")
        try:
            if old is not None:
                log.info("New robot connection replaces the old one")
                await old.close(CLOSE_REPLACED)
            log.info("Robot connected (firmware %r)", link.fw_version or "unknown")
            await _receive_loop(ws, link, events, _ButtonLimiter(clock))
        finally:
            try:
                await link.close()
                await sender
            finally:
                sender.cancel()  # no-op if it finished; never leak it if we were cancelled
                if switch.detach(link):
                    log.info("Robot disconnected")
                    events.put_nowait("reset")

    return app


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):  # the robot may already be gone
        await ws.close(code=code)


async def _read_hello(ws: WebSocket, timeout: float) -> protocol.Message | None:
    try:
        async with asyncio.timeout(timeout):
            frame = await ws.receive()
    except TimeoutError:
        return None
    text = frame.get("text")
    if text is None:
        return None
    try:
        msg = protocol.parse(text)
    except ProtocolError:
        return None
    return msg if msg.type == "hello" else None


async def _receive_loop(
    ws: WebSocket,
    link: RobotLink,
    events: asyncio.Queue[str],
    buttons: _ButtonLimiter | None = None,
) -> None:
    buttons = buttons or _ButtonLimiter(time.monotonic)
    while not link.closed:
        try:
            frame = await ws.receive()
        except Exception:  # socket closed under us (e.g. replaced)
            return
        if frame["type"] == "websocket.disconnect" or link.closed:
            return  # a replaced link must not push a stale down/up
        if (data := frame.get("bytes")) is not None:
            link.mic.feed(data)
            continue
        try:
            msg = protocol.parse(frame.get("text") or "")
        except ProtocolError as exc:
            log.warning("Ignored bad message from robot: %s", exc)
            continue
        if msg.type == "button":
            # Dropping a down while its up gets through is harmless: the orchestrator
            # ignores an up when it isn't listening.
            if not buttons.allow():
                continue
            if msg.data["action"] == "down":
                link.mic.begin()
            else:
                link.mic.end()
            events.put_nowait(msg.data["action"])
        elif msg.type == "stats":
            log.debug("Robot stats: %s", msg.data["data"])
        elif msg.type == "hello":
            log.warning("Ignored a second hello")
        else:
            log.info("Robot %s (not used yet)", msg.type)


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # Desk Buddy owns Ctrl+C, not uvicorn
        yield


@dataclass
class RunningServer:
    server: uvicorn.Server
    task: asyncio.Task[None]
    port: int
    stopping: bool = False

    def __post_init__(self) -> None:
        self.task.add_done_callback(self._on_done)

    async def stop(self) -> None:
        self.stopping = True
        self.server.should_exit = True
        await self.task

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if self.stopping:
            return
        if task.cancelled():
            log.error("Robot link stopped unexpectedly: cancelled")
        elif (exc := task.exception()) is not None:
            log.error("Robot link stopped unexpectedly: %s", exc, exc_info=exc)
        else:
            log.error("Robot link stopped unexpectedly")


async def start_server(app: FastAPI, host: str, port: int) -> RunningServer:
    """Bind first (so a busy port is a clear error), then serve in this event loop."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise ServerStartError(f"Can't listen on {host}:{port}: {exc}") from exc
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise ServerStartError(f"Can't listen on {host}:{port}: {exc}") from exc
    config = uvicorn.Config(
        app,
        log_config=None,  # keep Desk Buddy's logging setup; uvicorn logs propagate to it
        log_level="warning",
        lifespan="off",
        ws_max_size=MAX_FRAME_BYTES,
        ws_ping_interval=PING_SECONDS,
        ws_ping_timeout=PING_SECONDS,
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )
    server = _Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()  # re-raise why it stopped
                raise ServerStartError("server stopped during startup")
            await asyncio.sleep(0.01)
    except BaseException:  # failed or cancelled (e.g. Ctrl+C): don't leave it half-running
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        sock.close()
        raise
    return RunningServer(server, task, sock.getsockname()[1])
