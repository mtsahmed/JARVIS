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


# --- cloud with local fallback -------------------------------------------------------------------

from jarvis.voice.stt.base import Transcript  # noqa: E402
from jarvis.voice.stt.fallback import FallbackSTT  # noqa: E402


class FakeSTT:
    def __init__(self, name: str, text: str = "hello", fail: bool = False) -> None:
        self._name, self._text, self._fail, self.calls = name, text, fail, 0

    @property
    def model(self) -> str:
        return self._name

    def transcribe(self, samples: np.ndarray) -> Transcript:
        self.calls += 1
        if self._fail:
            raise STTError(f"{self._name} down")
        return Transcript(self._text, samples.size / 16_000)


def test_fallback_not_loaded_while_cloud_works() -> None:
    made: list[str] = []
    stt = FallbackSTT(FakeSTT("cloud", "from cloud"), lambda: made.append("local") or FakeSTT("local"))
    assert stt.transcribe(ONE_SECOND).text == "from cloud"
    assert made == [] and stt.model == "cloud"


def test_cloud_failure_uses_local_and_loads_it_once() -> None:
    made: list[str] = []
    local = FakeSTT("base.en", "from local")
    stt = FallbackSTT(FakeSTT("cloud", fail=True), lambda: made.append("x") or local)
    assert stt.transcribe(ONE_SECOND).text == "from local" and stt.model == "base.en"
    assert stt.transcribe(ONE_SECOND).text == "from local"
    assert made == ["x"] and local.calls == 2


def test_both_failing_raises_stt_error() -> None:
    stt = FallbackSTT(FakeSTT("cloud", fail=True), lambda: FakeSTT("local", fail=True))
    with pytest.raises(STTError):
        stt.transcribe(ONE_SECOND)


def test_factory_builds_cloud_with_lazy_local_fallback(tmp_path: Any) -> None:
    from jarvis.core.config import VoiceConfig
    from jarvis.voice.stt.factory import create_stt

    config = VoiceConfig(
        data_dir=tmp_path, models_dir=tmp_path / "models", stt_provider="openai", stt_model="gpt-4o-mini-transcribe",
        stt_compute_type="int8", stt_beam_size=5, stt_fallback_model="base.en", tts_provider="sapi", tts_rate=None,
        ptt_key="ctrl_r", max_record_seconds=30, elevenlabs_use_for=frozenset(), elevenlabs_voice_id=None,
        elevenlabs_model=None, elevenlabs_min_chars=0,
    )
    stt = create_stt(config, api_key="sk-test")
    assert isinstance(stt, FallbackSTT) and stt.model == "gpt-4o-mini-transcribe"
    assert not (tmp_path / "models").exists()  # local model not loaded (or downloaded) up front
    # a benchmark override measures the cloud model alone
    assert isinstance(create_stt(config, model="gpt-4o-mini-transcribe", provider="openai", api_key="sk-test"),
                      OpenAITranscribeAdapter)
