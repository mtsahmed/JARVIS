"""UI-3A: one inbox for talk-key events and typed requests, in voice mode and text mode."""

import builtins
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis import cli
from jarvis.core.chat import ChatSession
from jarvis.core.events import EventBus
from jarvis.core.inbox import MAX_TYPED, Inbox, Typed
from jarvis.tools.web_search import Sources
from jarvis.voice.loop import VoiceLoop

from test_events import Recorder
from test_voice_loop import ONE_SECOND, FakeLLM, FakeRecorder, FakeSpeaker, FakeSTT, make_confirming


def test_inbox_checks_what_it_accepts() -> None:
    inbox = Inbox()
    inbox.key("start")
    inbox.key("cancel")
    with pytest.raises(ValueError):
        inbox.key("shutdown")
    assert inbox.type("  hello  ") is True
    assert inbox.type("   ") is False and inbox.type("x" * (MAX_TYPED + 1)) is False
    assert inbox.type("x" * MAX_TYPED, "terminal") is True
    items = [inbox.queue.get_nowait() for _ in range(4)]
    assert items[:3] == ["start", "cancel", Typed("hello", "hud")] and items[3].source == "terminal"
    assert inbox.queue.empty()


# --- voice mode --------------------------------------------------------------------------------------


def voice(stt: FakeSTT | None = None, llm: FakeLLM | None = None) -> tuple[VoiceLoop, Inbox, FakeSpeaker, list, Recorder]:
    inbox, speaker, records, bus = Inbox(), FakeSpeaker(), [], EventBus()
    session = ChatSession(llm or FakeLLM(("Four.",)), history_turns=5, usage_sink=lambda r: None)
    loop = VoiceLoop(session, stt or FakeSTT(), speaker, FakeRecorder(), inbox.queue, max_record_seconds=5,
                     usage_sink=records.append, bus=bus)
    return loop, inbox, speaker, records, Recorder(bus)


def test_typed_request_is_a_full_turn_with_a_spoken_reply() -> None:
    loop, _, speaker, records, rec = voice()
    stt = loop._stt
    loop.handle_typed("what is two plus two")
    assert speaker.spoken == ["Four."] and stt.calls == 0  # no speech recognition needed
    assert rec.types() == ["transcript", "thinking", "speaking", "reply", "idle"]
    assert rec.of("transcript") == [{"text": "what is two plus two", "final": True}]
    assert records[-1]["kind"] == "voice_turn" and records[-1]["input"] == "typed"


def test_typed_reset_starts_a_new_conversation() -> None:
    loop, _, speaker, _, _ = voice()
    loop.handle_typed("start over")
    assert speaker.spoken == ["Okay, starting a new conversation."]


def test_cancel_throws_the_recording_away() -> None:
    loop, inbox, *_ = voice()
    inbox.key("cancel")
    assert loop._record() is None


def test_typed_request_while_recording_waits_its_turn() -> None:
    loop, inbox, *_ = voice()
    inbox.type("remind me later")
    inbox.key("start")  # a repeated start is ignored while recording
    inbox.key("stop")
    assert loop._record() is not None  # the typed request did not end the recording
    assert loop._next_event(timeout=0.1) == Typed("remind me later", "hud")  # ...and was kept


def test_busy_drain_drops_key_presses_but_keeps_typed_requests() -> None:
    loop, inbox, *_ = voice()
    inbox.key("start")
    inbox.type("first")
    inbox.key("stop")
    inbox.type("second")
    loop._drain_keys()
    assert [loop._next_event(timeout=0.1), loop._next_event(timeout=0.1), loop._next_event(timeout=0.1)] == \
        [Typed("first", "hud"), Typed("second", "hud"), None]


def run_until(loop: VoiceLoop, turns: list, count: int) -> None:
    threading.Thread(target=loop.run, daemon=True).start()  # runs until the test process ends
    deadline = time.monotonic() + 5
    while len(turns) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)  # give any unexpected extra turn a chance to show up


def watch_turns(loop: VoiceLoop, monkeypatch: pytest.MonkeyPatch) -> list:
    turns: list = []
    original_typed, original_turn = loop.handle_typed, loop.handle_turn
    monkeypatch.setattr(loop, "handle_typed", lambda text: (turns.append(text), original_typed(text)))
    monkeypatch.setattr(loop, "handle_turn", lambda s, ended_at: (turns.append("spoken"), original_turn(s, ended_at)))
    return turns


def test_run_handles_spoken_then_typed_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, inbox, speaker, _, _ = voice(llm=FakeLLM(("Done.",)))
    inbox.key("start")
    inbox.key("stop")
    inbox.type("typed while Jarvis was busy")  # kept, handled next
    turns = watch_turns(loop, monkeypatch)
    run_until(loop, turns, 2)
    assert turns == ["spoken", "typed while Jarvis was busy"]
    assert speaker.spoken.count("Done.") == 2


def test_key_presses_made_during_a_typed_turn_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, inbox, *_ = voice(llm=FakeLLM(("Done.",)))
    inbox.type("first")
    inbox.key("start")  # pressed while Jarvis was busy with "first": ignored, as for spoken turns
    inbox.key("stop")
    inbox.type("second")
    turns = watch_turns(loop, monkeypatch)
    run_until(loop, turns, 2)
    assert turns == ["first", "second"]


def test_typed_yes_answers_a_confirmation() -> None:
    loop, speaker, opened = make_confirming("unused", [])

    original = speaker.speak

    def speak(text: str) -> str:  # the user types yes on the HUD after hearing the question
        if text.endswith("Say yes to confirm."):
            threading.Timer(0.3, lambda: loop._events.put(Typed("yes", "hud"))).start()
        return original(text)

    speaker.speak = speak  # type: ignore[method-assign]
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == ["budget.xlsx"] and "Okay." in speaker.spoken


def test_request_typed_before_the_question_is_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.voice.loop as loop_module

    monkeypatch.setattr(loop_module, "CONFIRM_WAIT_SECONDS", 0.3)
    loop, speaker, opened = make_confirming("unused", [])
    loop._pending.append(Typed("yes", "hud"))  # queued earlier, as its own request
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == [] and "Okay, I won't." in speaker.spoken
    assert loop._next_event(timeout=0.1) == Typed("yes", "hud")  # still waiting to be handled


# --- text mode ---------------------------------------------------------------------------------------


def terminal(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    answers = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)


class CountingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__(("Sure.",))
        self.asked: list[str] = []

    def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
        self.asked.append(messages[-1].content)
        return super().generate(messages, instructions, on_text, tools, run_tool)


def test_text_mode_takes_requests_from_the_hud_too(monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    llm, inbox = CountingLLM(), Inbox()
    inbox.type("from the screen")
    inbox.type("/exit")  # commands work only from the terminal: this is just text for the model
    terminal(monkeypatch, "from the keyboard", "/exit")
    cli.run_text(SimpleNamespace(llm_model="fake"), ChatSession(llm, 3, usage_sink=lambda r: None), Sources(),
                 EventBus(), inbox)
    assert llm.asked == ["from the screen", "/exit", "from the keyboard"]
    assert "you (HUD)> from the screen" in capsys.readouterr().out


def test_text_mode_confirmation_can_be_answered_on_the_hud(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox()
    terminal(monkeypatch)  # the terminal has nothing to say (end of input comes only if asked)
    reader = SimpleNamespace(request=lambda prompt: None)
    inbox.type("yes")
    assert cli.TextConfirmer(inbox, reader).confirm("Open x.pdf?") is True  # type: ignore[arg-type]
    inbox.type("maybe")
    assert cli.TextConfirmer(inbox, reader).confirm("Open x.pdf?") is False  # type: ignore[arg-type]


def test_text_mode_ignores_talk_key_events(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, inbox = CountingLLM(), Inbox()
    inbox.key("start")
    inbox.key("stop")
    terminal(monkeypatch, "hello", "/exit")
    cli.run_text(SimpleNamespace(llm_model="fake"), ChatSession(llm, 3, usage_sink=lambda r: None), Sources(),
                 EventBus(), inbox)
    assert llm.asked == ["hello"]
