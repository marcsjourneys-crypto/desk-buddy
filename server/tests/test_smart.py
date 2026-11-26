"""When a question goes to the smart model: the spoken phrases and the stay."""

import pytest

from desk_buddy.brain.smart import (
    ASKED,
    FOLLOW_ON,
    SMART_PHRASES,
    STAY_SECONDS,
    SmartRouter,
    wants_smart,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("phrase", [" ".join(p) for p in SMART_PHRASES])
def test_each_phrase_matches_in_a_sentence(phrase):
    assert wants_smart(phrase)
    assert wants_smart(f"Okay CARL, {phrase}, what's 17 times 23?")


@pytest.mark.parametrize(
    "text",
    ["Can you THINK HARD about this?", "Think it through, please.", "BIG BRAIN time!"],
)
def test_phrases_match_in_any_case(text):
    assert wants_smart(text)


@pytest.mark.parametrize(
    "text", ["I think hardware is cool", "That's brainy", "I think so", "think", "", "hard think"]
)
def test_other_words_dont_match(text):
    assert not wants_smart(text)


def test_no_route_at_first():
    assert SmartRouter(clock=FakeClock()).route("what time is it?") is None


def test_a_smart_answer_starts_the_stay():
    clock = FakeClock()
    router = SmartRouter(clock=clock)
    router.answered(True)
    clock.now += 119
    assert router.route("and divided by 7?") == FOLLOW_ON
    clock.now += 2  # 121 s after the answer
    assert router.route("and divided by 7?") is None


def test_each_smart_answer_restarts_the_stay():
    clock = FakeClock()
    router = SmartRouter(clock=clock)
    router.answered(True)
    clock.now += 100
    router.answered(True)
    clock.now += 100
    assert router.route("more?") == FOLLOW_ON
    assert STAY_SECONDS == 120


def test_a_haiku_answer_ends_the_stay():
    clock = FakeClock()
    router = SmartRouter(clock=clock)
    router.answered(True)
    router.answered(False)
    assert router.route("more?") is None


def test_forget_ends_the_stay():
    router = SmartRouter(clock=FakeClock())
    router.answered(True)
    router.forget()
    assert router.route("more?") is None


def test_a_phrase_is_asked_even_during_a_stay():
    router = SmartRouter(clock=FakeClock())
    router.answered(True)
    assert router.route("think harder about that") == ASKED
    assert SmartRouter(clock=FakeClock()).route("think hard about X") == ASKED
