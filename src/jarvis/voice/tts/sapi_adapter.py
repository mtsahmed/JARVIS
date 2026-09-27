"""Free local Windows voice (SAPI5) via pyttsx3. The only module that imports `pyttsx3`."""

from __future__ import annotations

import logging

import pyttsx3

from jarvis.voice.tts.base import TTSError

logger = logging.getLogger(__name__)


class SapiAdapter:
    def __init__(self, rate: int | None = None) -> None:
        self._rate = rate

    @property
    def name(self) -> str:
        return "sapi"

    def speak(self, text: str) -> None:
        try:
            # Fresh engine per utterance: repeated runAndWait() on one engine can hang on some setups.
            engine = pyttsx3.init()
            if self._rate is not None:
                engine.setProperty("rate", self._rate)
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            logger.error("Local voice failed: %s", type(e).__name__)
            raise TTSError("The local Windows voice failed.") from None
