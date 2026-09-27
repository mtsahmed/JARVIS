"""Provider-neutral speech-to-text interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


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
