"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from desk_buddy.config import Settings

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence ends at . ! or ? (plus any closing quotes or brackets) followed by whitespace.
    Text at the end of the buffer is held until more arrives or flush() is called.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        sentences: list[str] = []
        while (cut := self._find_boundary()) is not None:
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _find_boundary(self) -> int | None:
        for match in _SENTENCE_END.finditer(self._buf):
            end = match.end()
            if end >= len(self._buf):
                return None  # wait for the next chunk to confirm
            if not self._buf[end].isspace():
                continue  # "3.5", "e.g" mid-token, "example.com"
            if match.group() == "." and self._last_word(match.start()) in _ABBREVIATIONS:
                continue
            return end
        return None

    def _last_word(self, pos: int) -> str:
        words = self._buf[:pos].split()
        return words[-1].lower().lstrip("\"'([") if words else ""


MAX_TOKENS = 2048  # thinking counts toward this; spoken replies are short


class LLMError(Exception):
    """Claude could not produce a reply (network, API error). Already retried by the SDK."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == 'refusal')."""


class ClaudeChat:
    """One-turn streaming chat. Session memory arrives in M2."""

    def __init__(self, client: Any, *, model: str, effort: str, system: str) -> None:
        # client: anthropic.AsyncAnthropic (typed Any so tests can pass a fake)
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Yield text deltas as they arrive. Raises LLMError / LLMRefusal."""
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                output_config={"effort": self._effort},
                messages=[{"role": "user", "content": user_text}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited by the API") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("can't reach the API") from exc
        if message.stop_reason == "refusal":
            raise LLMRefusal("Claude declined to answer")


def create_chat(settings: Settings) -> ClaudeChat:
    """Build the chat client for settings.llm_provider. Add new providers here."""
    from desk_buddy.brain.personality import system_prompt

    if settings.llm_provider == "anthropic":
        return ClaudeChat(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            effort=settings.llm_effort,
            system=system_prompt(settings.max_sentences),
        )
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
