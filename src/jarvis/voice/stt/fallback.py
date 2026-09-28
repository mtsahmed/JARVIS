"""Primary speech-to-text (e.g. cloud) with an automatic local fallback, so voice keeps working offline."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import numpy as np

from jarvis.voice.stt.base import STTAdapter, STTError, Transcript

logger = logging.getLogger(__name__)

# After the primary fails, use the fallback directly for this long instead of waiting for the primary
# to time out again on every turn (e.g. while offline). Then the primary is tried again.
COOLDOWN_SECONDS = 180.0


class FallbackSTT:
    def __init__(self, primary: STTAdapter, make_fallback: Callable[[], STTAdapter],
                 cooldown_seconds: float = COOLDOWN_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._primary = primary
        self._make_fallback = make_fallback  # called on the first failure (loads the local model then)
        self._fallback: STTAdapter | None = None
        self._last_model = primary.model
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._skip_primary_until = 0.0

    @property
    def model(self) -> str:
        """The model that produced the most recent transcript (for usage logs)."""
        return self._last_model

    def transcribe(self, samples: np.ndarray) -> Transcript:
        if self._clock() >= self._skip_primary_until:
            try:
                transcript = self._primary.transcribe(samples)
                self._last_model = self._primary.model
                return transcript
            except STTError as e:
                self._skip_primary_until = self._clock() + self._cooldown
                logger.warning("Primary speech recognition failed; using the local fallback for the next %.0f s: %s",
                               self._cooldown, e)
        if self._fallback is None:
            self._fallback = self._make_fallback()  # raises STTError if the local model can't load
        transcript = self._fallback.transcribe(samples)
        self._last_model = self._fallback.model
        return transcript
