import asyncio
import contextlib
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import Settings
from desk_buddy.llm import ClaudeChat, LLMError, LLMRefusal, create_chat

URL = "https://api.anthropic.com/v1/messages"


class FakeStream:
    def __init__(self, deltas, stop_reason="end_turn", error=None, stream_error=None):
        self._deltas, self._stop_reason, self._error = deltas, stop_reason, error
        self._stream_error = stream_error
        self.closed = False

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d
            if self._stream_error:
                raise self._stream_error

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


def status_error(cls, status: int):
    request = httpx2.Request("POST", URL)
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


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


def test_max_tokens_logs_warning_and_does_not_raise(caplog):
    chat, _ = make_chat(FakeStream(["Hi"], stop_reason="max_tokens"))
    with caplog.at_level("WARNING", logger="desk_buddy.llm"):
        assert collect(chat, "hi") == ["Hi"]
    assert "max_tokens" in caplog.text


def test_connection_error_becomes_llm_error():
    request = httpx2.Request("POST", URL)
    chat, _ = make_chat(FakeStream([], error=anthropic.APIConnectionError(request=request)))
    with pytest.raises(LLMError, match="reach"):
        collect(chat, "hi")


def test_rate_limit_becomes_llm_error():
    chat, _ = make_chat(FakeStream([], error=status_error(anthropic.RateLimitError, 429)))
    with pytest.raises(LLMError, match="rate limited"):
        collect(chat, "hi")


@pytest.mark.parametrize(
    ("cls", "status"),
    [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403)],
)
def test_auth_errors_become_key_rejected(cls, status):
    chat, _ = make_chat(FakeStream([], error=status_error(cls, status)))
    with pytest.raises(LLMError, match="API key rejected"):
        collect(chat, "hi")


def test_other_status_error_reports_status():
    chat, _ = make_chat(FakeStream([], error=status_error(anthropic.InternalServerError, 500)))
    with pytest.raises(LLMError, match="API error 500"):
        collect(chat, "hi")


def test_unmapped_api_error_becomes_llm_error():
    error = anthropic.APIError("weird", httpx2.Request("POST", URL), body=None)
    chat, _ = make_chat(FakeStream([], error=error))
    with pytest.raises(LLMError) as info:
        collect(chat, "hi")
    assert type(info.value) is LLMError


def test_error_mid_stream_after_text():
    request = httpx2.Request("POST", URL)
    error = anthropic.APIConnectionError(request=request)
    chat, _ = make_chat(FakeStream(["Hel"], stream_error=error))
    got: list[str] = []

    async def run():
        async for d in chat.stream_reply("hi"):
            got.append(d)

    with pytest.raises(LLMError):
        asyncio.run(run())
    assert got == ["Hel"]


def test_breaking_out_early_closes_stream():
    stream = FakeStream(["a", "b", "c"])
    chat, _ = make_chat(stream)

    async def run():
        async with contextlib.aclosing(chat.stream_reply("hi")) as agen:
            async for _ in agen:
                break

    asyncio.run(run())
    assert stream.closed


def test_create_chat_builds_claude_for_anthropic_provider():
    assert isinstance(create_chat(Settings(anthropic_api_key="k")), ClaudeChat)


def test_system_prompt_mentions_limits():
    prompt = system_prompt(max_sentences=3)
    assert "3" in prompt
    assert "markdown" in prompt.lower()
