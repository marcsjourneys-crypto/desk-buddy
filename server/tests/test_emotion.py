import pytest

from desk_buddy.brain.emotion import DEFAULT_EMOTION, EMOTIONS, EmotionParser


def run(chunks):
    p = EmotionParser()
    out = "".join(p.feed(c) for c in chunks) + p.flush()
    return p.emotion, out


def test_leading_tag_is_stripped_and_reported():
    assert run(["[happy] Hi there."]) == ("happy", "Hi there.")


def test_tag_split_across_chunks():
    assert run(["[", "hap", "py]", " ", "Hi."]) == ("happy", "Hi.")


def test_leading_whitespace_before_tag():
    assert run(["\n [curious] Ooh."]) == ("curious", "Ooh.")


def test_tag_is_case_insensitive():
    assert run(["[Surprised] Wow."]) == ("surprised", "Wow.")


def test_missing_tag_is_neutral_and_text_passes_through():
    assert run(["Hello there."]) == (DEFAULT_EMOTION, "Hello there.")


def test_unknown_tag_is_neutral_and_kept():
    assert run(["[angry] Grr."]) == (DEFAULT_EMOTION, "[angry] Grr.")


def test_non_tag_brackets_are_kept():
    assert run(["[1] see above."]) == (DEFAULT_EMOTION, "[1] see above.")


def test_known_tags_mid_reply_are_removed():
    assert run(["[happy] Hi! [sad] Bye."]) == ("happy", "Hi!  Bye.")


def test_mid_reply_tag_split_across_chunks_is_removed():
    assert run(["[happy] Hi! [sur", "prised] Oh."]) == ("happy", "Hi!  Oh.")


def test_emotion_is_known_as_soon_as_the_tag_completes():
    p = EmotionParser()
    assert p.feed("[thin") == ""
    assert p.emotion is None
    p.feed("king] Hmm")
    assert p.emotion == "thinking"


def test_empty_reply_is_neutral():
    assert run([]) == (DEFAULT_EMOTION, "")


def test_lone_bracket_at_end_is_released_on_flush():
    assert run(["Hi ["]) == (DEFAULT_EMOTION, "Hi [")


def test_every_emotion_is_recognised():
    for e in EMOTIONS:
        assert run([f"[{e}] x"]) == (e, "x")


TEXT = "  [curious] Labs love water! [happy] They [1] swim. [odd] Really [sad]"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11])
def test_output_does_not_depend_on_chunking(size):
    chunks = [TEXT[i : i + size] for i in range(0, len(TEXT), size)]
    assert run(chunks) == run([TEXT])
