"""Voice round trip: hold key -> record -> STT -> LLM -> speak. print() is the CLI UI."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import psutil

from jarvis.core.text import for_terminal
from jarvis.core.chat import ChatSession
from jarvis.core.events import EventBus
from jarvis.core.logging_setup import log_usage
from jarvis.llm.base import LLMError
from jarvis.voice.audio import SAMPLE_RATE, AudioError
from jarvis.voice.sentences import SentenceSplitter, speakable
from jarvis.tools.web_search import Sources, format_sources
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
    "If a full answer would be long, give the key point and offer to say more. "
    "For prices, market values, news, weather, or other facts that change over time, use the web_search tool "
    "and answer from its results in your own words; never read out web addresses, since sources are shown on "
    "screen. If web search is not available or fails, say you cannot look it up live right now, then give the "
    "most recent information you know and roughly when it is from. "
    "When an action needs the user's approval, just call the tool: Jarvis asks the user itself, "
    "so never ask for confirmation in your reply. "
    "Only offer to do things you have tools for."
)

# Saying one of these (and nothing else) starts a new conversation, like /reset in text mode.
RESET_PHRASES = frozenset({"reset", "new conversation", "start over", "start a new conversation", "clear history"})

# Speech recognition can turn background noise into strings of filler words. A short "yes" or
# "okay" can be a real answer to Jarvis's question, so only longer all-filler transcripts are dropped.
_FILLER_WORDS = frozenset({"okay", "ok", "all", "right", "alright", "yeah", "yes", "yep", "um", "uh", "hmm",
                           "mm", "oh", "ah", "so", "well", "like", "you", "know"})
_MIN_FILLER_RUN = 4
_POLL_SECONDS = 0.2  # queue waits time out regularly so Ctrl+C works on Windows

# Spoken approval for CONFIRM-tier actions. Anything else, including silence, means no.
CONFIRM_WAIT_SECONDS = 10.0
_YES_WORDS = frozenset({"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "correct", "affirmative"})
_YES_PHRASES = ("go ahead", "do it")
_NO_WORDS = frozenset({"no", "not", "don't", "dont", "cancel", "stop", "wait", "never", "nope"})


SEARCH_ACK = "Let me check."
BRIEFING_ACK = "Let me check your inbox."
# Slow tools: say something at once so the user isn't left in silence.
_ACK_PHRASES = {"web_search": SEARCH_ACK, "morning_briefing": BRIEFING_ACK}


@dataclass(frozen=True)
class Ack:
    """A short filler phrase (e.g. while searching). Spoken, but not counted as the answer in metrics."""

    text: str
    # The answer that follows is a briefing: speak it in one piece with the briefing (premium) voice,
    # since sentence-by-sentence premium requests (~3-4 s each) would leave long gaps.
    briefing: bool = False


@dataclass(frozen=True)
class BriefingAlert:
    """A composed morning briefing, queued like a reminder alert and spoken while Jarvis is idle."""

    spoken: str
    screen: str


@dataclass
class ConfirmRequest:
    """Sent from the LLM worker thread to the main thread, which asks the user by voice."""

    question: str
    done: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class _QueueConfirmer:
    """Confirmer used on the worker thread: hands the question to the main thread and waits."""

    def __init__(self, items: queue.Queue[Any]) -> None:
        self._items = items

    def confirm(self, question: str) -> bool:
        request = ConfirmRequest(question)
        self._items.put(request)
        request.done.wait()
        return request.approved


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
        alerts: queue.Queue[str | BriefingAlert] | None = None,
        sources: Sources | None = None,
        briefing_speaker: SpeakerLike | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._bus = bus or EventBus()  # what Jarvis is doing, for displays such as the HUD
        self._mode: tuple[str, str] = ("", "")  # last published (mode, detail)
        self._briefing_speaker = briefing_speaker or speaker  # premium voice where config allows
        self._sources = sources  # web search sources: shown on screen after the answer, never spoken
        self._alerts = alerts  # e.g. due reminders; spoken only while Jarvis is idle
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
        self._set_mode("idle")
        while True:
            event = self._next_event(timeout=1.0)
            if event is None:
                self.speak_alerts()
                continue
            if event != "start":
                continue
            samples = self._record()
            if samples is not None:
                try:
                    self.handle_turn(samples, ended_at=time.perf_counter())
                except Exception:
                    logger.exception("Unexpected error during voice turn")
                    print("[error] Something went wrong. Details are in logs/jarvis.log.")
                    self._error("internal", "Something went wrong. Details are in the log.")
            self._drain()  # ignore key presses made while Jarvis was busy
            self.speak_alerts()
            self._set_mode("idle")
            print("\n(hold the key to talk)")

    def speak_alerts(self) -> None:
        """Say any queued alerts, between turns only, so they never interrupt a reply."""
        while self._alerts is not None:
            try:
                alert = self._alerts.get_nowait()
            except queue.Empty:
                return
            if isinstance(alert, BriefingAlert):
                print(f"\n[briefing]\n{alert.screen}\njarvis> {alert.spoken}")
                self._bus.reply(alert.screen, kind="briefing")
                self._say(alert.spoken, self._briefing_speaker)
            else:
                print(f"\n[reminder] {for_terminal(alert)}")
                self._bus.reply(alert, kind="reminder")
                self._say(alert)
            self._set_mode("idle")

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
            self._error("microphone", str(e))
            return None
        self._set_mode("listening")
        print("listening...", end="", flush=True)
        self._next_event(timeout=self._max_record_seconds)  # "stop", or give up at the cap
        samples = self._recorder.stop()
        print(" done")
        return samples

    def handle_turn(self, samples: np.ndarray, ended_at: float) -> None:
        """Transcribe, ask the LLM, speak the reply. Never raises for provider failures."""
        try:
            self._handle_turn(samples, ended_at)
        finally:
            self._set_mode("idle")

    def _handle_turn(self, samples: np.ndarray, ended_at: float) -> None:
        audio_seconds = samples.size / SAMPLE_RATE
        if audio_seconds < MIN_SPEECH_SECONDS:
            print("(too short - hold the key while you speak)")
            return

        self._set_mode("thinking")
        started = time.perf_counter()
        try:
            transcript = self._stt.transcribe(samples)
        except STTError as e:
            print(f"[error] {e}")
            self._error("speech_recognition", str(e))
            return
        stt_ms = _ms_since(started)
        self._usage_sink({"kind": "stt", "model": self._stt.model, "audio_seconds": round(audio_seconds, 2),
                          "latency_ms": stt_ms})

        if not transcript.text or is_filler_only(transcript.text):
            print(f"(didn't catch that{': ' + for_terminal(transcript.text) if transcript.text else ''})")
            self._answer("Sorry, I didn't catch that.")
            return
        print(f"you said> {for_terminal(transcript.text)}")
        self._bus.transcript(transcript.text)
        if is_reset_request(transcript.text):
            self._session.reset()
            print("(history cleared)")
            self._answer("Okay, starting a new conversation.")
            return

        # The LLM streams on a worker thread; this (main) thread speaks each sentence as soon as it
        # is complete, so Jarvis starts talking while the rest of the reply is still being written.
        sentences: queue.Queue[str | Ack | ConfirmRequest | None] = queue.Queue()
        outcome: dict[str, Any] = {}
        worker = threading.Thread(target=self._stream_reply, args=(transcript.text, sentences, outcome), daemon=True)
        worker.start()

        until_speak_ms: int | None = None
        spoken = 0
        answer: list[str] = []  # the spoken answer, for displays (fillers like "Let me check." left out)
        held: list[str] | None = None  # a briefing answer, collected to be spoken in one piece
        while (raw := self._next_sentence(sentences)) is not None:
            if isinstance(raw, Ack):
                print(f"jarvis> {raw.text}")
                self._say(raw.text)
                self._set_mode("thinking")  # the slow tool is still running
                if raw.briefing and held is None:
                    held = []
                continue
            if held is not None and isinstance(raw, str):
                if sentence := speakable(raw):
                    held.append(sentence)
                continue
            if isinstance(raw, ConfirmRequest):
                if spoken:
                    print()
                try:
                    raw.approved = self._confirm_by_voice(raw.question)
                finally:
                    raw.done.set()  # never leave the worker waiting
                self._set_mode("thinking")
                continue
            if not (sentence := speakable(raw)):
                continue  # nothing left to say once markdown is removed
            if until_speak_ms is None:
                until_speak_ms = _ms_since(ended_at)
                print("jarvis>", end="", flush=True)
            print(f" {sentence}", end="", flush=True)
            self._say(sentence)
            answer.append(sentence)
            spoken += 1
        if spoken:
            print()
        worker.join()
        if held:
            until_speak_ms = until_speak_ms or _ms_since(ended_at)
            print(f"jarvis> {' '.join(held)}")
            self._say(" ".join(held), self._briefing_speaker)
            answer += held
            spoken += len(held)
        found = self._sources.take() if self._sources is not None else []
        if found:
            print(format_sources(found))
        if answer:
            # Sent once the answer is complete: Jarvis starts speaking before the whole reply exists.
            self._bus.reply(" ".join(answer), [{"title": r.title, "url": r.url} for r in found])

        if "error" in outcome:
            error = outcome["error"]
            if not isinstance(error, LLMError):
                raise error  # unexpected bug: handled and logged by run()
            print(f"[error] {error}")
            self._error("language_service", str(error))
            if not spoken:
                self._say("Sorry, I couldn't reach my language service.")
            return
        if not spoken:
            until_speak_ms = _ms_since(ended_at)
            print("jarvis> (no answer)")
            self._answer("Sorry, I don't have an answer for that.")

        self._usage_sink({
            "kind": "voice_turn",
            "stt_ms": stt_ms,
            "llm_ms": outcome["llm_ms"],  # full reply, start to finish
            "until_speak_ms": until_speak_ms,  # end of speech -> first sentence handed to TTS
            "sentences": spoken,
            "rss_mb": round(psutil.Process().memory_info().rss / 1_048_576),
        })

    def _confirm_by_voice(self, question: str) -> bool:
        """Main thread: read the action back, then listen for an explicit spoken yes."""
        print(f"jarvis> {for_terminal(question)} (hold the key and say yes to confirm)")
        self._say(f"{question} Say yes to confirm.")
        self._set_mode("confirm", question)
        self._drain()  # ignore key presses made while the question was being read out
        approved = False
        if self._next_event(timeout=CONFIRM_WAIT_SECONDS) == "start" and (samples := self._record()) is not None:
            self._set_mode("thinking")
            try:
                answer = self._stt.transcribe(samples).text
            except STTError as e:
                print(f"[error] {e}")
                self._error("speech_recognition", str(e))
                answer = ""
            print(f"you said> {for_terminal(answer) or '(nothing)'}")
            if answer:
                self._bus.transcript(answer)
            approved = is_yes(answer)
        else:
            print("(no answer)")
        self._say("Okay." if approved else "Okay, I won't.")
        return approved

    def _stream_reply(self, text: str, sentences: queue.Queue[str | Ack | ConfirmRequest | None], outcome: dict[str, Any]) -> None:
        """Worker thread: stream the LLM reply and queue complete sentences. Always ends with None."""
        splitter = SentenceSplitter()
        started = time.perf_counter()
        acked = False

        def on_tool_start(name: str) -> None:
            nonlocal acked
            if name in _ACK_PHRASES and not acked:  # once per question, even if the model searches twice
                acked = True
                sentences.put(Ack(_ACK_PHRASES[name], briefing=name == "morning_briefing"))

        try:
            self._session.ask(text, on_text=lambda piece: [sentences.put(s) for s in splitter.feed(piece)],
                              confirmer=_QueueConfirmer(sentences), on_tool_start=on_tool_start)
            if rest := splitter.flush():
                sentences.put(rest)
        except Exception as e:  # handed to the main thread
            outcome["error"] = e
        finally:
            outcome["llm_ms"] = _ms_since(started)
            sentences.put(None)

    @staticmethod
    def _next_sentence(sentences: queue.Queue[str | Ack | ConfirmRequest | None]) -> str | Ack | ConfirmRequest | None:
        while True:
            try:
                return sentences.get(timeout=_POLL_SECONDS)  # timeout keeps Ctrl+C responsive on Windows
            except queue.Empty:
                continue

    def _say(self, text: str, speaker: SpeakerLike | None = None) -> None:
        self._set_mode("speaking")
        try:
            (speaker or self._speaker).speak(text)
        except TTSError as e:
            print(f"[voice error] {e}")
            self._error("voice_output", str(e))

    def _answer(self, text: str) -> None:
        """A short fixed answer (e.g. "Sorry, I didn't catch that."): shown as the reply and spoken."""
        self._bus.reply(text)
        self._say(text)

    def _set_mode(self, mode: str, detail: str = "") -> None:
        """Publish a mode change (only when it changes). Main thread only."""
        if (mode, detail) != self._mode:
            self._mode = (mode, detail)
            self._bus.state(mode, detail)

    def _error(self, code: str, message: str) -> None:
        """A user-visible failure: shown on displays; Jarvis carries on."""
        self._bus.error(code, message)
        self._set_mode("error", message)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def is_filler_only(text: str) -> bool:
    words = _words(text)
    return len(words) >= _MIN_FILLER_RUN and all(w in _FILLER_WORDS for w in words)


def is_yes(text: str) -> bool:
    """An explicit yes with no negation. Anything unclear counts as no."""
    words = _words(text)
    if not words or any(w in _NO_WORDS for w in words):
        return False
    joined = " ".join(words)
    return words[0] in _YES_WORDS or any(joined.startswith(p) for p in _YES_PHRASES)


def is_reset_request(text: str) -> bool:
    words = _words(text)
    if words[:1] == ["jarvis"]:
        words = words[1:]
    return " ".join(words) in RESET_PHRASES


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
