"""Voice round trip: hold key -> record -> STT -> LLM -> speak. print() is the CLI UI."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
import psutil

from jarvis.core.chat import ChatSession
from jarvis.core.logging_setup import log_usage
from jarvis.llm.base import LLMError
from jarvis.voice.audio import SAMPLE_RATE, AudioError
from jarvis.voice.sentences import SentenceSplitter, speakable
from jarvis.voice.stt.base import STTAdapter, STTError
from jarvis.voice.tts.base import TTSError

logger = logging.getLogger(__name__)

MIN_SPEECH_SECONDS = 0.3

# Voice mode only: replies are heard, not read, so they must be short and plain.
VOICE_INSTRUCTIONS = (
    "You are Jarvis, a personal voice assistant. Your reply will be spoken aloud. "
    "Answer in one to three short sentences of plain spoken English. "
    "Never use lists, bullet points, headings, markdown, emojis, URLs, or special symbols. "
    "If the request is unclear, ask one short question. "
    "If a full answer would be long, give the key point and offer to say more."
)
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

        # The LLM streams on a worker thread; this (main) thread speaks each sentence as soon as it
        # is complete, so Jarvis starts talking while the rest of the reply is still being written.
        sentences: queue.Queue[str | None] = queue.Queue()
        outcome: dict[str, Any] = {}
        worker = threading.Thread(target=self._stream_reply, args=(transcript.text, sentences, outcome), daemon=True)
        worker.start()

        until_speak_ms: int | None = None
        spoken = 0
        while (raw := self._next_sentence(sentences)) is not None:
            if not (sentence := speakable(raw)):
                continue  # nothing left to say once markdown is removed
            if until_speak_ms is None:
                until_speak_ms = _ms_since(ended_at)
                print("jarvis>", end="", flush=True)
            print(f" {sentence}", end="", flush=True)
            self._say(sentence)
            spoken += 1
        if spoken:
            print()
        worker.join()

        if "error" in outcome:
            error = outcome["error"]
            if not isinstance(error, LLMError):
                raise error  # unexpected bug: handled and logged by run()
            print(f"[error] {error}")
            if not spoken:
                self._say("Sorry, I couldn't reach my language service.")
            return
        if not spoken:
            until_speak_ms = _ms_since(ended_at)
            print("jarvis> (no answer)")
            self._say("Sorry, I don't have an answer for that.")

        self._usage_sink({
            "kind": "voice_turn",
            "stt_ms": stt_ms,
            "llm_ms": outcome["llm_ms"],  # full reply, start to finish
            "until_speak_ms": until_speak_ms,  # end of speech -> first sentence handed to TTS
            "sentences": spoken,
            "rss_mb": round(psutil.Process().memory_info().rss / 1_048_576),
        })

    def _stream_reply(self, text: str, sentences: queue.Queue[str | None], outcome: dict[str, Any]) -> None:
        """Worker thread: stream the LLM reply and queue complete sentences. Always ends with None."""
        splitter = SentenceSplitter()
        started = time.perf_counter()
        try:
            self._session.ask(text, on_text=lambda piece: [sentences.put(s) for s in splitter.feed(piece)])
            if rest := splitter.flush():
                sentences.put(rest)
        except Exception as e:  # handed to the main thread
            outcome["error"] = e
        finally:
            outcome["llm_ms"] = _ms_since(started)
            sentences.put(None)

    @staticmethod
    def _next_sentence(sentences: queue.Queue[str | None]) -> str | None:
        while True:
            try:
                return sentences.get(timeout=_POLL_SECONDS)  # timeout keeps Ctrl+C responsive on Windows
            except queue.Empty:
                continue

    def _say(self, text: str) -> None:
        try:
            self._speaker.speak(text)
        except TTSError as e:
            print(f"[voice error] {e}")


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
