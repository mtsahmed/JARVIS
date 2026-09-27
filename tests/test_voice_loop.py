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
    def __init__(self, fail: bool = False) -> None:
        self._fail, self.calls = fail, 0

    @property
    def model(self) -> str:
        return "fake-llm"

    def generate(self, messages: Sequence[Message], instructions: str | None = None) -> LLMReply:
        self.calls += 1
        if self._fail:
            raise LLMError("llm down")
        return LLMReply(text="Four.", model="fake-llm", usage=None)


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
