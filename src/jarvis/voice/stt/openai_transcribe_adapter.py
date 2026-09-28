"""Cloud speech-to-text with OpenAI's transcription API.

Privacy trade-off: the recorded audio is sent to OpenAI. Used only when configured; local
faster-whisper stays the default and the offline fallback.
"""

from __future__ import annotations

import io
import logging
import wave

import numpy as np
import openai
from openai import OpenAI

from jarvis.voice.stt.base import STTError, Transcript

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class OpenAITranscribeAdapter:
    # 8 s: the slowest of the owner's 10 benchmark clips took 2.8 s; a hung network should not stall a voice turn.
    def __init__(self, api_key: str, model: str, timeout_seconds: float = 8.0, prompt: str | None = None) -> None:
        self._model = model
        self._prompt = prompt
        self._timeout = timeout_seconds
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=1)

    @property
    def model(self) -> str:
        return self._model

    def transcribe(self, samples: np.ndarray) -> Transcript:
        audio_seconds = samples.size / SAMPLE_RATE
        if samples.size == 0:
            return Transcript("", 0.0)
        request = {"model": self._model, "file": ("speech.wav", _to_wav(samples), "audio/wav"), "language": "en"}
        if self._prompt:
            request["prompt"] = self._prompt
        try:
            result = self._client.audio.transcriptions.create(**request)
        # Provider error text is not passed on (it can echo parts of the key).
        except openai.APITimeoutError:
            logger.warning("OpenAI transcription timed out after %ss", self._timeout)
            raise STTError("Cloud speech recognition timed out.") from None
        except openai.APIConnectionError:
            logger.warning("OpenAI transcription connection error")
            raise STTError("Could not reach the cloud speech service.") from None
        except openai.APIStatusError as e:
            logger.error("OpenAI transcription HTTP %s", e.status_code)
            raise STTError(f"Cloud speech recognition failed (HTTP {e.status_code}).") from None
        except openai.OpenAIError as e:
            logger.error("OpenAI transcription error: %s", type(e).__name__)
            raise STTError("Cloud speech recognition failed.") from None
        return Transcript(result.text.strip(), audio_seconds)


def _to_wav(samples: np.ndarray) -> bytes:
    """Mono float32 at 16 kHz -> in-memory 16-bit WAV."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())
    return buffer.getvalue()
