"""STT adapters with fake back ends (no model, no network, no cost)."""

import io
import wave
from types import SimpleNamespace
from typing import Any

import httpx2  # the HTTP library the installed openai SDK uses
import numpy as np
import openai
import pytest

from jarvis.voice.stt.base import VOCABULARY_HINT, STTError
from jarvis.voice.stt.openai_transcribe_adapter import OpenAITranscribeAdapter

ONE_SECOND = np.zeros(16_000, dtype=np.float32)


class FakeTranscriptions:
    def __init__(self, text: str = " Cancel all my reminders. ", error: Exception | None = None) -> None:
        self.text, self.error, self.requests = text, error, []

    def create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


def make(fake: FakeTranscriptions) -> OpenAITranscribeAdapter:
    adapter = OpenAITranscribeAdapter("sk-test", "gpt-4o-mini-transcribe", prompt=VOCABULARY_HINT)
    adapter._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake))  # type: ignore[assignment]
    return adapter


def test_sends_wav_with_language_and_prompt() -> None:
    fake = FakeTranscriptions()
    transcript = make(fake).transcribe(ONE_SECOND)
    assert transcript.text == "Cancel all my reminders." and transcript.audio_seconds == 1.0
    request = fake.requests[0]
    assert request["model"] == "gpt-4o-mini-transcribe" and request["language"] == "en"
    assert request["prompt"] == VOCABULARY_HINT
    name, data, content_type = request["file"]
    assert name == "speech.wav" and content_type == "audio/wav"
    with wave.open(io.BytesIO(data)) as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate(), f.getnframes()) == (1, 2, 16_000, 16_000)


def test_empty_audio_skips_the_call() -> None:
    fake = FakeTranscriptions()
    assert make(fake).transcribe(np.zeros(0, dtype=np.float32)).text == ""
    assert fake.requests == []


def test_network_error_becomes_clear_stt_error() -> None:
    drop = openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/v1/audio/transcriptions"))
    with pytest.raises(STTError, match="Could not reach"):
        make(FakeTranscriptions(error=drop)).transcribe(ONE_SECOND)


def test_vocabulary_hint_is_general() -> None:
    # Must not contain the benchmark sentences, or benchmark results would be inflated.
    import sys

    sys.path.insert(0, "scripts")
    from benchmark_stt import PROMPTS

    for sentence in PROMPTS:
        assert sentence.lower().rstrip(".?") not in VOCABULARY_HINT.lower()


def test_local_adapter_passes_vocabulary_hint() -> None:
    from jarvis.voice.stt.faster_whisper_adapter import FasterWhisperAdapter

    seen: dict[str, Any] = {}

    class FakeModel:
        def transcribe(self, samples: np.ndarray, **options: Any) -> tuple[list[Any], None]:
            seen.update(options)
            return [SimpleNamespace(text=" hello ", compression_ratio=1.0, no_speech_prob=0.0, avg_logprob=-0.2)], None

    adapter = FasterWhisperAdapter.__new__(FasterWhisperAdapter)  # skip loading a real model
    adapter._model, adapter._model_name, adapter._beam_size, adapter._prompt = FakeModel(), "base.en", 5, VOCABULARY_HINT
    assert adapter.transcribe(ONE_SECOND).text == "hello"
    assert seen["initial_prompt"] == VOCABULARY_HINT and seen["condition_on_previous_text"] is False
