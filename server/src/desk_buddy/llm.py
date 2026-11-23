"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import Any, Protocol

import anthropic

from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import Settings

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


class LLMError(Exception):
    """Claude could not produce a reply (network, API error). Already retried by the SDK."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == 'refusal')."""


class Chat(Protocol):
    """What the voice loop needs from any LLM provider."""

    def stream_reply(self, user_text: str) -> AsyncGenerator[str, None]: ...


class ClaudeChat:
    """One-turn streaming chat. Session memory arrives in M2."""

    def __init__(self, client: Any, *, model: str, effort: str, system: str) -> None:
        # client: anthropic.AsyncAnthropic (typed Any so tests can pass a fake)
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system

    async def stream_reply(self, user_text: str) -> AsyncGenerator[str, None]:
        """Yield text deltas as they arrive.

        Raises LLMError (any API or network failure) or LLMRefusal. Either can arrive
        after some text was already yielded, so callers must handle a partial reply.
        """
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
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMError("API key rejected (check ANTHROPIC_API_KEY)") from exc
        except anthropic.APIStatusError as exc:
            # status 200 here means an error event arrived mid-stream; type names it
            raise LLMError(f"API error {exc.status_code} ({getattr(exc, 'type', None)})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("can't reach the API") from exc
        except anthropic.APIError as exc:
            raise LLMError(f"unexpected API error ({type(exc).__name__})") from exc
        if message.stop_reason == "refusal":
            raise LLMRefusal("Claude declined to answer")
        if message.stop_reason == "max_tokens":
            log.warning("reply truncated: stop_reason=max_tokens (MAX_TOKENS=%d)", MAX_TOKENS)


def create_chat(settings: Settings) -> Chat:
    """Build the chat client for settings.llm_provider. Add new providers here."""
    if settings.llm_provider == "anthropic":
        return ClaudeChat(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            effort=settings.llm_effort,
            system=system_prompt(settings.max_sentences),
        )
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
