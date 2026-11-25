"""CARL's editable character and signature lines: limits, reset, a corrupt file, and the
locked rules that are always in the prompt."""

import json
from datetime import date

import pytest

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import CUT_OFF_NOTE_START, INTERRUPTED_NOTE
from desk_buddy.brain.personality import (
    ABOUT_FIELDS,
    DEFAULT_CHARACTER,
    DEFAULT_LINES,
    GREETING,
    LINE_NAMES,
    About,
    Personality,
    system_prompt,
)
from desk_buddy.personality_store import (
    ABOUT_LIMITS,
    MAX_CHARACTER,
    MAX_LINE,
    PersonalityStore,
)

DAY = date(2026, 11, 25)


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
    assert view["limits"] == {
        "character": MAX_CHARACTER,
        "line": MAX_LINE,
        "about": {"name": 60, "city": 60, "region": 60, "country": 2, "notes": 1000},
    }
    assert view["about"] == dict.fromkeys(ABOUT_FIELDS, "")


# --- About you ---

ME = {
    "name": "Marta",
    "city": "Leeds",
    "region": "West Yorkshire",
    "country": "GB",
    "notes": "I build robots.\nI like tea.",
}


def test_about_you_is_saved_and_survives_a_restart(store, tmp_path):
    p, errors = store.update({"about": ME})
    assert errors == {}
    assert p.about == About(**ME)
    assert PersonalityStore(tmp_path / "app" / "personality.json").current().about == p.about
    assert store.view()["about"] == ME


def test_about_you_fields_are_cleaned(store):
    p, errors = store.update(
        {"about": {"name": "  Marta ", "country": " gb ", "notes": "one\r\ntwo\tthree"}}
    )
    assert errors == {}
    assert (p.about.name, p.about.country, p.about.notes) == ("Marta", "GB", "one\ntwo\tthree")


def test_a_blank_about_field_clears_it(store, tmp_path):
    store.update({"about": {"name": "Marta", "city": "Leeds"}})
    p, errors = store.update({"about": {"name": "   ", "city": None}})
    assert errors == {}
    assert p.about == About()
    saved = json.loads((tmp_path / "app" / "personality.json").read_text(encoding="utf-8"))
    assert "about" not in saved


@pytest.mark.parametrize(
    ("about", "field"),
    [
        ({"name": "x" * (ABOUT_LIMITS["name"] + 1)}, "about.name"),
        ({"city": "x" * 61}, "about.city"),
        ({"region": "x" * 61}, "about.region"),
        ({"notes": "x" * (ABOUT_LIMITS["notes"] + 1)}, "about.notes"),
        ({"name": "two\nlines"}, "about.name"),
        ({"city": "Le\reds"}, "about.city"),
        ({"region": "a\tb"}, "about.region"),
        ({"notes": "bell\x07"}, "about.notes"),
        ({"notes": "del\x7f"}, "about.notes"),
        ({"country": "GBR"}, "about.country"),
        ({"country": "G"}, "about.country"),
        ({"country": "G1"}, "about.country"),
        ({"country": "ÉU"}, "about.country"),
        ({"name": 42}, "about.name"),
        ({"age": "42"}, "about.age"),
    ],
)
def test_invalid_about_fields_are_refused_and_nothing_is_saved(store, tmp_path, about, field):
    p, errors = store.update({"about": about})
    assert p is None
    assert field in errors
    assert not (tmp_path / "app" / "personality.json").exists()


def test_about_must_be_an_object(store):
    p, errors = store.update({"about": "Marta"})
    assert p is None and "about" in errors


def test_notes_may_span_lines_but_names_may_not(store):
    p, errors = store.update({"about": {"notes": "a\nb"}})
    assert errors == {} and p.about.notes == "a\nb"


def test_reset_one_about_field(store):
    store.update({"about": ME})
    p = store.reset("about.city")
    assert p.about.city == ""
    assert p.about.name == "Marta"
    with pytest.raises(KeyError):
        store.reset("about.age")
    with pytest.raises(KeyError):
        store.reset("about")


def test_bad_about_values_in_the_file_are_skipped(tmp_path):
    path = tmp_path / "personality.json"
    path.write_text(
        json.dumps({"about": {"name": "Marta", "country": "Great Britain", "shoe": "9"}}),
        encoding="utf-8",
    )
    store = PersonalityStore(path)
    assert store.current().about == About(name="Marta")
    assert store.error and "about.country" in store.error


def test_an_about_that_isnt_an_object_in_the_file_is_skipped(tmp_path):
    path = tmp_path / "personality.json"
    path.write_text(json.dumps({"about": ["Marta"]}), encoding="utf-8")
    store = PersonalityStore(path)
    assert store.current().about == About()
    assert store.error


def test_location_has_only_the_filled_in_parts():
    assert About().location == {}
    assert About(city="Leeds", country="GB").location == {"city": "Leeds", "country": "GB"}
    assert About(**ME).location == {"city": "Leeds", "region": "West Yorkshire", "country": "GB"}


# --- About you in the prompt ---


def test_a_blank_about_leaves_the_prompt_unchanged():
    plain = system_prompt(2, today=DAY)
    assert system_prompt(2, today=DAY, about=About()) == plain
    assert system_prompt(2, today=DAY, about=None) == plain
    assert "About the user" not in plain


def test_the_about_block_sits_between_the_character_and_the_rules():
    prompt = system_prompt(2, today=DAY, about=About(**ME))
    character = prompt.index(DEFAULT_CHARACTER)
    block = prompt.index("About the user")
    rules = prompt.index("spoken aloud")
    assert character < block < rules
    assert "Name: Marta" in prompt
    assert "Leeds, West Yorkshire, GB" in prompt
    assert "I build robots.\nI like tea." in prompt
    assert "not something to recite" in prompt
    assert "Don't use their name in every reply" in prompt


def test_only_filled_in_about_fields_appear():
    prompt = system_prompt(2, today=DAY, about=About(city="Leeds"))
    assert "About the user" in prompt
    assert "Lives in: Leeds" in prompt
    assert "Name:" not in prompt
    assert "In their own words" not in prompt


def test_the_about_text_is_never_formatted():
    text = "I say 100% of {what} I mean, {0} and %s."
    prompt = system_prompt(2, today=DAY, about=About(name="{name}", notes=text))
    assert "Name: {name}" in prompt
    assert text in prompt


def test_prompt_for_includes_the_about_block():
    from desk_buddy.config import Settings
    from desk_buddy.llm import prompt_for

    p = Personality(about=About(name="Marta"))
    prompt = prompt_for(Settings(anthropic_api_key="k"), None, p)
    assert "Name: Marta" in prompt
