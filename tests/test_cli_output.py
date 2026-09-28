"""Threat T16: control characters in outside text must never reach the terminal (or the voice)."""

from jarvis.core.text import for_terminal
from jarvis.tools.search.base import SearchResult
from jarvis.tools.web_search import format_sources
from jarvis.voice.sentences import speakable

ESC_TRICK = "Safe title\x1b[2J\x1b[1A\x07hidden\x9b31m"


def test_control_characters_removed_but_text_kept() -> None:
    assert for_terminal(ESC_TRICK) == "Safe title[2J[1Ahidden31m"
    assert for_terminal("line one\nline two\ttabbed") == "line one\nline two\ttabbed"  # newlines/tabs kept
    assert for_terminal("naïve café — 5 °C") == "naïve café — 5 °C"  # normal Unicode kept


def test_sources_and_spoken_text_are_cleaned() -> None:
    sources = format_sources([SearchResult(ESC_TRICK, "https://example.com/\x1b]8;;evil\x07", "snippet")])
    assert "\x1b" not in sources and "\x07" not in sources
    assert "\x1b" not in speakable(f"Hello {ESC_TRICK}.")
