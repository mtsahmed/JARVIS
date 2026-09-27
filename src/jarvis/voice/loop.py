"""Voice round trip: hold key -> record -> STT -> LLM -> speak. print() is the CLI UI."""

from __future__ import annotations

import logging
import queue
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
import psutil

from jarvis.core.chat import ChatSession
from jarvis.core.logging_setup import log_usage
from jarvis.llm.base import LLMError
from jarvis.voice.audio import SAMPLE_RATE, AudioError
from jarvis.voice.stt.base import STTAdapter, STTError
from jarvis.voice.tts.base import TTSError

logger = logging.getLogger(__name__)

MIN_SPEECH_SECONDS = 0.3
_POLL_SECONDS = 0.2  # queue waits time out regularly so Ctrl+C works on Windows


class RecorderLike(Protocol):
    def start(self) -> None: ...
    def stop(self) -> np.ndarray: ...


class SpeakerLike(Protocol):
    def speak(self, text: str) -> str: ...


class VoiceLoop:
    def __init__(
        self,
        session: ChatSession,
        stt: STTAdapter,
        speaker: SpeakerLike,
        recorder: RecorderLike,
        events: queue.Queue[str],
        drain: Callable[[], None],
        max_record_seconds: float,
        usage_sink: Callable[[dict[str, Any]], None] = log_usage,
    ) -> None:
        self._session = session
        self._stt = stt
        self._speaker = speaker
        self._recorder = recorder
        self._events = events
        self._drain = drain
        self._max_record_seconds = max_record_seconds
        self._usage_sink = usage_sink

    def run(self) -> None:
        """Loop until Ctrl+C."""
        while True:
            if self._next_event() != "start":
                continue
            samples = self._record()
            if samples is not None:
                try:
                    self.handle_turn(samples, ended_at=time.perf_counter())
                except Exception:
                    logger.exception("Unexpected error during voice turn")
                    print("[error] Something went wrong. Details are in logs/jarvis.log.")
            self._drain()  # ignore key presses made while Jarvis was busy
            print("\n(hold the key to talk)")

    def _next_event(self, timeout: float | None = None) -> str | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is None or time.monotonic() < deadline:
            try:
                return self._events.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue
        return None

    def _record(self) -> np.ndarray | None:
        try:
            self._recorder.start()
        except AudioError as e:
            print(f"[error] {e}")
            return None
        print("listening...", end="", flush=True)
        self._next_event(timeout=self._max_record_seconds)  # "stop", or give up at the cap
        samples = self._recorder.stop()
        print(" done")
        return samples

    def handle_turn(self, samples: np.ndarray, ended_at: float) -> None:
        """Transcribe, ask the LLM, speak the reply. Never raises for provider failures."""
        audio_seconds = samples.size / SAMPLE_RATE
        if audio_seconds < MIN_SPEECH_SECONDS:
            print("(too short - hold the key while you speak)")
            return

        started = time.perf_counter()
        try:
            transcript = self._stt.transcribe(samples)
        except STTError as e:
            print(f"[error] {e}")
            return
        stt_ms = _ms_since(started)
        self._usage_sink({"kind": "stt", "model": self._stt.model, "audio_seconds": round(audio_seconds, 2),
                          "latency_ms": stt_ms})

        if not transcript.text:
            print("(didn't catch that)")
            self._say("Sorry, I didn't catch that.")
            return
        print(f"you said> {transcript.text}")

        started = time.perf_counter()
        try:
            reply = self._session.ask(transcript.text)
        except LLMError as e:
            print(f"[error] {e}")
            self._say("Sorry, I couldn't reach my language service.")
            return
        llm_ms = _ms_since(started)

        text = reply.text or "Sorry, I don't have an answer for that."
        print(f"jarvis> {text}")
        until_speak_ms = _ms_since(ended_at)
        self._say(text)
        self._usage_sink({
            "kind": "voice_turn",
            "stt_ms": stt_ms,
            "llm_ms": llm_ms,
            "until_speak_ms": until_speak_ms,  # end of speech -> reply handed to TTS
            "rss_mb": round(psutil.Process().memory_info().rss / 1_048_576),
        })

    def _say(self, text: str) -> None:
        try:
            self._speaker.speak(text)
        except TTSError as e:
            print(f"[voice error] {e}")


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
