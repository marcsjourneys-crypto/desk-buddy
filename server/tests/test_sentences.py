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
