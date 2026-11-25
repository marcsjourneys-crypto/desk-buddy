import asyncio
import contextlib
import threading
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from desk_buddy.config import Settings
from desk_buddy.llm import (
    INTERRUPTED_RESULT,
    MAX_TOOL_ROUNDS,
    ClaudeChat,
    LLMError,
    LLMRefusal,
    create_chat,
)
from desk_buddy.tools.registry import Tool, ToolError, ToolRegistry

URL = "https://api.anthropic.com/v1/messages"


class FakeStream:
    def __init__(self, deltas, stop_reason="end_turn", error=None, stream_error=None, content=None):
        self._deltas, self._stop_reason, self._error = deltas, stop_reason, error
        self._content = content if content is not None else []
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
        return SimpleNamespace(stop_reason=self._stop_reason, content=self._content)


class FakeClient:
    def __init__(self, *streams: FakeStream):
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._streams = list(streams)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._streams.pop(0)


def collect(chat: ClaudeChat, text: str, history=()) -> list[str]:
    async def run():
        return [d async for d in chat.stream_reply(list(history), text)]

    return asyncio.run(run())


def make_chat(*streams: FakeStream, tools=None) -> tuple[ClaudeChat, FakeClient]:
    client = FakeClient(*streams)
    return ClaudeChat(client, model="m", effort="low", system="sys", tools=tools), client


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


def test_history_is_sent_before_the_new_message():
    chat, client = make_chat(FakeStream(["ok"]))
    history = [
        {"role": "user", "content": "tell me about labs"},
        {"role": "assistant", "content": "[happy] Labs are great."},
    ]
    collect(chat, "are they good hunters?", history)
    assert client.calls[0]["messages"] == [
        *history,
        {"role": "user", "content": "are they good hunters?"},
    ]
    assert len(history) == 2  # caller's list is not modified


def test_refusal_raises_llm_refusal():
    chat, _ = make_chat(FakeStream([], stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        collect(chat, "hi")


def test_max_tokens_logs_warning_and_does_not_raise(caplog):
    chat, _ = make_chat(FakeStream(["Hi"], stop_reason="max_tokens"))
    with caplog.at_level("WARNING", logger="desk_buddy.llm"):
        assert collect(chat, "hi") == ["Hi"]
    assert "max_tokens" in caplog.text


def test_max_tokens_before_any_text_is_an_error():
    # e.g. the budget ran out on thinking or a tool call: silence would be confusing
    chat, _ = make_chat(FakeStream([], stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="ran out of room"):
        collect(chat, "hi")


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
        async for d in chat.stream_reply([], "hi"):
            got.append(d)

    with pytest.raises(LLMError):
        asyncio.run(run())
    assert got == ["Hel"]


def test_breaking_out_early_closes_stream():
    stream = FakeStream(["a", "b", "c"])
    chat, _ = make_chat(stream)

    async def run():
        async with contextlib.aclosing(chat.stream_reply([], "hi")) as agen:
            async for _ in agen:
                break

    asyncio.run(run())
    assert stream.closed


def test_create_chat_builds_claude_for_anthropic_provider():
    assert isinstance(create_chat(Settings(anthropic_api_key="k")), ClaudeChat)


def _tool(name):
    return Tool(name, "d", SCHEMA, lambda text: "ok")


@pytest.mark.parametrize(
    ("names", "notes", "music"),
    [
        ([], False, False),
        (["take_note", "read_notes"], True, False),
        (["play_music"], False, True),
        (["take_note", "play_music"], True, True),
    ],
)
def test_create_chat_tells_the_prompt_which_tools_exist(names, notes, music):
    chat = create_chat(Settings(anthropic_api_key="k"), ToolRegistry([_tool(n) for n in names]))
    prompt = chat._system()
    assert ("take_note" in prompt) is notes
    assert ("play_music" in prompt) is music


SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def note_registry(calls):
    def take_note(text):
        calls.append(text)
        return "Saved"

    return ToolRegistry([Tool("take_note", "d", SCHEMA, take_note)])


def tool_use(id="t1", text="buy milk"):
    return SimpleNamespace(type="tool_use", id=id, name="take_note", input={"text": text})


def test_no_tools_sends_no_tool_params():
    chat, client = make_chat(FakeStream(["hi"]))
    collect(chat, "hi")
    assert "tools" not in client.calls[0]
    assert "tool_choice" not in client.calls[0]


def test_tool_round_trip_streams_text_around_the_tool():
    calls = []
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = FakeStream(
        ["Let me note that."], stop_reason="tool_use", content=[thinking, tool_use()]
    )
    second = FakeStream(["Noted."])
    chat, client = make_chat(first, second, tools=note_registry(calls))
    assert collect(chat, "note buy milk") == ["Let me note that.", "\n", "Noted."]
    assert calls == ["buy milk"]
    assert client.calls[0]["tools"][0]["name"] == "take_note"
    sent = client.calls[1]["messages"]
    assert sent[-2] == {"role": "assistant", "content": [thinking, tool_use()]}
    assert sent[-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Saved"}],
    }


def test_parallel_tool_calls_return_in_one_message():
    calls = []
    first = FakeStream([], stop_reason="tool_use", content=[tool_use("a", "x"), tool_use("b", "y")])
    chat, client = make_chat(first, FakeStream(["ok"]), tools=note_registry(calls))
    collect(chat, "two notes")
    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert calls == ["x", "y"]
    assert chat.tools_succeeded == 2


def test_each_reply_records_its_tool_rounds_as_plain_api_blocks():
    calls = []
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = FakeStream(
        ["Let me note that."], stop_reason="tool_use", content=[thinking, tool_use()]
    )
    chat, _ = make_chat(first, FakeStream(["Noted."]), tools=note_registry(calls))
    collect(chat, "note buy milk")
    assert chat.last_tool_rounds == [
        (
            [{"type": "tool_use", "id": "t1", "name": "take_note", "input": {"text": "buy milk"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "content": "Saved"}],
        )
    ]
    # A reply without tools starts afresh.
    chat._client._streams.append(FakeStream(["Hi."]))
    collect(chat, "hello")
    assert chat.last_tool_rounds == []


def test_history_ending_in_tool_results_joins_the_new_user_message():
    chat, client = make_chat(FakeStream(["ok"]))
    leftover = [{"type": "tool_result", "tool_use_id": "t1", "content": "Saved"}]
    history = [
        {"role": "user", "content": "note it"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
        {"role": "user", "content": leftover},
    ]
    collect(chat, "and now?", history=history)
    assert client.calls[0]["messages"][-1] == {
        "role": "user",
        "content": [*leftover, {"type": "text", "text": "and now?"}],
    }


def test_tools_succeeded_starts_at_zero_and_skips_failed_tools():
    def broken(text):
        raise ToolError("disk full")

    registry = ToolRegistry([Tool("take_note", "d", SCHEMA, broken)])
    first = FakeStream([], stop_reason="tool_use", content=[tool_use()])
    chat, _ = make_chat(first, FakeStream(["Sorry."]), tools=registry)
    assert chat.tools_succeeded == 0
    collect(chat, "note it")
    assert chat.tools_succeeded == 0


def test_tool_error_is_sent_back_flagged():
    def broken(text):
        raise ToolError("disk full")

    registry = ToolRegistry([Tool("take_note", "d", SCHEMA, broken)])
    first = FakeStream([], stop_reason="tool_use", content=[tool_use()])
    chat, client = make_chat(first, FakeStream(["Sorry."]), tools=registry)
    collect(chat, "note it")
    assert client.calls[1]["messages"][-1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "disk full",
        "is_error": True,
    }


def test_last_round_forbids_tools():
    calls = []
    looping = [
        FakeStream([], stop_reason="tool_use", content=[tool_use(str(i))])
        for i in range(MAX_TOOL_ROUNDS - 1)
    ]
    chat, client = make_chat(*looping, FakeStream(["Done."]), tools=note_registry(calls))
    assert collect(chat, "go") == ["Done."]
    assert len(client.calls) == MAX_TOOL_ROUNDS
    assert "tool_choice" not in client.calls[0]
    assert client.calls[-1]["tool_choice"] == {"type": "none"}


def test_refusal_stops_before_any_tool_runs():
    calls = []
    first = FakeStream([], stop_reason="refusal", content=[tool_use()])
    chat, _ = make_chat(first, tools=note_registry(calls))
    with pytest.raises(LLMRefusal):
        collect(chat, "x")
    assert calls == []


def test_system_prompt_can_be_rebuilt_per_request():
    stamps = iter(["s1", "s2"])
    client = FakeClient(FakeStream(["a"]), FakeStream(["b"]))
    chat = ClaudeChat(client, model="m", effort="low", system=lambda: next(stamps))
    collect(chat, "one")
    collect(chat, "two")
    assert [c["system"] for c in client.calls] == ["s1", "s2"]


def test_breaking_out_in_a_later_round_closes_that_stream():
    calls = []
    first = FakeStream(["One."], stop_reason="tool_use", content=[tool_use()])
    second = FakeStream(["Two", "three"])
    chat, _ = make_chat(first, second, tools=note_registry(calls))

    async def run():
        async with contextlib.aclosing(chat.stream_reply([], "hi")) as agen:
            async for d in agen:
                if d == "Two":
                    break

    asyncio.run(run())
    assert first.closed and second.closed


def test_tool_use_stop_without_tool_blocks_ends_the_reply():
    chat, client = make_chat(FakeStream(["Hm."], stop_reason="tool_use"), tools=note_registry([]))
    assert collect(chat, "x") == ["Hm."]
    assert len(client.calls) == 1


def test_cancel_while_a_tool_runs_propagates():
    started, release = threading.Event(), threading.Event()

    def slow(text):
        started.set()
        release.wait(5)
        return "Saved"

    registry = ToolRegistry([Tool("take_note", "d", SCHEMA, slow)])
    first = FakeStream(["One."], stop_reason="tool_use", content=[tool_use()])
    chat, client = make_chat(first, FakeStream(["never"]), tools=registry)

    async def consume():
        async with contextlib.aclosing(chat.stream_reply([], "hi")) as agen:
            async for _ in agen:
                pass

    async def run():
        task = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(run())
    assert first.closed
    assert len(client.calls) == 1
    # The tool was cut off mid-run: it's still recorded, with a placeholder result.
    assert chat.last_tool_rounds == [
        (
            [{"type": "tool_use", "id": "t1", "name": "take_note", "input": {"text": "buy milk"}}],
            [{"type": "tool_result", "tool_use_id": "t1", "content": INTERRUPTED_RESULT}],
        )
    ]


def test_a_multi_tool_round_cancelled_midway_records_every_call():
    started, release = threading.Event(), threading.Event()
    done = []

    def note(text):
        if text == "slow":
            started.set()
            release.wait(5)
        done.append(text)
        return f"Saved {text}"

    registry = ToolRegistry([Tool("take_note", "d", SCHEMA, note)])
    uses = [tool_use("a", "fast"), tool_use("b", "slow"), tool_use("c", "never")]
    first = FakeStream([], stop_reason="tool_use", content=uses)
    chat, _ = make_chat(first, FakeStream(["never"]), tools=registry)

    async def consume():
        async with contextlib.aclosing(chat.stream_reply([], "hi")) as agen:
            async for _ in agen:
                pass

    async def run():
        task = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(run())
    ((recorded_uses, results),) = chat.last_tool_rounds
    assert [u["id"] for u in recorded_uses] == ["a", "b", "c"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert results[0]["content"] == "Saved fast"
    assert results[1]["content"] == INTERRUPTED_RESULT
    assert results[2]["content"] == INTERRUPTED_RESULT
