from datetime import date

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import INTERRUPTED_NOTE
from desk_buddy.brain.personality import (
    ERROR_LINE,
    FORGET_LINE,
    REFUSAL_LINE,
    system_prompt,
)


def test_prompt_mentions_sentence_limit_and_spoken_style():
    prompt = system_prompt(max_sentences=3)
    assert "3 short sentences" in prompt
    assert "markdown" in prompt.lower()


def test_prompt_lists_every_emotion_tag():
    prompt = system_prompt(2)
    for e in EMOTIONS:
        assert f"[{e}]" in prompt


def test_prompt_explains_interrupted_marker():
    assert INTERRUPTED_NOTE in system_prompt(2)


def test_canned_lines_use_known_emotions_and_have_text():
    for line in (ERROR_LINE, REFUSAL_LINE, FORGET_LINE):
        assert line.emotion in EMOTIONS
        assert line.text.strip()


DAY = date(2026, 11, 23)


def test_prompt_includes_todays_date_and_weekday():
    prompt = system_prompt(2, today=DAY)
    assert "Monday 23 November 2026" in prompt
    assert "2026-11-23" in prompt


def test_prompt_mentions_note_tools_only_when_enabled():
    assert "take_note" in system_prompt(2, today=DAY, notes=True)
    off = system_prompt(2, today=DAY, notes=False)
    assert "take_note" not in off
    assert "not set up" in off
