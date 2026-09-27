"""Provider-neutral speech-to-text interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


# Primes speech recognition with the words Jarvis commands use (e.g. so "cancel" is not heard as
# "cancer"). General on purpose: it must not copy the benchmark sentences, or tests would overrate it.
VOCABULARY_HINT = (
    "Jarvis is a personal voice assistant. Typical requests: set a reminder, remind me in five minutes, "
    "list my reminders, cancel a reminder, search my files, open a document, search the web, read my email."
)


@dataclass(frozen=True)
class Transcript:
    text: str  # empty when no speech was recognized
    audio_seconds: float


class STTError(Exception):
    """Speech recognition failed. The message is safe to show the user."""


class STTAdapter(Protocol):
    @property
    def model(self) -> str: ...

    def transcribe(self, samples: np.ndarray) -> Transcript:
        """Transcribe mono float32 samples at 16 kHz. Raises STTError on failure."""
        ...
