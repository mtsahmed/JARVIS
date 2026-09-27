"""Build the speech-to-text adapter selected in config."""

from __future__ import annotations

from jarvis.core.config import VoiceConfig
from jarvis.voice.stt.base import VOCABULARY_HINT, STTAdapter, STTError


def create_stt(config: VoiceConfig, model: str | None = None, provider: str | None = None,
               api_key: str | None = None) -> STTAdapter:
    """`model` / `provider` override the config (used by the benchmark). `api_key` is for cloud providers."""
    provider = provider or config.stt_provider
    if provider == "openai":
        from jarvis.voice.stt.openai_transcribe_adapter import OpenAITranscribeAdapter

        if not api_key or not model:
            raise STTError("OpenAI transcription needs an API key and a model name.")
        return OpenAITranscribeAdapter(api_key, model, prompt=VOCABULARY_HINT)
    if provider == "faster-whisper":
        from jarvis.voice.stt.faster_whisper_adapter import FasterWhisperAdapter

        return FasterWhisperAdapter(
            model=model or config.stt_model,
            compute_type=config.stt_compute_type,
            beam_size=config.stt_beam_size,
            models_dir=config.models_dir,
            prompt=VOCABULARY_HINT,
        )
    raise STTError(f"No adapter for STT provider {provider!r}.")
