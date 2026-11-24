"""Tools Claude can call: plain functions with a strict JSON schema, run by name."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

LOG_PREVIEW = 80  # results can hold private notes; log only their start


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LOG_PREVIEW else text[:LOG_PREVIEW] + "..."


class ToolError(Exception):
    """A tool couldn't do what was asked. The message is sent back to Claude."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]  # strict: additionalProperties false, every property required
    func: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    is_error: bool = False


class ToolRegistry:
    """The one place tools are registered. run() never raises."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name {tool.name!r}")
            self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def run(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            log.warning("Tool %s: unknown", name)
            return ToolOutcome(f"There is no tool called {name!r}.", is_error=True)
        try:
            # Check the arguments first, so a TypeError inside the tool counts as a crash.
            inspect.signature(tool.func).bind(**args)
        except TypeError as exc:
            # Keys only: values can be private note text.
            log.warning("Tool %s: bad arguments %s (%s)", name, sorted(args), exc)
            return ToolOutcome("The tool was called with the wrong arguments.", is_error=True)
        try:
            text = tool.func(**args)
        except ToolError as exc:
            log.info("Tool %s failed: %s", name, exc)
            return ToolOutcome(str(exc), is_error=True)
        except Exception:
            log.exception("Tool %s crashed", name)
            return ToolOutcome("The tool failed unexpectedly.", is_error=True)
        log.info("Tool %s -> %s", name, _preview(text))
        return ToolOutcome(text)

    def __len__(self) -> int:
        return len(self._tools)
