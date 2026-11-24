"""A stand-in robot: a WebSocket client for tests, and (later) a voice tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from desk_buddy.audio import CHUNK_BYTES


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

    async def recv(self, timeout: float = 5.0) -> dict[str, Any] | bytes | None:
        """The next frame: a dict for JSON text, bytes for audio, None once closed.
        Raises TimeoutError if nothing arrives within `timeout` seconds."""
        try:
            frame = await asyncio.wait_for(self.ws.recv(), timeout)
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
            return None
        return frame if isinstance(frame, bytes) else json.loads(frame)

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
