import asyncio
import re

import anthropic
import pytest

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import Conversation
from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat

pytestmark = pytest.mark.integration

LEADING_TAG = re.compile(r"\s*\[(" + "|".join(EMOTIONS) + r")\]")


def live_chat() -> ClaudeChat:
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    return ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=system_prompt(settings.max_sentences),
    )


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
