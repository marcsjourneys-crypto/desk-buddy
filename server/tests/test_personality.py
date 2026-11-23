from desk_buddy.brain.emotion import EMOTIONS
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
    assert "(interrupted)" in system_prompt(2)


def test_canned_lines_use_known_emotions_and_have_text():
    for line in (ERROR_LINE, REFUSAL_LINE, FORGET_LINE):
        assert line.emotion in EMOTIONS
        assert line.text.strip()
