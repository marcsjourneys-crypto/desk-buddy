"""Robot <-> server messages. Text frames are JSON objects with a "type"; audio is binary.

The full protocol is documented in docs/protocol.md. Parsing never raises anything but
ProtocolError, and error messages never include field values (the token must not leak).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from desk_buddy.audio import SAMPLE_RATE

MAX_TOKEN_CHARS = 256
BUTTON_ACTIONS = ("down", "up")
# wake is reserved for a later wake word; the server accepts and ignores it for now.
ROBOT_MESSAGES = ("hello", "button", "touch", "stats", "wake")


class ProtocolError(ValueError):
    """A text frame from the robot is not a valid message."""


@dataclass(frozen=True)
class Message:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


def parse(text: str) -> Message:
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("not JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("not a JSON object")
    kind = obj.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("missing or non-string type")
    if kind not in ROBOT_MESSAGES:
        raise ProtocolError(f"unknown type {kind[:32]!r}")
    if kind == "hello":
        token, fw = obj.get("token"), obj.get("fw_version", "")
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
            raise ProtocolError("hello needs a token string")
        if not isinstance(fw, str):
            raise ProtocolError("fw_version must be a string")
        return Message(kind, {"token": token, "fw_version": fw[:64]})
    if kind == "button":
        action = obj.get("action")
        if action not in BUTTON_ACTIONS:
            raise ProtocolError("button action must be down or up")
        return Message(kind, {"action": action})
    if kind == "stats":
        data = obj.get("data", {})
        if not isinstance(data, dict):
            raise ProtocolError("stats data must be an object")
        return Message(kind, {"data": data})
    return Message(kind)


def welcome() -> dict[str, Any]:
    return {"type": "welcome"}


def state(name: str) -> dict[str, Any]:
    return {"type": "state", "name": name}


def emotion(name: str) -> dict[str, Any]:
    return {"type": "emotion", "name": name}


def audio_start(rate: int = SAMPLE_RATE) -> dict[str, Any]:
    return {"type": "audio_start", "rate": rate}


def audio_end() -> dict[str, Any]:
    return {"type": "audio_end"}


def audio_stop() -> dict[str, Any]:
    return {"type": "audio_stop"}
