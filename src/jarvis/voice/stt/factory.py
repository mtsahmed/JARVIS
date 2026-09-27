"""Build the speech-to-text adapter selected in config."""

from __future__ import annotations

from jarvis.core.config import VoiceConfig
from jarvis.voice.stt.base import VOCABULARY_HINT, STTAdapter, STTError


def create_stt(config: VoiceConfig, model: str | None = None, provider: str | None = None,
               api_key: str | None = None) -> STTAdapter:
    """`model` / `provider` override the config (used by the benchmark). `api_key` is for cloud providers.

    A cloud provider gets the local fallback from config (JARVIS_STT_FALLBACK_MODEL), unless this is a
    benchmark override (`model` given), which must measure one model only.
    """
    provider = provider or config.stt_provider
    if provider == "openai":
        from jarvis.voice.stt.openai_transcribe_adapter import OpenAITranscribeAdapter

        if not api_key:
            raise STTError("OpenAI transcription needs the OpenAI API key.")
        primary = OpenAITranscribeAdapter(api_key, model or config.stt_model, prompt=VOCABULARY_HINT)
        fallback_model = config.stt_fallback_model
        if model is not None or not fallback_model:
            return primary

        from jarvis.voice.stt.fallback import FallbackSTT

        # Import the local engine's native libraries now, before anything loads WinRT (reminder
        # notifications): loading them afterwards crashes the process (see integrations/notify.py).
        # The model itself (~250 MB) loads only if the cloud ever fails.
        import jarvis.voice.stt.faster_whisper_adapter  # noqa: F401

        return FallbackSTT(primary, lambda: _local(config, fallback_model))
    if provider == "faster-whisper":
        return _local(config, model or config.stt_model)
    raise STTError(f"No adapter for STT provider {provider!r}.")


def _local(config: VoiceConfig, model: str) -> STTAdapter:
    from jarvis.voice.stt.faster_whisper_adapter import FasterWhisperAdapter

    return FasterWhisperAdapter(
        model=model,
        compute_type=config.stt_compute_type,
        beam_size=config.stt_beam_size,
        models_dir=config.models_dir,
        prompt=VOCABULARY_HINT,
    )
