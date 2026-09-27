"""Build the Speaker: premium voice where config allows, local voice otherwise, with fallback."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from jarvis.core.config import VoiceConfig
from jarvis.core.logging_setup import log_usage
from jarvis.voice.tts.base import TTSAdapter, TTSError

logger = logging.getLogger(__name__)


class Speaker:
    """Speaks through `premium` when set and quota allows; falls back to `local` on any problem.

    Every call writes one usage line (provider, characters, fallback) — never the text itself.
    """

    def __init__(
        self,
        local: TTSAdapter,
        premium: TTSAdapter | None = None,
        min_chars_left: int = 0,
        purpose: str = "reply",
        usage_sink: Callable[[dict[str, Any]], None] = log_usage,
    ) -> None:
        self._local = local
        self._premium = premium
        self._min_chars_left = min_chars_left
        self._purpose = purpose
        self._usage_sink = usage_sink

    def speak(self, text: str) -> str:
        """Speak text; returns the provider name actually used. Raises TTSError only if the local voice fails."""
        text = text.strip()
        if not text:
            return "none"
        started = time.perf_counter()
        fallback = False
        adapter = self._local
        if self._premium is not None:
            if self._premium_has_quota(len(text)):
                try:
                    self._premium.speak(text)
                    adapter = self._premium
                except TTSError as e:
                    logger.warning("Premium voice failed, using local voice: %s", e)
                    fallback = True
            else:
                fallback = True
        try:
            if adapter is self._local:
                self._local.speak(text)
        finally:
            self._usage_sink(
                {
                    "kind": "tts",
                    "provider": adapter.name,
                    "purpose": self._purpose,
                    "characters": len(text),
                    "fallback": fallback,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            )
        return adapter.name

    def _premium_has_quota(self, needed: int) -> bool:
        check = getattr(self._premium, "characters_left", None)
        if check is None:
            return True
        try:
            left = check()
        except TTSError as e:
            # Quota unknown (e.g. key lacks permission): try anyway; a real quota error falls back.
            logger.warning("Could not check premium voice quota: %s", e)
            return True
        if left - needed < self._min_chars_left:
            logger.info("Premium voice quota low (%s chars left); using local voice", left)
            return False
        return True


def create_speaker(config: VoiceConfig, purpose: str = "reply", force_premium: bool = False) -> Speaker:
    """`purpose` is matched against JARVIS_ELEVENLABS_USE_FOR; `force_premium` is for manual tests."""
    if config.tts_provider != "sapi":
        raise TTSError(f"No adapter for TTS provider {config.tts_provider!r}.")
    from jarvis.voice.tts.sapi_adapter import SapiAdapter

    local = SapiAdapter(rate=config.tts_rate)
    if not (force_premium or purpose in config.elevenlabs_use_for):
        return Speaker(local, purpose=purpose)

    if not (config.elevenlabs_api_key and config.elevenlabs_voice_id and config.elevenlabs_model):
        raise TTSError("ElevenLabs needs ELEVENLABS_API_KEY, JARVIS_ELEVENLABS_VOICE_ID and JARVIS_ELEVENLABS_MODEL.")
    from jarvis.voice.tts.elevenlabs_adapter import ElevenLabsAdapter

    premium = ElevenLabsAdapter(config.elevenlabs_api_key, config.elevenlabs_voice_id, config.elevenlabs_model)
    return Speaker(local, premium, min_chars_left=config.elevenlabs_min_chars, purpose=purpose)
