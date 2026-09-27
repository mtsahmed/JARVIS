"""Local CPU speech-to-text with faster-whisper. The only module that imports `faster_whisper`."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

from jarvis.voice.stt.base import STTError, Transcript

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class FasterWhisperAdapter:
    def __init__(self, model: str, compute_type: str, beam_size: int, models_dir: Path) -> None:
        self._model_name = model
        self._beam_size = beam_size
        models_dir.mkdir(parents=True, exist_ok=True)
        self._model = self._load(model, compute_type, models_dir)

    @property
    def model(self) -> str:
        return self._model_name

    @staticmethod
    def _load(model: str, compute_type: str, models_dir: Path) -> WhisperModel:
        options = {"device": "cpu", "compute_type": compute_type, "download_root": str(models_dir)}
        try:
            # Cached copy first: works offline and skips a network check on every start.
            return WhisperModel(model, local_files_only=True, **options)
        except Exception:
            logger.info("Speech model %s not cached; downloading to %s", model, models_dir)
        try:
            return WhisperModel(model, **options)
        except Exception as e:
            logger.error("Could not load speech model %s: %s", model, type(e).__name__)
            raise STTError(
                f"Could not load speech model {model!r}. The first run needs internet to download it."
            ) from None

    def transcribe(self, samples: np.ndarray) -> Transcript:
        audio_seconds = samples.size / SAMPLE_RATE
        if samples.size == 0:
            return Transcript("", 0.0)
        try:
            segments, _info = self._model.transcribe(
                samples, language="en", beam_size=self._beam_size, vad_filter=True
            )
            # segments is a generator; transcription happens while iterating.
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as e:
            logger.error("Transcription failed: %s", type(e).__name__)
            raise STTError("Speech recognition failed. Please try again.") from None
        return Transcript(text, audio_seconds)
