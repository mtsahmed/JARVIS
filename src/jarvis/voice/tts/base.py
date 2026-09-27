"""Provider-neutral text-to-speech interface."""

from __future__ import annotations

from typing import Protocol


class TTSError(Exception):
    """Speaking failed. The message is safe to show the user (no secrets)."""


class TTSAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def speak(self, text: str) -> None:
        """Speak text and block until playback ends. Raises TTSError on failure."""
        ...
