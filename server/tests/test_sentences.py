from desk_buddy.llm import SentenceSplitter


def split_all(chunks: list[str]) -> list[str]:
    splitter = SentenceSplitter()
    out: list[str] = []
    for chunk in chunks:
        out += splitter.feed(chunk)
    return out + splitter.flush()


def test_simple_sentences():
    assert split_all(["Hello there. How are you?"]) == ["Hello there.", "How are you?"]


def test_sentence_is_released_only_once_confirmed_by_following_space():
    splitter = SentenceSplitter()
    assert splitter.feed("Hello there.") == []  # could still be "there.com"
    assert splitter.feed(" Next") == ["Hello there."]
    assert splitter.flush() == ["Next"]


def test_chunk_boundaries_mid_word():
    assert split_all(["Hello the", "re. How", " are you?"]) == ["Hello there.", "How are you?"]


def test_decimals_do_not_split():
    assert split_all(["It costs 3.5 dollars. Cheap!"]) == ["It costs 3.5 dollars.", "Cheap!"]


def test_abbreviations_do_not_split():
    text = "Dr. Smith said e.g. apples are fine. Yes."
    assert split_all([text]) == ["Dr. Smith said e.g. apples are fine.", "Yes."]


def test_mixed_punctuation_and_ellipsis():
    assert split_all(["Really?! Wow... Okay."]) == ["Really?!", "Wow...", "Okay."]


def test_closing_quote_stays_with_sentence():
    assert split_all(['He said "hi." Then left.']) == ['He said "hi."', "Then left."]


def test_flush_on_empty_returns_nothing():
    assert SentenceSplitter().flush() == []


def test_newline_is_a_boundary():
    assert split_all(["Sure\n\nHere you go."]) == ["Sure", "Here you go."]


def test_newline_after_text_without_letters_or_digits_does_not_split():
    assert split_all(["**\nHello there."]) == ["**\nHello there."]


def test_numbered_list_items_stay_whole():
    text = "1. Buy milk.\n2. Call mom."
    assert split_all([text]) == ["1. Buy milk.", "2. Call mom."]


def test_initials_do_not_split():
    assert split_all(["J. Smith called. Bye."]) == ["J. Smith called.", "Bye."]


def test_punctuation_only_chunk_joins_next_sentence():
    assert split_all(["... Hello there. Bye."]) == ["... Hello there.", "Bye."]


def test_flush_drops_text_without_letters_or_digits():
    assert split_all(["Done. **"]) == ["Done."]


def test_curly_quotes_close_a_sentence():
    assert split_all(["“Hi.” Then left."]) == ["“Hi.”", "Then left."]


def test_markdown_emphasis_closes_a_sentence():
    assert split_all(["**Done.** Next one."]) == ["**Done.**", "Next one."]


def test_unicode_ellipsis_ends_a_sentence():
    assert split_all(["Hmm… Okay."]) == ["Hmm…", "Okay."]


def test_long_text_is_cut_at_last_whitespace_before_cap():
    splitter = SentenceSplitter(max_chars=20)
    out = splitter.feed("alpha beta gamma delta epsilon zeta") + splitter.flush()
    assert out == ["alpha beta gamma", "delta epsilon zeta"]


def test_long_text_keeps_clause_punctuation_with_first_part():
    splitter = SentenceSplitter(max_chars=20)
    out = splitter.feed("aaaa bbbb cccc ddd, eeee ffff") + splitter.flush()
    assert out == ["aaaa bbbb cccc ddd,", "eeee ffff"]


def test_long_text_without_breaks_is_hard_cut():
    splitter = SentenceSplitter(max_chars=20)
    out = splitter.feed("a" * 50) + splitter.flush()
    assert out == ["a" * 20, "a" * 20, "a" * 10]


def test_default_cap_is_200_chars():
    words = " ".join(["word"] * 100)  # 499 chars, no sentence end
    out = split_all([words])
    assert len(out) > 1
    assert all(len(s) <= 200 for s in out)
    assert " ".join(out) == words


_INVARIANCE_TEXTS = [
    "Hello there. How are you?",
    "It costs 3.5 dollars. Cheap! Dr. Smith said e.g. apples are fine. Yes.",
    "Really?! Wow... Okay.… Hmm… fine.",
    'He said "hi." Then left. “Hi.” Then **Done.** Next _ok._ end',
    "Sure\n\nHere you go:\n1. Buy milk.\n2. Call J. Smith.\n- **bold**\n",
    "... leading dots. And example.com is a site. Mr. Jones",
    " ".join(["word"] * 120) + ". Then a short one.",
    "a" * 450 + " tail, more text; and: clauses here.",
]


def _feed_in_chunks(text: str, size: int) -> list[str]:
    return split_all([text[i : i + size] for i in range(0, len(text), size)])


def test_output_does_not_depend_on_chunking():
    for text in _INVARIANCE_TEXTS:
        whole = split_all([text])
        assert _feed_in_chunks(text, 1) == whole, text
        assert _feed_in_chunks(text, 3) == whole, text
