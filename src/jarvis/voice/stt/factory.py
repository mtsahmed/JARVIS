"""Build the speech-to-text adapter selected in config."""

from __future__ import annotations

from jarvis.core.config import VoiceConfig
from jarvis.voice.stt.base import STTAdapter, STTError


def create_stt(config: VoiceConfig, model: str | None = None) -> STTAdapter:
    """`model` overrides config.stt_model (used by the benchmark)."""
    if config.stt_provider == "faster-whisper":
        from jarvis.voice.stt.faster_whisper_adapter import FasterWhisperAdapter

        return FasterWhisperAdapter(
            model=model or config.stt_model,
            compute_type=config.stt_compute_type,
            beam_size=config.stt_beam_size,
            models_dir=config.models_dir,
        )
    raise STTError(f"No adapter for STT provider {config.stt_provider!r}.")
