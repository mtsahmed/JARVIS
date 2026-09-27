"""Split streamed LLM text into speakable sentences as soon as each one is complete."""

from __future__ import annotations

import re

# End of sentence: . ! ? (optionally followed by closing quotes/brackets) and then whitespace; or a line break.
# Requiring whitespace after the punctuation means "3.5" or "example.com" never split, and a
# sentence is only released once the next piece of text shows it really ended.
_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*\s+|\n+")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "approx"}


class SentenceSplitter:
    def __init__(self, min_chars: int = 8) -> None:
        self._min_chars = min_chars  # avoids speaking tiny fragments on their own
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any sentences that are now complete."""
        self._buffer += text
        sentences: list[str] = []
        start = 0
        for match in _BOUNDARY.finditer(self._buffer):
            candidate = self._buffer[start : match.end()].strip()
            if len(candidate) < self._min_chars or self._ends_with_abbreviation(candidate):
                continue
            sentences.append(candidate)
            start = match.end()
        self._buffer = self._buffer[start:]
        return sentences

    def flush(self) -> str:
        """Return whatever text is left at the end of the reply."""
        rest, self._buffer = self._buffer.strip(), ""
        return rest

    @staticmethod
    def _ends_with_abbreviation(sentence: str) -> bool:
        last_word = sentence.rstrip(".!?\"')] \n").rsplit(maxsplit=1)[-1].lower() if sentence.strip() else ""
        return sentence.rstrip().endswith(".") and last_word in _ABBREVIATIONS


_LIST_MARKER = re.compile(r"^\s*(?:[-*•]+|#+|\d+[.)])\s+")
_MARKUP = re.compile(r"[*_`#>|]+")
_DASHES = re.compile(r"\s*[—–]\s*")


def speakable(sentence: str) -> str:
    """Strip markdown and symbols a voice would read out literally (bullets, **bold**, headings, dashes)."""
    # Per line: a short fragment can be joined with the next line, which may start with its own bullet.
    text = "\n".join(_LIST_MARKER.sub("", line) for line in sentence.splitlines())
    text = _MARKUP.sub("", text)
    text = _DASHES.sub(", ", text)
    return " ".join(text.split())
