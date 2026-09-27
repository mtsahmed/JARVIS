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

    def generate(self, messages: Sequence[Message], instructions: str | None = None, on_text=None, tools=(), run_tool=None) -> LLMReply:
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
        def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
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


def test_markdown_reply_is_cleaned_before_speaking() -> None:
    pieces = ["Here are options:\n", "- **Quick** questions\n", "***\n", "- Task planning\n"]
    loop, *_, speaker, records = make(llm=FakeLLM(pieces))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert speaker.spoken == ["Here are options:", "Quick questions", "Task planning"]
    assert records[-1]["sentences"] == 3


def test_voice_instructions_ask_for_short_plain_speech() -> None:
    from jarvis.voice.loop import VOICE_INSTRUCTIONS

    assert "spoken aloud" in VOICE_INSTRUCTIONS and "Never use lists" in VOICE_INSTRUCTIONS


def test_filler_only_transcript_treated_as_not_understood() -> None:
    loop, _, llm, speaker, _ = make(stt=FakeSTT("Okay. Okay. All right. Okay. Yeah. All right."))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert llm.calls == 0
    assert speaker.spoken == ["Sorry, I didn't catch that."]


def test_short_yes_still_reaches_llm() -> None:
    loop, _, llm, _, _ = make(stt=FakeSTT("Yes."))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert llm.calls == 1


def test_spoken_reset_clears_history_without_llm() -> None:
    loop, stt, llm, speaker, _ = make()
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert loop._session.history  # one turn remembered
    loop._stt = FakeSTT("Jarvis, new conversation.")
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert loop._session.history == ()
    assert llm.calls == 1
    assert speaker.spoken[-1] == "Okay, starting a new conversation."


def test_reset_word_inside_a_request_is_not_a_reset() -> None:
    from jarvis.voice.loop import is_reset_request

    assert is_reset_request("Start over!")
    assert not is_reset_request("Reset my alarm to seven")


def test_voice_instructions_cover_live_data() -> None:
    from jarvis.voice.loop import VOICE_INSTRUCTIONS

    assert "use the web_search tool" in VOICE_INSTRUCTIONS
    assert "cannot look it up live" in VOICE_INSTRUCTIONS  # honest fallback when search is off or fails
    assert "never read out web addresses" in VOICE_INSTRUCTIONS


# --- voice confirmation (CONFIRM tier) -------------------------------------------------------------


class FakeRecorder:
    def start(self) -> None:
        pass

    def stop(self) -> np.ndarray:
        return ONE_SECOND


class ConfirmingLLM(FakeLLM):
    """Calls a CONFIRM tool through run_tool, then answers with the tool result."""

    def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
        result = run_tool("open_file", '{"path": "budget.xlsx"}')
        on_text(f"Result: {result} ")
        return LLMReply(text=result, model="fake", usage=None, tool_calls=1)


def make_confirming(answer_text: str, key_events: list[str]):  # type: ignore[no-untyped-def]
    from jarvis.tools.base import Tier, Tool
    from jarvis.tools.registry import ToolRegistry

    opened: list[str] = []
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
              "additionalProperties": False}
    registry = ToolRegistry([Tool("open_file", "Open a file", schema, Tier.CONFIRM,
                                  handler=lambda a: opened.append(a["path"]) or "opened",
                                  describe=lambda a: f"Open {a['path']}?")], audit_sink=lambda r: None)
    events: queue.Queue[str] = queue.Queue()
    speaker = FakeSpeaker()
    stt = FakeSTT("open my budget")
    session = ChatSession(ConfirmingLLM(), history_turns=5, usage_sink=lambda r: None, tools=registry)
    loop = VoiceLoop(session, stt, speaker, FakeRecorder(), events, drain=lambda: None, max_record_seconds=5,
                     usage_sink=lambda r: None)

    original = stt.transcribe

    def transcribe(samples: np.ndarray) -> Transcript:
        # first call = the request; later calls = the spoken answer to the confirmation
        stt._text = answer_text if stt.calls >= 1 else "open my budget"
        return original(samples)

    stt.transcribe = transcribe  # type: ignore[method-assign]
    for event in key_events:
        events.put(event)
    return loop, speaker, opened


def test_spoken_yes_runs_confirm_action() -> None:
    loop, speaker, opened = make_confirming("Yes.", ["start", "stop"])
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == ["budget.xlsx"]
    assert speaker.spoken[0] == "Open budget.xlsx? Say yes to confirm."
    assert "Okay." in speaker.spoken


def test_spoken_no_declines() -> None:
    loop, speaker, opened = make_confirming("No, don't.", ["start", "stop"])
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == []
    assert "Okay, I won't." in speaker.spoken


def test_no_answer_declines(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import jarvis.voice.loop as loop_module

    monkeypatch.setattr(loop_module, "CONFIRM_WAIT_SECONDS", 0.3)
    loop, speaker, opened = make_confirming("Yes.", [])  # key never pressed
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == []
    assert "Okay, I won't." in speaker.spoken


def test_is_yes_only_for_clear_yes() -> None:
    from jarvis.voice.loop import is_yes

    assert is_yes("Yes.") and is_yes("Go ahead") and is_yes("Yeah sure")
    assert not is_yes("") and not is_yes("okay") and not is_yes("Yes, no wait") and not is_yes("I'm not sure")


def test_queued_reminder_spoken_between_turns() -> None:
    alerts: queue.Queue[str] = queue.Queue()
    speaker = FakeSpeaker()
    session = ChatSession(FakeLLM(), history_turns=5, usage_sink=lambda r: None)
    loop = VoiceLoop(session, FakeSTT(), speaker, recorder=None, events=queue.Queue(), drain=lambda: None,
                     max_record_seconds=5, usage_sink=lambda r: None, alerts=alerts)
    alerts.put("Reminder: stretch")
    loop.speak_alerts()
    assert speaker.spoken == ["Reminder: stretch"]
    loop.speak_alerts()  # nothing left
    assert speaker.spoken == ["Reminder: stretch"]
