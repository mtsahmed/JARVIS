from jarvis.voice.sentences import SentenceSplitter


def feed_all(text: str, chunk: int = 3) -> tuple[list[str], str]:
    splitter, out = SentenceSplitter(), []
    for i in range(0, len(text), chunk):
        out += splitter.feed(text[i : i + chunk])
    return out, splitter.flush()


def test_sentence_released_only_after_following_whitespace() -> None:
    splitter = SentenceSplitter()
    assert splitter.feed("The sky is blue.") == []  # might still be "blue.com"
    assert splitter.feed(" Next") == ["The sky is blue."]
    assert splitter.flush() == "Next"


def test_splits_on_question_exclamation_and_newline() -> None:
    sentences, rest = feed_all("Is it raining today? Yes, bring an umbrella!\nStay dry out there")
    assert sentences == ["Is it raining today?", "Yes, bring an umbrella!"]
    assert rest == "Stay dry out there"


def test_numbers_domains_and_abbreviations_do_not_split() -> None:
    sentences, rest = feed_all("Pi is about 3.14 says Dr. Smith of example.com today. Done")
    assert sentences == ["Pi is about 3.14 says Dr. Smith of example.com today."]
    assert rest == "Done"


def test_tiny_fragment_joins_next_sentence() -> None:
    sentences, _ = feed_all("Sure. The meeting is at noon. ")
    assert sentences == ["Sure. The meeting is at noon."]


def test_closing_quote_stays_with_sentence() -> None:
    sentences, _ = feed_all('He said "see you tomorrow." Then left. ')
    assert sentences == ['He said "see you tomorrow."', "Then left."]


def test_flush_empties_buffer() -> None:
    splitter = SentenceSplitter()
    splitter.feed("unfinished")
    assert splitter.flush() == "unfinished"
    assert splitter.flush() == ""
