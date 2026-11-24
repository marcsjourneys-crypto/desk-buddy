"""The FastAPI app (robot WebSocket now, dashboard in M8) and an in-loop uvicorn runner."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import socket
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


class ServerStartError(Exception):
    """The server could not listen (port in use, bad host)."""


def create_app(
    *,
    token: str,
    switch: DeviceSwitch,
    events: asyncio.Queue[str],
    current_state: Callable[[], str],
    hello_timeout: float = 5.0,
) -> FastAPI:
    app = FastAPI(title="Desk Buddy", docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.encode()

    @app.websocket("/robot")
    async def robot(ws: WebSocket) -> None:
        await ws.accept()
        hello = await _read_hello(ws, hello_timeout)
        if hello is None:
            await _close(ws, CLOSE_NO_HELLO)
            log.warning("Robot refused: no hello within %.0f s", hello_timeout)
            return
        # Bytes, so a non-ASCII token can't make compare_digest raise TypeError.
        if not expected or not hmac.compare_digest(hello.data["token"].encode(), expected):
            await _close(ws, CLOSE_BAD_TOKEN)
            log.warning("Robot refused: bad token")
            return
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
            await _receive_loop(ws, link, events)
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


async def _receive_loop(ws: WebSocket, link: RobotLink, events: asyncio.Queue[str]) -> None:
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

    async def stop(self) -> None:
        self.server.should_exit = True
        await self.task


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
