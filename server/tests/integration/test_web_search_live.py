"""One real reply with Anthropic's web search on. Skipped by default (integration)."""

import asyncio

import anthropic
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat, Search

pytestmark = pytest.mark.integration


def test_live_reply_reports_a_web_search():
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    reported: list[Search] = []
    chat = ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=lambda: system_prompt(settings.max_sentences, web=True),
        web_search_max=2,  # forced on, whatever WEB_SEARCH says
        on_search=reported.append,
    )

    async def run():
        question = "What is today's top news headline? Search the web."
        return "".join([d async for d in chat.stream_reply([], question)])

    reply = asyncio.run(run())
    if not chat.web_search:
        pytest.skip("web search isn't enabled for this API key (Anthropic Console)")
    assert reply.strip(), "no text came back"
    assert reported, f"no web search was reported; reply: {reply}"
    assert chat.searches == reported
    assert all(s.query for s in reported)
