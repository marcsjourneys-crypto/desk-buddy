"""Real replies on the smart model. Skipped by default (integration); a few cents a run,
so run it only when the user asks."""

import asyncio

import anthropic
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.brain.smart import ASKED
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat

pytestmark = pytest.mark.integration

SMART = "claude-sonnet-5-5"


def _settings():
    try:
        return load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")


def _chat(settings, **kwargs) -> ClaudeChat:
    return ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=lambda: system_prompt(settings.max_sentences, web=True, think_harder=True),
        plain_system=lambda: system_prompt(settings.max_sentences, web=True),
        web_search_max=2,  # offered, whatever WEB_SEARCH says
        smart_model=SMART,
        **kwargs,
    )


def _reply(chat: ClaudeChat, question: str) -> str:
    async def run():
        return "".join([d async for d in chat.stream_reply([], question)])

    return asyncio.run(run())


def test_an_asked_question_is_answered_on_the_smart_model_with_search_offered():
    chat = _chat(_settings())
    chat.smart_route = ASKED
    reply = _reply(chat, "Think hard: what's 17 times 23, minus the square root of 144?")
    assert reply.strip(), "no text came back"
    assert chat.answered_by == (SMART, ASKED), f"answered by {chat.answered_by}; reply: {reply}"


def test_haiku_may_hand_a_hard_maths_question_over():
    chat = _chat(_settings())
    reply = _reply(
        chat,
        "If a, b and c are positive integers with a + b + c = 30 and a*b*c as large as "
        "possible subject to a, b, c all being distinct primes, what is a*b*c?",
    )
    assert reply.strip(), "no text came back"
    print(f"\nanswered_by: {chat.answered_by} (None means Haiku answered itself)")
    print(f"reply: {reply}")
