import asyncio
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.llm import ClaudeChat, LLMError, LLMRefusal


class FakeStream:
    def __init__(self, deltas, stop_reason="end_turn", error=None):
        self._deltas, self._stop_reason, self._error = deltas, stop_reason, error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason)


class FakeClient:
    def __init__(self, stream: FakeStream):
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._s = stream

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._s


def collect(chat: ClaudeChat, text: str) -> list[str]:
    async def run():
        return [d async for d in chat.stream_reply(text)]

    return asyncio.run(run())


def make_chat(stream: FakeStream) -> tuple[ClaudeChat, FakeClient]:
    client = FakeClient(stream)
    return ClaudeChat(client, model="m", effort="low", system="sys"), client


def test_streams_text_and_sends_settings():
    chat, client = make_chat(FakeStream(["Hel", "lo."]))
    assert collect(chat, "hi") == ["Hel", "lo."]
    call = client.calls[0]
    assert call["model"] == "m"
    assert call["system"] == "sys"
    assert call["output_config"] == {"effort": "low"}
    assert call["messages"] == [{"role": "user", "content": "hi"}]


def test_refusal_raises_llm_refusal():
    chat, _ = make_chat(FakeStream([], stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        collect(chat, "hi")


def test_connection_error_becomes_llm_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    chat, _ = make_chat(FakeStream([], error=anthropic.APIConnectionError(request=request)))
    with pytest.raises(LLMError, match="reach"):
        collect(chat, "hi")


def test_create_chat_builds_claude_for_anthropic_provider():
    from desk_buddy.config import Settings
    from desk_buddy.llm import create_chat

    assert isinstance(create_chat(Settings(anthropic_api_key="k")), ClaudeChat)


def test_system_prompt_mentions_limits():
    prompt = system_prompt(max_sentences=3)
    assert "3" in prompt
    assert "markdown" in prompt.lower()
