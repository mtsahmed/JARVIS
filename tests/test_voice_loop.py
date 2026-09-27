import json
import queue
import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from jarvis.core.chat import ChatSession
from jarvis.llm.base import LLMError, LLMReply, Message
from jarvis.voice.loop import VoiceLoop
from jarvis.voice.stt.base import STTError, Transcript
from jarvis.voice.tts.base import TTSError

ONE_SECOND = np.zeros(16_000, dtype=np.float32)


class FakeSTT:
    def __init__(self, text: str = "what is two plus two", fail: bool = False) -> None:
        self._text, self._fail, self.calls = text, fail, 0

    @property
    def model(self) -> str:
        return "fake-stt"

    def transcribe(self, samples: np.ndarray) -> Transcript:
        self.calls += 1
        if self._fail:
            raise STTError("stt broke")
        return Transcript(self._text, samples.size / 16_000)


class FakeLLM:
    """Streams `pieces` through on_text, like the real adapter; can fail before or mid-stream."""

    def __init__(self, pieces: Sequence[str] = ("Fo", "ur."), fail: bool = False, fail_after: int | None = None) -> None:
        self._pieces, self._fail, self._fail_after, self.calls = list(pieces), fail, fail_after, 0

    @property
    def model(self) -> str:
        return "fake-llm"

    def generate(self, messages: Sequence[Message], instructions: str | None = None, on_text=None) -> LLMReply:
        self.calls += 1
        if self._fail:
            raise LLMError("llm down")
        for i, piece in enumerate(self._pieces):
            if self._fail_after is not None and i == self._fail_after:
                raise LLMError("connection dropped")
            if on_text:
                on_text(piece)
        return LLMReply(text="".join(self._pieces), model="fake-llm", usage=None)


class FakeSpeaker:
    def __init__(self, fail: bool = False) -> None:
        self._fail, self.spoken = fail, []

    def speak(self, text: str) -> str:
        if self._fail:
            raise TTSError("speaker broke")
        self.spoken.append(text)
        return "sapi"


def make(stt: FakeSTT | None = None, llm: FakeLLM | None = None, speaker: FakeSpeaker | None = None):
    stt, llm, speaker = stt or FakeSTT(), llm or FakeLLM(), speaker or FakeSpeaker()
    records: list[dict[str, Any]] = []
    session = ChatSession(llm, history_turns=5, usage_sink=records.append)
    loop = VoiceLoop(session, stt, speaker, recorder=None, events=queue.Queue(), drain=lambda: None,
                     max_record_seconds=30, usage_sink=records.append)
    return loop, stt, llm, speaker, records


def test_full_turn_speaks_reply_and_logs_metrics() -> None:
    loop, stt, llm, speaker, records = make()
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == ["Four."]
    kinds = [r["kind"] for r in records]
    assert kinds == ["stt", "llm", "voice_turn"]
    turn = records[-1]
    assert {"stt_ms", "llm_ms", "until_speak_ms", "rss_mb"} <= turn.keys()
    assert turn["rss_mb"] > 0


def test_metrics_never_contain_speech_text() -> None:
    loop, *_, records = make(stt=FakeSTT("my secret plan"))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    dumped = json.dumps(records)
    assert "my secret plan" not in dumped and "Four." not in dumped


def test_too_short_recording_skips_stt() -> None:
    loop, stt, llm, speaker, _ = make()
    loop.handle_turn(np.zeros(1_000, dtype=np.float32), ended_at=time.perf_counter())
    assert stt.calls == 0 and llm.calls == 0 and speaker.spoken == []


def test_nothing_recognized_apologizes_without_llm() -> None:
    loop, _, llm, speaker, _ = make(stt=FakeSTT(""))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert llm.calls == 0
    assert speaker.spoken == ["Sorry, I didn't catch that."]


def test_stt_error_does_not_reach_llm() -> None:
    loop, _, llm, speaker, _ = make(stt=FakeSTT(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert llm.calls == 0 and speaker.spoken == []


def test_llm_error_is_spoken_not_raised() -> None:
    loop, _, _, speaker, _ = make(llm=FakeLLM(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == ["Sorry, I couldn't reach my language service."]


def test_speaker_error_does_not_raise() -> None:
    loop, *_ = make(speaker=FakeSpeaker(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())  # must not raise


def test_reply_spoken_sentence_by_sentence_in_order() -> None:
    pieces = ["Paris is the capital", " of France. It has about", " two million people. Anything else", "?"]
    loop, *_, speaker, records = make(llm=FakeLLM(pieces))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == [
        "Paris is the capital of France.",
        "It has about two million people.",
        "Anything else?",
    ]
    assert records[-1]["sentences"] == 3


def test_first_sentence_spoken_before_reply_finishes() -> None:
    events: list[str] = []

    class SlowLLM(FakeLLM):
        def generate(self, messages, instructions=None, on_text=None):  # type: ignore[no-untyped-def]
            on_text("First sentence here. ")
            time.sleep(0.3)  # rest of the reply still "generating"
            events.append("llm finished")
            on_text("Second one.")
            return LLMReply(text="First sentence here. Second one.", model="fake", usage=None)

    class RecordingSpeaker(FakeSpeaker):
        def speak(self, text: str) -> str:
            events.append(f"spoke: {text}")
            return super().speak(text)

    loop, *_ = make(llm=SlowLLM(), speaker=RecordingSpeaker())
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert events[0] == "spoke: First sentence here."
    assert events.index("llm finished") > 0


def test_error_mid_stream_keeps_spoken_part_and_does_not_apologize() -> None:
    loop, *_, speaker, records = make(llm=FakeLLM(["One sentence done. ", "Two"], fail_after=1))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == ["One sentence done."]
    assert records[-1]["kind"] == "llm" and records[-1]["status"] == "error"


def test_empty_reply_gets_fallback_answer() -> None:
    loop, *_, speaker, records = make(llm=FakeLLM([]))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == ["Sorry, I don't have an answer for that."]
    assert records[-1]["kind"] == "voice_turn"
