"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from typing import Any, Protocol

import anthropic

from desk_buddy.brain.memory import merge_consecutive
from desk_buddy.brain.personality import Personality, system_prompt
from desk_buddy.config import Settings
from desk_buddy.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# Boundary candidates, in buffer order: a newline, or sentence-ending punctuation
# plus any closing quotes, brackets or markdown emphasis.
_BOUNDARY = re.compile(r"(?P<nl>\n)|(?P<end>[.!?\u2026]+[\"')\]\u201d\u2019*_]*)")
_CLOSERS = "\"')]\u201d\u2019*_"
_OPENERS = "\"'([\u201c\u2018*_"
_CLAUSE_BREAKS = ",;:"
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}


def _speakable(text: str) -> bool:
    return any(c.isalnum() for c in text)


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence ends at . ! ? or … (plus any closing quotes, brackets or markdown emphasis)
    followed by whitespace, or at a newline. A "." after an abbreviation, a number or a
    single letter does not end a sentence. Pieces with no letters or digits are never
    released on their own; they join the next sentence. If the buffer passes max_chars
    with no boundary, it is cut at the last , ; : or whitespace before the cap (hard cut
    as a last resort). The output does not depend on how the text is chunked.
    """

    def __init__(self, max_chars: int = 200) -> None:
        self._buf = ""
        self._max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        sentences: list[str] = []
        while (cut := self._find_boundary()) is not None:
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if _speakable(sentence):
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if _speakable(rest) else []

    def _find_boundary(self) -> int | None:
        for match in _BOUNDARY.finditer(self._buf):
            cut = match.start() if match.group("nl") else match.end()
            if cut > self._max_chars:
                break  # past the cap: same decision however the text was chunked
            if not _speakable(self._buf[:cut]):
                continue  # nothing to say yet; let it join the next sentence
            if match.group("nl"):
                return cut
            if cut >= len(self._buf):
                return None  # wait for the next chunk to confirm
            if not self._buf[cut].isspace():
                continue  # "3.5", "e.g" mid-token, "example.com"
            if match.group("end").rstrip(_CLOSERS) == "." and self._is_non_final_dot(match.start()):
                continue
            return cut
        if len(self._buf) > self._max_chars:
            return self._cap_cut()
        return None

    def _is_non_final_dot(self, pos: int) -> bool:
        word = self._last_word(pos)
        return word in _ABBREVIATIONS or word.isdigit() or (len(word) == 1 and word.isalpha())

    def _cap_cut(self) -> int:
        head = self._buf[: self._max_chars]
        for i in range(len(head) - 1, 0, -1):
            if (head[i] in _CLAUSE_BREAKS or head[i].isspace()) and _speakable(head[: i + 1]):
                return i + 1
        return self._max_chars  # hard cut (dropped by feed if it has nothing speakable)

    def _last_word(self, pos: int) -> str:
        words = self._buf[max(0, pos - 20) : pos].split()
        return words[-1].lower().lstrip(_OPENERS) if words else ""


MAX_TOKENS = 2048  # thinking counts toward this; spoken replies are short
MAX_TOOL_ROUNDS = 3  # API calls per reply; the last one may not call tools
# Remembered for a tool call whose result never came (the turn was cut off).
INTERRUPTED_RESULT = "Interrupted; the action may have completed."


class LLMError(Exception):
    """Claude could not produce a reply (network, API error). Already retried by the SDK."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == 'refusal')."""


class Chat(Protocol):
    """What the voice loop needs from any LLM provider. A provider that runs tools may
    also keep a `tools_succeeded` count and `last_tool_rounds` (see ClaudeChat); the
    loop reads them if present."""

    def stream_reply(
        self, history: Sequence[Mapping[str, Any]], user_text: str
    ) -> AsyncGenerator[str, None]: ...


def _llm_error(exc: anthropic.APIError) -> LLMError:
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError("rate limited by the API")
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMError("API key rejected (check ANTHROPIC_API_KEY)")
    if isinstance(exc, anthropic.APIStatusError):
        # status 200 here means an error event arrived mid-stream; type names it
        return LLMError(f"API error {exc.status_code} ({getattr(exc, 'type', None)})")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("can't reach the API")
    return LLMError(f"unexpected API error ({type(exc).__name__})")


class ClaudeChat:
    """Streaming chat with a tool loop. The caller owns the history (see brain.memory)."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        effort: str,
        system: str | Callable[[], str],
        tools: ToolRegistry | None = None,
    ) -> None:
        # client: anthropic.AsyncAnthropic (typed Any so tests can pass a fake)
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system if callable(system) else (lambda: system)
        self._tools = tools or ToolRegistry()
        # Tool calls that succeeded, ever. Only grows; the loop compares it before and
        # after a reply to tell whether that reply's turn ran a tool successfully.
        self.tools_succeeded = 0
        # The latest reply's tool rounds, (tool_use blocks, tool_result blocks) each, as
        # plain dicts for the conversation memory. Reset when a reply starts.
        self.last_tool_rounds: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def stream_reply(
        self, history: Sequence[Mapping[str, Any]], user_text: str
    ) -> AsyncGenerator[str, None]:
        """Yield text deltas as they arrive, running tools between rounds.

        Raises LLMError (any API or network failure) or LLMRefusal. Either can arrive
        after some text was already yielded, so callers must handle a partial reply.
        """
        # A remembered turn can end on tool results: the new text then joins that message.
        messages: list[Any] = merge_consecutive([*history, {"role": "user", "content": user_text}])
        self.last_tool_rounds = []
        spoke = False
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            params: dict[str, Any] = {
                "model": self._model,
                "max_tokens": MAX_TOKENS,
                "system": self._system(),
                "output_config": {"effort": self._effort},
                "messages": messages,
            }
            if self._tools:
                params["tools"] = self._tools.schemas()
                if round_no == MAX_TOOL_ROUNDS:
                    params["tool_choice"] = {"type": "none"}
            first = True
            try:
                async with self._client.messages.stream(**params) as stream:
                    async for text in stream.text_stream:
                        if first and spoke:
                            yield "\n"  # keep sentences from separate rounds apart
                        first = False
                        spoke = True
                        yield text
                    message = await stream.get_final_message()
            except anthropic.APIError as exc:
                raise _llm_error(exc) from exc
            if message.stop_reason == "refusal":
                raise LLMRefusal("Claude declined to answer")
            if message.stop_reason == "max_tokens":
                log.warning("reply truncated: stop_reason=max_tokens (MAX_TOKENS=%d)", MAX_TOKENS)
                if not spoke:
                    raise LLMError("reply ran out of room before saying anything")
                return
            if message.stop_reason != "tool_use":
                return
            uses = [block for block in message.content if block.type == "tool_use"]
            if not uses:
                log.warning("stop_reason=tool_use but no tool_use blocks; ending the reply")
                return
            # For memory: plain tool_use blocks (no thinking, no text, which was spoken),
            # recorded before they run. Each result starts as a placeholder and is filled
            # in as its tool finishes, so a call cut off mid-run (or never reached) is
            # still remembered, and every tool_use keeps a result.
            results: list[dict[str, Any]] = [
                {"type": "tool_result", "tool_use_id": b.id, "content": INTERRUPTED_RESULT}
                for b in uses
            ]
            self.last_tool_rounds.append(
                (
                    [
                        {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                        for b in uses
                    ],
                    results,
                )
            )
            for i, block in enumerate(uses):
                results[i] = await self._run_tool(block)
            # New list each round: the content list goes back unchanged (thinking blocks too).
            messages = [
                *messages,
                {"role": "assistant", "content": message.content},
                {"role": "user", "content": results},
            ]

    async def _run_tool(self, block: Any) -> dict[str, Any]:
        # If the turn is cancelled here the thread still finishes (a note may be saved),
        # but the cancel propagates at once and the reply ends.
        outcome = await asyncio.to_thread(self._tools.run, block.name, block.input)
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": outcome.text,
        }
        if outcome.is_error:
            result["is_error"] = True
        else:
            self.tools_succeeded += 1
        return result


def create_chat(
    settings: Settings,
    tools: ToolRegistry | None = None,
    personality: Callable[[], Personality] = Personality,
) -> Chat:
    """Build the chat client for settings.llm_provider. Add new providers here. The
    system prompt is rebuilt for every request, with the personality as it is then."""
    if settings.llm_provider == "anthropic":
        return ClaudeChat(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            effort=settings.llm_effort,
            system=lambda: prompt_for(settings, tools, personality()),
            tools=tools,
        )
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")


def prompt_for(settings: Settings, tools: ToolRegistry | None, personality: Personality) -> str:
    """The full system prompt CARL gets (also the dashboard's read-only preview)."""
    return system_prompt(
        settings.max_sentences,
        notes=tools is not None and "take_note" in tools,
        music=tools is not None and "play_music" in tools,
        web=settings.web_search,
        character=personality.character,
        about=personality.about,
    )
