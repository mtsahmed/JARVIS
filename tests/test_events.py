"""UI-2A: the in-process event bus, and what the voice and text loops publish on it."""

import builtins
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.chat import ChatSession
from jarvis.core.events import Event, EventBus
from jarvis.tools.search.base import SearchResult
from jarvis.tools.web_search import Sources
from jarvis.voice.loop import BriefingAlert, VoiceLoop

from test_voice_loop import ONE_SECOND, FakeLLM, FakeSpeaker, FakeSTT, make_confirming


class Recorder:
    """Collects published events (thread-safe via list.append)."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.subscribe(self.events.append)

    def modes(self) -> list[str]:
        return [e.data["mode"] for e in self.events if e.type == "state"]

    def of(self, type: str) -> list[dict[str, Any]]:
        return [e.data for e in self.events if e.type == type]

    def types(self) -> list[str]:
        return [e.type if e.type != "state" else e.data["mode"] for e in self.events]


# --- the bus -----------------------------------------------------------------------------------------


def test_publish_reaches_every_subscriber_until_unsubscribed() -> None:
    bus, a, b = EventBus(), [], []
    stop_a = bus.subscribe(a.append)
    bus.subscribe(b.append)
    bus.state("thinking")
    stop_a()
    stop_a()  # unsubscribing twice is harmless
    bus.transcript("hello")
    assert a == [Event("state", {"mode": "thinking", "detail": ""})]
    assert [e.type for e in b] == ["state", "transcript"] and b[1].data == {"text": "hello", "final": True}


def test_failing_subscriber_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    bus, seen = EventBus(), []

    def broken(event: Event) -> None:
        raise RuntimeError("display crashed")

    bus.subscribe(broken)
    bus.subscribe(seen.append)
    bus.error("x", "message")  # must not raise
    assert seen[0].data == {"code": "x", "message": "message", "recoverable": True}
    assert "Event subscriber failed" in caplog.text


def test_helpers_use_protocol_shapes_and_reject_unknown_modes() -> None:
    bus = EventBus()
    rec = Recorder(bus)
    bus.reply("Four.", [{"title": "t", "url": "https://x"}])
    bus.reply("Stretch", kind="reminder")
    assert rec.of("reply") == [{"text": "Four.", "sources": [{"title": "t", "url": "https://x"}], "kind": "answer"},
                               {"text": "Stretch", "sources": [], "kind": "reminder"}]
    with pytest.raises(ValueError):
        bus.state("dancing")


def test_publish_from_many_threads_while_subscribing() -> None:
    bus, seen = EventBus(), []
    bus.subscribe(seen.append)
    threads = [threading.Thread(target=lambda: [bus.transcript("x") for _ in range(200)]) for _ in range(8)]
    threads.append(threading.Thread(target=lambda: [bus.subscribe(lambda e: None)() for _ in range(200)]))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 1600


# --- what the voice loop publishes ------------------------------------------------------------------


def voice_loop(stt: FakeSTT | None = None, llm: FakeLLM | None = None, speaker: FakeSpeaker | None = None,
               sources: Sources | None = None, alerts: queue.Queue | None = None) -> tuple[VoiceLoop, Recorder]:
    bus = EventBus()
    session = ChatSession(llm or FakeLLM(("Four. ", "Easy.")), history_turns=5, usage_sink=lambda r: None)
    loop = VoiceLoop(session, stt or FakeSTT(), speaker or FakeSpeaker(), recorder=None, events=queue.Queue(),
                     drain=lambda: None, max_record_seconds=5, usage_sink=lambda r: None, sources=sources,
                     alerts=alerts, bus=bus)
    return loop, Recorder(bus)


def test_voice_turn_sequence() -> None:
    sources = Sources()
    sources.add((SearchResult("Maths", "https://maths.example", "snippet"),))
    loop, rec = voice_loop(sources=sources)
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert rec.types() == ["thinking", "transcript", "speaking", "reply", "idle"]
    assert rec.of("transcript") == [{"text": "what is two plus two", "final": True}]
    # The reply comes once the answer is complete (after speaking has started), with its sources.
    assert rec.of("reply") == [{"text": "Four. Easy.", "sources": [{"title": "Maths", "url": "https://maths.example"}],
                                "kind": "answer"}]


def test_stt_failure_publishes_error_then_idle() -> None:
    loop, rec = voice_loop(stt=FakeSTT(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert rec.types() == ["thinking", "error", "error", "idle"]  # the error event, then the error mode
    assert rec.of("error") == [{"code": "speech_recognition", "message": "stt broke", "recoverable": True}]


def test_llm_failure_publishes_error_and_the_apology_is_spoken() -> None:
    loop, rec = voice_loop(llm=FakeLLM(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert rec.of("error")[0]["code"] == "language_service"
    assert rec.modes()[-3:] == ["error", "speaking", "idle"] and rec.of("reply") == []


def test_not_understood_is_shown_as_a_reply() -> None:
    loop, rec = voice_loop(stt=FakeSTT(""))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert rec.of("transcript") == [] and rec.of("reply")[0]["text"] == "Sorry, I didn't catch that."
    assert rec.modes() == ["thinking", "speaking", "idle"]


def test_too_short_recording_goes_straight_back_to_idle() -> None:
    import numpy as np

    loop, rec = voice_loop()
    loop.handle_turn(np.zeros(100, dtype=np.float32), ended_at=time.perf_counter())
    assert rec.types() == ["idle"]


def test_voice_output_failure_is_published() -> None:
    loop, rec = voice_loop(speaker=FakeSpeaker(fail=True))
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert rec.of("error")[0] == {"code": "voice_output", "message": "speaker broke", "recoverable": True}


def test_confirmation_publishes_confirm_mode_with_the_question() -> None:
    loop, _, opened = make_confirming("Yes.", ["start", "stop"])
    bus = EventBus()
    rec = Recorder(bus)
    loop._bus = bus
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    confirm = [e.data for e in rec.events if e.type == "state" and e.data["mode"] == "confirm"]
    assert opened == ["budget.xlsx"] and confirm == [{"mode": "confirm", "detail": "Open budget.xlsx?"}]
    assert rec.modes().index("confirm") < rec.modes().index("listening")  # then the spoken answer
    assert [t["text"] for t in rec.of("transcript")] == ["open my budget", "Yes."]
    assert rec.modes()[-1] == "idle"


def test_reminders_and_briefings_are_published_as_replies() -> None:
    alerts: queue.Queue = queue.Queue()
    loop, rec = voice_loop(alerts=alerts)
    alerts.put("Reminder: stretch")
    alerts.put(BriefingAlert(spoken="Two emails.", screen="Inbox: 2 new"))
    loop.speak_alerts()
    assert [(r["kind"], r["text"]) for r in rec.of("reply")] == [("reminder", "Reminder: stretch"),
                                                                 ("briefing", "Inbox: 2 new")]
    assert rec.modes()[-1] == "idle"


def test_no_events_needed_without_a_bus() -> None:
    session = ChatSession(FakeLLM(), history_turns=5, usage_sink=lambda r: None)
    speaker = FakeSpeaker()
    loop = VoiceLoop(session, FakeSTT(), speaker, recorder=None, events=queue.Queue(), drain=lambda: None,
                     max_record_seconds=5, usage_sink=lambda r: None)
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())  # works exactly as before
    assert speaker.spoken == ["Four."]


# --- what text mode publishes -----------------------------------------------------------------------


def test_text_mode_sequence(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jarvis import cli

    answers = iter(["what is two plus two", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    bus = EventBus()
    rec = Recorder(bus)
    session = ChatSession(FakeLLM(("Four.",)), history_turns=3, usage_sink=lambda r: None)
    cli.run_text(SimpleNamespace(llm_model="fake"), session, Sources(), bus)
    assert rec.types() == ["idle", "transcript", "thinking", "reply", "idle"]

    answers = iter(["hello", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    rec.events.clear()
    cli.run_text(SimpleNamespace(llm_model="fake"), ChatSession(FakeLLM(fail=True), 3, usage_sink=lambda r: None),
                 Sources(), bus)
    assert rec.types() == ["idle", "transcript", "thinking", "error", "idle"]
