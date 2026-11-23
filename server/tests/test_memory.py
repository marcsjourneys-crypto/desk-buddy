import pytest

from desk_buddy.brain.memory import Conversation, is_forget_command


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
    ],
)
def test_near_misses_do_not_match(text):
    assert not is_forget_command(text)
