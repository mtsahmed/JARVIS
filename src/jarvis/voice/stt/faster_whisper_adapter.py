"""Local CPU speech-to-text with faster-whisper. The only module that imports `faster_whisper`."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.transcribe import Segment

from jarvis.voice.stt.base import STTError, Transcript

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class FasterWhisperAdapter:
    def __init__(self, model: str, compute_type: str, beam_size: int, models_dir: Path,
                 prompt: str | None = None) -> None:
        self._model_name = model
        self._beam_size = beam_size
        self._prompt = prompt  # vocabulary hint for the first (for short clips: only) window
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
                samples,
                language="en",
                beam_size=self._beam_size,
                vad_filter=True,
                # Library docs: False makes the model "less prone to getting stuck in a failure loop,
                # such as repetition looping". Each push-to-talk clip is independent anyway.
                condition_on_previous_text=False,
                initial_prompt=self._prompt,
            )
            # segments is a generator; transcription happens while iterating.
            text = " ".join(s.text.strip() for s in segments if not _looks_hallucinated(s)).strip()
        except Exception as e:
            logger.error("Transcription failed: %s", type(e).__name__)
            raise STTError("Speech recognition failed. Please try again.") from None
        return Transcript(text, audio_seconds)


# The library's own defaults for "failed" and "silent" segments (transcribe() docstring).
_MAX_COMPRESSION_RATIO = 2.4
_NO_SPEECH_PROB = 0.6
_MIN_AVG_LOGPROB = -1.0


def _looks_hallucinated(segment: Segment) -> bool:
    """Drop segments that are repetitive, or likely silence the model 'heard' words in."""
    if segment.compression_ratio > _MAX_COMPRESSION_RATIO:
        logger.info("Dropped repetitive speech segment (compression ratio %.2f)", segment.compression_ratio)
        return True
    if segment.no_speech_prob > _NO_SPEECH_PROB and segment.avg_logprob < _MIN_AVG_LOGPROB:
        logger.info("Dropped likely-silent speech segment")
        return True
    return False
