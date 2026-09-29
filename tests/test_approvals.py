"""UI-3B: approvals answerable by voice, typing, or the HUD; first answer wins (protocol §6)."""

import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis import cli
from jarvis.core.approvals import Approvals, action_label
from jarvis.core.events import EventBus
from jarvis.core.inbox import Inbox, Typed

from test_events import Recorder
from test_voice_loop import ONE_SECOND, make_confirming


def approvals() -> tuple[Approvals, list[dict[str, Any]], Recorder]:
    bus, audit = EventBus(), []
    return Approvals(bus, audit_sink=audit.append), audit, Recorder(bus)


def test_request_is_published_with_protocol_fields() -> None:
    a, _, rec = approvals()
    approval = a.open("Open budget.xlsx?", "open_file", timeout=15)
    uuid.UUID(approval.request_id)
    assert rec.of("confirm_request") == [{"request_id": approval.request_id, "action": "Open file", "target": "",
                                          "summary": "Open budget.xlsx?", "tier": "CONFIRM", "expires_in_s": 15}]
    assert a.pending() == [approval] and not approval.decided
    assert a.open("x", "open_file").request_id != approval.request_id  # single-use ids


def test_first_answer_wins_and_later_ones_are_ignored_and_audited() -> None:
    a, audit, rec = approvals()
    approval = a.open("Open x?", "open_file")
    assert a.answer(approval.request_id, True, "screen") is True
    assert a.answer(approval.request_id, False, "voice") is False  # too late: already decided
    assert approval.approved and approval.by == "screen" and a.pending() == []
    assert rec.of("confirm_result") == [{"request_id": approval.request_id, "outcome": "approved", "by": "screen"}]
    assert audit == [{"approval": approval.request_id, "action": "Open file", "outcome": "approved", "by": "screen"},
                     {"approval": approval.request_id, "status": "ignored", "by": "voice"}]


def test_unknown_request_ids_are_ignored() -> None:
    a, audit, rec = approvals()
    assert a.answer("made-up", True, "screen") is False
    assert rec.of("confirm_result") == [] and audit[0]["status"] == "ignored"
    with pytest.raises(ValueError):
        a.answer("made-up", True, "the internet")


def test_no_answer_in_time_is_denied() -> None:
    a, audit, rec = approvals()
    approval = a.open("Open x?", "open_file", timeout=0.05)
    time.sleep(0.1)
    assert a.answer(approval.request_id, True, "screen") is False  # a late yes does not count
    assert approval.outcome == "expired" and approval.by == "timeout" and not approval.approved
    assert rec.of("confirm_result")[0]["outcome"] == "expired"
    a.expire(approval)  # harmless when already settled
    assert len(rec.of("confirm_result")) == 1


def test_simultaneous_answers_have_exactly_one_winner() -> None:
    a, *_ = approvals()
    for _ in range(20):
        approval = a.open("Open x?", "open_file")
        wins: list[bool] = []
        threads = [threading.Thread(target=lambda i=i: wins.append(a.answer(approval.request_id, i % 2 == 0,
                                                                              ("voice", "screen")[i % 2])))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert wins.count(True) == 1


def test_action_label() -> None:
    assert action_label("open_file") == "Open file" and action_label("") == "Action"


def test_registry_tells_the_confirmer_which_tool_asks() -> None:
    from jarvis.tools.base import Tier, Tool
    from jarvis.tools.registry import ToolRegistry

    asked: list[tuple[str, str]] = []

    class Spy:
        def confirm(self, question: str, action: str = "") -> bool:
            asked.append((question, action))
            return True

    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    registry = ToolRegistry([Tool("open_file", "d", schema, Tier.CONFIRM, handler=lambda a: "ok",
                                  describe=lambda a: "Open it?")], audit_sink=lambda r: None)
    assert registry.run("open_file", "{}", Spy()) == "ok" and asked == [("Open it?", "open_file")]


# --- voice mode ---------------------------------------------------------------------------------------


def confirming(monkeypatch: pytest.MonkeyPatch, wait: float = 15.0):  # type: ignore[no-untyped-def]
    import jarvis.voice.loop as loop_module

    monkeypatch.setattr(loop_module, "CONFIRM_WAIT_SECONDS", wait)
    loop, speaker, opened = make_confirming("unused", [])
    bus, audit = EventBus(), []
    loop._approvals = Approvals(bus, audit_sink=audit.append)
    return loop, speaker, opened, Recorder(bus), audit


def click_when_asked(loop, speaker, approved: bool, delay: float = 0.3) -> None:  # type: ignore[no-untyped-def]
    """The user clicks Approve/Deny on the HUD shortly after the question appears."""
    original = speaker.speak

    def speak(text: str) -> str:
        if text.endswith("Say yes to confirm."):
            request_id = loop._approvals.pending()[0].request_id
            threading.Timer(delay, lambda: loop._approvals.answer(request_id, approved, "screen")).start()
        return original(text)

    speaker.speak = speak


@pytest.mark.parametrize(("approved", "said"), [(True, "Okay."), (False, "Okay, I won't.")])
def test_hud_click_answers_a_spoken_question(monkeypatch: pytest.MonkeyPatch, approved: bool, said: str) -> None:
    loop, speaker, opened, rec, audit = confirming(monkeypatch)
    click_when_asked(loop, speaker, approved)
    started = time.monotonic()
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert time.monotonic() - started < 3  # did not wait for the 15 s deadline
    assert opened == (["budget.xlsx"] if approved else []) and said in speaker.spoken
    assert audit[-1]["by"] == "screen" and rec.of("confirm_result")[0]["by"] == "screen"


def test_hud_click_while_recording_wins_and_the_recording_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, speaker, opened, _, audit = confirming(monkeypatch)
    original = speaker.speak

    def speak(text: str) -> str:
        if text.endswith("Say yes to confirm."):
            request_id = loop._approvals.pending()[0].request_id
            loop._events.put("start")  # the user starts to answer by voice...
            threading.Timer(0.3, lambda: loop._approvals.answer(request_id, False, "screen")).start()  # ...but clicks Deny
            threading.Timer(0.6, lambda: loop._events.put("stop")).start()
        return original(text)

    speaker.speak = speak
    loop._stt._text = "yes"  # what the recording would have said
    calls_before = loop._stt.calls
    loop._drain_keys = lambda: None  # keep the "start" put while the question was read out
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == [] and audit[-1] == {**audit[-1], "outcome": "denied", "by": "screen"}
    assert loop._stt.calls == calls_before + 1  # only the request itself was transcribed


def test_spoken_timeout_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, speaker, opened, rec, audit = confirming(monkeypatch, wait=0.3)
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == [] and "Okay, I won't." in speaker.spoken
    assert audit[-1]["outcome"] == "expired" and audit[-1]["by"] == "timeout"
    assert rec.of("confirm_request")[0]["expires_in_s"] == 0.3


def test_typed_hud_answer_counts_as_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, speaker, opened, _, audit = confirming(monkeypatch)
    original = speaker.speak

    def speak(text: str) -> str:
        if text.endswith("Say yes to confirm."):
            threading.Timer(0.3, lambda: loop._events.put(Typed("yes", "hud"))).start()
        return original(text)

    speaker.speak = speak
    loop.handle_turn(ONE_SECOND, ended_at=time.perf_counter())
    assert opened == ["budget.xlsx"] and audit[-1]["by"] == "screen"


# --- text mode ----------------------------------------------------------------------------------------


def text_confirmer(monkeypatch: pytest.MonkeyPatch, timeout: float = 60.0):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "TEXT_TIMEOUT_SECONDS", timeout)
    inbox, bus, audit = Inbox(), EventBus(), []
    a = Approvals(bus, audit_sink=audit.append)
    reader = SimpleNamespace(request=lambda prompt: None)  # the terminal stays silent
    return cli.TextConfirmer(inbox, reader, a, bus), inbox, a, audit, Recorder(bus)  # type: ignore[arg-type]


def test_text_mode_hud_click_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    confirmer, _, a, audit, rec = text_confirmer(monkeypatch)
    threading.Timer(0.3, lambda: a.answer(a.pending()[0].request_id, True, "screen")).start()
    assert confirmer.confirm("Open x.pdf?", "open_file") is True
    assert audit[-1]["by"] == "screen"
    assert rec.modes() == ["confirm", "thinking"] and rec.of("confirm_request")[0]["expires_in_s"] == 60.0


def test_text_mode_terminal_answer_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    confirmer, inbox, _, audit, _ = text_confirmer(monkeypatch, timeout=0.3)
    inbox.type("y", "terminal")
    assert confirmer.confirm("Open x.pdf?", "open_file") is True and audit[-1]["by"] == "terminal"
    assert confirmer.confirm("Open x.pdf?", "open_file") is False  # nobody answers
    assert audit[-1]["outcome"] == "expired"
