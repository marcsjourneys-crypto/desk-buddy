"""Tool calls are remembered as real API blocks, so on later turns Claude sees what it
actually did (a "Queen is playing" reply has the play_music call behind it)."""

from desk_buddy.brain.memory import (
    CUT_OFF_NOTE_START,
    INTERRUPTED_NOTE,
    MAX_STORED_RESULT,
    Conversation,
    merge_consecutive,
)
from desk_buddy.tools.untrusted import wrap


def use(id, name="play_music", **input):
    return {"type": "tool_use", "id": id, "name": name, "input": input or {"query": "Queen"}}


def result(id, content="Started playing on Spotify.", is_error=False):
    block = {"type": "tool_result", "tool_use_id": id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def check_api_order(messages):
    """Alternating roles, starting with the user, and every tool_use answered by a
    tool_result in the very next (user) message, with no orphan results."""
    assert messages[0]["role"] == "user"
    for a, b in zip(messages, messages[1:], strict=False):
        assert a["role"] != b["role"]
    for i, msg in enumerate(messages):
        content = msg["content"] if isinstance(msg["content"], list) else []
        uses = [b["id"] for b in content if b.get("type") == "tool_use"]
        results = [b["tool_use_id"] for b in content if b.get("type") == "tool_result"]
        if uses:
            nxt = messages[i + 1]["content"]
            answered = [b["tool_use_id"] for b in nxt if b.get("type") == "tool_result"]
            assert answered == uses
        if results:
            prev = messages[i - 1]["content"]
            asked = [b["id"] for b in prev if isinstance(b, dict) and b.get("type") == "tool_use"]
            assert results == asked
            # tool results come first in their message
            types = [b.get("type") for b in content]
            assert types[: len(results)] == ["tool_result"] * len(results)


def test_a_tool_turn_round_trips_in_api_order():
    c = Conversation()
    c.add(
        "play some queen", "[happy] Here's some Queen.", tool_rounds=[([use("t1")], [result("t1")])]
    )
    messages = c.messages()
    assert messages == [
        {"role": "user", "content": "play some queen"},
        {"role": "assistant", "content": [use("t1")]},
        {"role": "user", "content": [result("t1")]},
        {"role": "assistant", "content": "[happy] Here's some Queen."},
    ]
    check_api_order(messages)


def test_several_rounds_and_parallel_calls_keep_their_order():
    c = Conversation()
    rounds = [
        ([use("a"), use("b", "music_volume", level=30)], [result("a"), result("b")]),
        ([use("c", "now_playing")], [result("c", wrap("spotify", "Bicycle Race by Queen"))]),
    ]
    c.add("queen, quieter, what's on", "[happy] Done.", tool_rounds=rounds)
    messages = c.messages()
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    check_api_order(messages)


def test_trimming_old_turns_never_leaves_orphans():
    c = Conversation(max_turns=2)
    for n in range(5):
        c.add(f"q{n}", f"[happy] r{n}", tool_rounds=[([use(f"t{n}")], [result(f"t{n}")])])
    messages = c.messages()
    assert messages[0] == {"role": "user", "content": "q3"}
    assert len(c) == 2
    check_api_order(messages)


def test_a_long_tool_result_is_capped_with_its_untrusted_wrapper_intact():
    notes = wrap("notes", "Notes for today:\n" + "- 09:00 something long\n" * 300)
    c = Conversation()
    c.add(
        "what did I note",
        "[neutral] Lots.",
        tool_rounds=[([use("n", "read_notes")], [result("n", notes)])],
    )
    stored = c.messages()[2]["content"][0]["content"]
    assert len(stored) <= MAX_STORED_RESULT
    assert stored.startswith('<untrusted source="notes">\n')
    assert stored.endswith("(and more)\n</untrusted>")


def test_an_error_result_keeps_its_flag():
    c = Conversation()
    c.add(
        "play x",
        "[sad] Couldn't.",
        tool_rounds=[([use("t")], [result("t", "Nope.", is_error=True)])],
    )
    assert c.messages()[2]["content"][0]["is_error"] is True


def test_a_tool_turn_without_a_reply_becomes_a_note_on_the_next_user_message():
    # Cut off before CARL said anything: the action still happened. No dangling tool
    # blocks and no made-up assistant text: a note on the next user message instead.
    c = Conversation()
    c.add("play queen", None, interrupted=True, tool_rounds=[([use("t")], [result("t")])])
    assert c.messages() == []
    note = c.user_message("louder")
    assert note.startswith(CUT_OFF_NOTE_START)
    assert 'play_music {"query": "Queen"} -> Started playing on Spotify.' in note
    assert note.endswith(" louder")
    assert INTERRUPTED_NOTE not in note
    c.add("louder", "[happy] Louder it is.")
    messages = c.messages()
    assert messages == [
        {"role": "user", "content": note},
        {"role": "assistant", "content": "[happy] Louder it is."},
    ]
    # The note was used once: the message after that is plain.
    assert c.user_message("thanks") == "thanks"


def test_the_note_keeps_untrusted_results_well_formed_and_short():
    c = Conversation()
    long_result = "Started playing on Spotify: " + wrap("spotify", "x" * 2000)
    c.add("play x", None, tool_rounds=[([use("t")], [result("t", long_result)])])
    note = c.user_message("hi")
    assert len(note) < 1000
    assert note.count('<untrusted source="spotify">') == 1
    assert note.count("</untrusted>") == 1


def test_notes_from_several_cut_off_turns_add_up_and_forget_clears_them():
    c = Conversation()
    c.add("play queen", None, tool_rounds=[([use("a")], [result("a")])])
    c.add(
        "pause",
        None,
        tool_rounds=[([use("b", "music_control", action="pause")], [result("b", "Paused.")])],
    )
    note = c.user_message("hi")
    assert "play_music" in note and "music_control" in note
    c.forget()
    assert c.user_message("hi") == "hi"


def test_a_cut_off_turn_without_tools_leaves_no_note():
    c = Conversation()
    c.add("hello", None)
    assert c.messages() == []
    assert c.user_message("hi") == "hi"


def test_merge_consecutive_joins_user_messages_tool_results_first():
    merged = merge_consecutive(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [use("t")]},
            {"role": "user", "content": [result("t")]},
            {"role": "user", "content": "next"},
        ]
    )
    assert merged[-1] == {
        "role": "user",
        "content": [result("t"), {"type": "text", "text": "next"}],
    }


def test_forget_clears_tool_turns_too():
    c = Conversation()
    c.add("play queen", "[happy] Queen.", tool_rounds=[([use("t")], [result("t")])])
    c.forget()
    assert c.messages() == []


def test_messages_are_copies():
    c = Conversation()
    c.add("play queen", "[happy] Queen.", tool_rounds=[([use("t")], [result("t")])])
    c.messages()[1]["content"].append("junk")
    assert c.messages()[1]["content"] == [use("t")]
