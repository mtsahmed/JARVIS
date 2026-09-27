"""Primary speech-to-text (e.g. cloud) with an automatic local fallback, so voice keeps working offline."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

from jarvis.voice.stt.base import STTAdapter, STTError, Transcript

logger = logging.getLogger(__name__)


class FallbackSTT:
    def __init__(self, primary: STTAdapter, make_fallback: Callable[[], STTAdapter]) -> None:
        self._primary = primary
        self._make_fallback = make_fallback  # called on the first failure (loads the local model then)
        self._fallback: STTAdapter | None = None
        self._last_model = primary.model

    @property
    def model(self) -> str:
        """The model that produced the most recent transcript (for usage logs)."""
        return self._last_model

    def transcribe(self, samples: np.ndarray) -> Transcript:
        try:
            transcript = self._primary.transcribe(samples)
            self._last_model = self._primary.model
            return transcript
        except STTError as e:
            logger.warning("Primary speech recognition failed, using local fallback: %s", e)
        if self._fallback is None:
            self._fallback = self._make_fallback()  # raises STTError if the local model can't load
        transcript = self._fallback.transcribe(samples)
        self._last_model = self._fallback.model
        return transcript
