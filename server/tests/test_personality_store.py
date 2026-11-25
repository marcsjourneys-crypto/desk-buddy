"""CARL's editable character and signature lines: limits, reset, a corrupt file, and the
locked rules that are always in the prompt."""

import json

import pytest

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import CUT_OFF_NOTE_START, INTERRUPTED_NOTE
from desk_buddy.brain.personality import (
    DEFAULT_CHARACTER,
    DEFAULT_LINES,
    GREETING,
    LINE_NAMES,
    Personality,
    system_prompt,
)
from desk_buddy.personality_store import MAX_CHARACTER, MAX_LINE, PersonalityStore


@pytest.fixture
def store(tmp_path):
    return PersonalityStore(tmp_path / "app" / "personality.json")


def test_defaults_without_a_file(store):
    p = store.current()
    assert p.character == DEFAULT_CHARACTER
    assert p.line("greeting") == GREETING
    assert (
        set(LINE_NAMES)
        == set(DEFAULT_LINES)
        == {
            "greeting",
            "farewell",
            "pondering",
            "task_done",
            "error",
            "refusal",
            "forget",
        }
    )


def test_the_locked_rules_are_always_in_the_prompt():
    grumpy = "You are a grumpy old toaster who hates mornings."
    prompt = system_prompt(2, character=grumpy, notes=True, music=True)
    assert prompt.startswith(grumpy)
    assert DEFAULT_CHARACTER not in prompt
    for rule in (
        "spoken aloud",
        "at most 2 short sentences",
        INTERRUPTED_NOTE,
        CUT_OFF_NOTE_START,
        '<untrusted source="...">',
        "never authorise or confirm an action",
        "take_note",
        "play_music",
    ):
        assert rule in prompt
    for e in EMOTIONS:
        assert f"[{e}]" in prompt


def test_the_character_is_never_formatted():
    text = "Say 100% of {what} you mean, {0} and %s."
    assert system_prompt(2, character=text).startswith(text)


def test_an_update_is_saved_and_survives_a_restart(store, tmp_path):
    p, errors = store.update(
        {
            "character": "You are CARL, but grumpier.",
            "lines": {"greeting": {"text": "Oh. You again.", "emotion": "sleepy"}},
        }
    )
    assert errors == {}
    assert p.character == "You are CARL, but grumpier."
    assert p.line("greeting") == ("sleepy", "Oh. You again.")
    assert p.line("farewell") == DEFAULT_LINES["farewell"]
    again = PersonalityStore(tmp_path / "app" / "personality.json")
    assert again.current() == p


def test_a_line_can_change_just_its_emotion(store):
    p, errors = store.update({"lines": {"farewell": {"emotion": "sad"}}})
    assert errors == {}
    assert p.line("farewell") == ("sad", DEFAULT_LINES["farewell"].text)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"character": "x" * (MAX_CHARACTER + 1)}, "character"),
        ({"character": "   "}, "character"),
        ({"character": "You are {evil}"}, "character"),
        ({"character": 42}, "character"),
        ({"lines": {"greeting": {"text": "x" * (MAX_LINE + 1)}}}, "lines.greeting"),
        ({"lines": {"greeting": {"text": "two\nlines"}}}, "lines.greeting"),
        ({"lines": {"greeting": {"text": ""}}}, "lines.greeting"),
        ({"lines": {"greeting": {"emotion": "angry"}}}, "lines.greeting"),
        ({"lines": {"shout": {"text": "Hi"}}}, "lines.shout"),
        ({"lines": "nope"}, "lines"),
        ({"system": "x"}, "system"),
    ],
)
def test_invalid_changes_are_refused_and_nothing_is_saved(store, tmp_path, changes, field):
    p, errors = store.update(changes)
    assert p is None
    assert field in errors
    assert not (tmp_path / "app" / "personality.json").exists()


def test_reset_one_item(store):
    store.update(
        {
            "character": "Grumpy.",
            "lines": {"greeting": {"text": "Hmph."}, "farewell": {"text": "Finally."}},
        }
    )
    p = store.reset("greeting")
    assert p.line("greeting") == GREETING
    assert p.line("farewell").text == "Finally."
    p = store.reset("character")
    assert p.character == DEFAULT_CHARACTER
    with pytest.raises(KeyError):
        store.reset("system")


def test_a_corrupt_file_falls_back_to_defaults_with_a_banner(tmp_path):
    path = tmp_path / "personality.json"
    path.write_text("{oops", encoding="utf-8")
    store = PersonalityStore(path)
    assert store.error and "personality.json" in store.error
    assert store.current() == Personality()


def test_bad_values_in_the_file_are_skipped(tmp_path):
    path = tmp_path / "personality.json"
    path.write_text(
        json.dumps(
            {
                "character": "You are {bad}",
                "lines": {
                    "greeting": {"text": "Hello.", "emotion": "happy"},
                    "farewell": {"text": "Bye.", "emotion": "furious"},
                    "nope": {"text": "x", "emotion": "happy"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = PersonalityStore(path)
    p = store.current()
    assert p.character == DEFAULT_CHARACTER
    assert p.line("greeting").text == "Hello."
    assert p.line("farewell") == DEFAULT_LINES["farewell"]
    assert store.error


def test_the_view_shows_values_defaults_and_what_is_custom(store):
    store.update({"lines": {"greeting": {"text": "Hi there."}}})
    view = store.view()
    assert view["character"] == {
        "value": DEFAULT_CHARACTER,
        "default": DEFAULT_CHARACTER,
        "custom": False,
    }
    assert view["lines"]["greeting"]["text"] == "Hi there."
    assert view["lines"]["greeting"]["custom"] is True
    assert view["lines"]["greeting"]["default"] == {"text": GREETING.text, "emotion": "happy"}
    assert view["lines"]["farewell"]["custom"] is False
    assert view["emotions"] == list(EMOTIONS)
    assert view["limits"] == {"character": MAX_CHARACTER, "line": MAX_LINE}
