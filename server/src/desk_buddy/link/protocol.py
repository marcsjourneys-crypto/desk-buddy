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
ROBOT_MESSAGES = ("hello", "button", "touch", "stats", "wake")
WAKE_THRESHOLD_MIN = 0.5  # the range of the robot's wake word cutoff (wake_config)
WAKE_THRESHOLD_MAX = 0.99


class ProtocolError(ValueError):
    """A text frame from the robot is not a valid message."""


@dataclass(frozen=True)
class Message:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        data = self.data
        if "token" in data:
            data = {**data, "token": "***"}  # never show the hello token in logs
        return f"Message(type={self.type!r}, data={data!r})"


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
        try:
            token.encode()  # json.loads lets lone surrogates through
        except UnicodeEncodeError:
            raise ProtocolError("token is not valid UTF-8") from None
        if not isinstance(fw, str):
            raise ProtocolError("fw_version must be a string")
        fw = fw[:64].encode("utf-8", "replace").decode()
        return Message(kind, {"token": token, "fw_version": fw})
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
    if kind == "wake":
        score = obj.get("score")
        # bool is an int in Python, so a JSON true is refused explicitly. The range check
        # also refuses NaN and infinities (json.loads accepts them), and a huge int never
        # reaches float().
        if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1:
            raise ProtocolError("wake score must be a number from 0 to 1")
        return Message(kind, {"score": float(score)})
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


def wake_config(enabled: bool, threshold: float) -> dict[str, Any]:
    """The robot's wake word settings. Raises ValueError for a bad value (never sent)."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not WAKE_THRESHOLD_MIN <= threshold <= WAKE_THRESHOLD_MAX
    ):
        raise ValueError(f"threshold must be from {WAKE_THRESHOLD_MIN} to {WAKE_THRESHOLD_MAX}")
    return {"type": "wake_config", "enabled": enabled, "threshold": float(threshold)}
