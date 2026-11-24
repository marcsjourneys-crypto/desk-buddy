import asyncio
import re
from datetime import date

import anthropic
import pytest

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import Conversation
from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat
from desk_buddy.tools.notes import NoteVault, note_tools
from desk_buddy.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

LEADING_TAG = re.compile(r"\s*\[(" + "|".join(EMOTIONS) + r")\]")


def live_chat(tools: ToolRegistry | None = None) -> ClaudeChat:
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    return ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=lambda: system_prompt(settings.max_sentences, notes=bool(tools)),
        tools=tools,
    )


def test_live_note_round_trip(tmp_path):
    vault = NoteVault(tmp_path)
    chat = live_chat(ToolRegistry(note_tools(vault)))
    conversation = Conversation()

    async def ask(text):
        reply = "".join([d async for d in chat.stream_reply(conversation.messages(), text)])
        conversation.add(text, reply)
        return reply

    async def run():
        await ask("Take a note: buy batteries.")
        return await ask("What did I note today?")

    summary = asyncio.run(run())
    daily = tmp_path / "Daily" / f"{date.today().isoformat()}.md"
    assert "batteries" in daily.read_text(encoding="utf-8").lower()
    assert "batter" in summary.lower(), summary


def test_live_streaming_reply():
    chat = live_chat()

    async def run():
        return "".join([d async for d in chat.stream_reply([], "Say just the word pineapple.")])

    assert "pineapple" in asyncio.run(run()).lower()


def test_follow_up_question_uses_memory_and_tags():
    chat = live_chat()
    conversation = Conversation()

    async def ask(text):
        reply = "".join([d async for d in chat.stream_reply(conversation.messages(), text)])
        conversation.add(text, reply)
        return reply

    async def run():
        return await ask("Tell me about Labrador Retrievers."), await ask("Are they good hunters?")

    first, second = asyncio.run(run())
    assert LEADING_TAG.match(first), first
    assert LEADING_TAG.match(second), second
    assert any(w in second.lower() for w in ("dog", "lab", "retriev", "bird", "duck")), second
