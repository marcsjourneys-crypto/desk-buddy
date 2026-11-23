import asyncio

import anthropic
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat

pytestmark = pytest.mark.integration


def test_live_streaming_reply():
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    chat = ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=system_prompt(settings.max_sentences),
    )

    async def run():
        return "".join([d async for d in chat.stream_reply("Say just the word pineapple.")])

    assert "pineapple" in asyncio.run(run()).lower()
