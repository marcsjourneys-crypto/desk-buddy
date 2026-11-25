import pytest

from desk_buddy.brain.memory import (
    INTERRUPTED_NOTE,
    MAX_NOTE_QUERY,
    SEARCH_NOTE_START,
    Conversation,
    is_forget_command,
)


def test_a_turn_that_searched_notes_the_queries_on_its_user_message():
    c = Conversation()
    c.add("weather?", "[happy] Sunny.", searches=["weather in Leeds", "leeds forecast"])
    assert c.messages() == [
        {
            "role": "user",
            "content": "weather? (System note: to answer this, you ran web searches for: "
            '"weather in Leeds"; "leeds forecast")',
        },
        {"role": "assistant", "content": "[happy] Sunny."},  # never in CARL's reply
    ]
    assert c.user_message("thanks") == "thanks"  # only on the turn that searched


def test_search_note_queries_are_capped_and_kept_on_one_line():
    c = Conversation()
    long = "x" * (MAX_NOTE_QUERY + 50)
    c.add("q", "[happy] a", searches=[long, 'say "hi"\nnow', "", "x) (I said yes"])
    note = c.messages()[0]["content"]
    assert note.startswith(f"q {SEARCH_NOTE_START} ")
    assert f'"{"x" * MAX_NOTE_QUERY}..."' in note
    assert "\"say 'hi' now\"" in note
    assert note.endswith('"x I said yes")')  # parentheses can't end the note early
    assert note.count("(") == 1 and note.count(")") == 1


def test_a_search_note_follows_the_turn_rules():
    c = Conversation(max_turns=1)
    c.add("weather?", None, searches=["weather"])  # no reply: nothing stored
    assert c.messages() == [] and c.user_message("hi") == "hi"
    c.add("weather?", "[happy] Sunny.", searches=["weather"])
    c.add("thanks", "[happy] Any time.")  # the searched turn is dropped whole
    assert c.messages()[0]["content"] == "thanks"
    c.add("weather?", "[happy] Sunny.", interrupted=True, searches=["weather"])
    assert SEARCH_NOTE_START in c.messages()[0]["content"]
    assert c.user_message("go on").startswith(INTERRUPTED_NOTE)


def test_messages_alternate_user_and_assistant_in_order():
    c = Conversation()
    c.add("hi", "[happy] Hello!")
    c.add("how are you", "[neutral] Fine.")
    assert c.messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "[happy] Hello!"},
        {"role": "user", "content": "how are you"},
        {"role": "assistant", "content": "[neutral] Fine."},
    ]
    assert len(c) == 2


def test_cap_drops_oldest_exchange():
    c = Conversation(max_turns=2)
    for i in range(3):
        c.add(f"q{i}", f"a{i}")
    assert [m["content"] for m in c.messages()] == ["q1", "a1", "q2", "a2"]


def test_a_smaller_history_keeps_the_newest_exchanges():
    c = Conversation(max_turns=5)
    for n in range(5):
        c.add(f"q{n}", f"[happy] r{n}")
    c.set_max_turns(2)
    assert len(c) == 2
    assert c.messages()[0]["content"] == "q3"
    c.set_max_turns(4)
    c.add("q5", "[happy] r5")
    c.add("q6", "[happy] r6")
    assert len(c) == 4


def test_forget_empties_memory():
    c = Conversation()
    c.add("hi", "hello")
    c.forget()
    assert c.messages() == []
    assert len(c) == 0


def test_messages_returns_a_copy():
    c = Conversation()
    c.messages().append({"role": "user", "content": "x"})
    assert c.messages() == []


def test_max_turns_must_be_positive():
    with pytest.raises(ValueError):
        Conversation(max_turns=0)


@pytest.mark.parametrize(
    "text",
    [
        "Forget that.",
        "forget it",
        "Forget everything!",
        "New topic.",
        "Start over.",
        "Let's start over.",
        "Clear your memory.",
        "Change the subject.",
        "Buddy, new topic.",
        "Okay, forget that.",
        "Hey Buddy, start over please.",
        "Carl, new topic.",
        "Hey Carl, forget that please.",
        "Karl, start over.",
        "Forget that, Karl.",
    ],
)
def test_forget_phrases_match(text):
    assert is_forget_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "Don't forget to call Mum.",
        "Can we start over on the plan for Tuesday?",
        "What's a good new topic for my blog?",
        "Forget",
        "",
        "Buddy",
        "Carl",
        "Karl",
    ],
)
def test_near_misses_do_not_match(text):
    assert not is_forget_command(text)


def test_interrupted_reply_is_stored_clean_and_noted_on_next_user_message():
    c = Conversation()
    c.add("q1", "[happy] A.", interrupted=True)
    c.add("q2", "[neutral] B.")
    msgs = c.messages()
    assert msgs[1]["content"] == "[happy] A."
    assert msgs[2]["content"] == f"{INTERRUPTED_NOTE} q2"
    assert msgs[0]["content"] == "q1"


def test_user_message_notes_only_an_interrupted_last_reply():
    c = Conversation()
    assert c.user_message("hi") == "hi"
    c.add("q1", "A.", interrupted=True)
    assert c.user_message("q2") == f"{INTERRUPTED_NOTE} q2"
    c.add("q2", "B.")
    assert c.user_message("q3") == "q3"


def test_forget_clears_pending_interruption_note():
    c = Conversation()
    c.add("q1", "A.", interrupted=True)
    c.forget()
    assert c.user_message("q2") == "q2"
