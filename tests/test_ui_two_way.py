"""UI-3C: the HUD server takes the page's input: mic button, typed requests, approval clicks."""

import time
from collections.abc import Iterator
from typing import Any

import pytest

from jarvis.core.approvals import Approvals
from jarvis.core.events import EventBus
from jarvis.core.inbox import Inbox, Typed
from jarvis.ui import server as server_module
from jarvis.ui.server import HudServer

from test_ui_server import hello, open_ws, receive, send


@pytest.fixture
def two_way() -> Iterator[tuple[HudServer, Inbox, Approvals, list[dict[str, Any]]]]:
    bus, inbox, audit = EventBus(), Inbox(), []
    approvals = Approvals(bus, audit_sink=audit.append)
    server = HudServer(bus, 0, hello_timeout=0.5, inbox=inbox, approvals=approvals)
    server.start()
    yield server, inbox, approvals, audit
    server.stop()


def items(inbox: Inbox, count: int, timeout: float = 3.0) -> list[Any]:
    got, deadline = [], time.monotonic() + timeout
    while len(got) < count and time.monotonic() < deadline:
        try:
            got.append(inbox.queue.get(timeout=0.05))
        except Exception:
            pass
    return got


# --- mic button --------------------------------------------------------------------------------------


def test_mic_button_works_like_the_talk_key_in_voice_mode(two_way) -> None:  # type: ignore[no-untyped-def]
    server, inbox, *_ = two_way
    inbox.voice = True
    with open_ws(server) as ws:
        snapshot = hello(ws, server)["data"]
        assert snapshot["voice"] is True
        send(ws, "ptt", {"action": "start"})
        send(ws, "ptt", {"action": "stop"})
        send(ws, "ptt", {"action": "cancel"})
        assert items(inbox, 3) == ["start", "stop", "cancel"]


def test_mic_button_is_refused_in_text_mode(two_way) -> None:  # type: ignore[no-untyped-def]
    server, inbox, *_ = two_way
    with open_ws(server) as ws:
        assert hello(ws, server)["data"]["voice"] is False
        send(ws, "ptt", {"action": "start"})
        error = receive(ws)["data"]
    assert error["code"] == "voice_unavailable" and inbox.queue.empty()


def test_page_closing_mid_recording_cancels_it(two_way) -> None:  # type: ignore[no-untyped-def]
    server, inbox, *_ = two_way
    inbox.voice = True
    with open_ws(server) as ws:
        hello(ws, server)
        send(ws, "ptt", {"action": "start"})
        assert items(inbox, 1) == ["start"]
    assert items(inbox, 1) == ["cancel"]  # the recording is thrown away, not sent


# --- typed requests ----------------------------------------------------------------------------------


def test_typed_request_goes_to_the_inbox(two_way) -> None:  # type: ignore[no-untyped-def]
    server, inbox, *_ = two_way
    with open_ws(server) as ws:
        hello(ws, server)
        send(ws, "text_input", {"text": "  what's the time?  "})
        assert items(inbox, 1) == [Typed("what's the time?", "hud")]
        send(ws, "text_input", {"text": "x" * 2001})
        assert receive(ws)["data"]["code"] == "bad_message" and inbox.queue.empty()


def test_typed_requests_are_rate_limited(two_way, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server_module, "TEXT_LIMIT", (2, 60.0))
    server, inbox, *_ = two_way
    with open_ws(server) as ws:
        hello(ws, server)
        for i in range(3):
            send(ws, "text_input", {"text": f"question {i}"})
        assert receive(ws)["data"]["code"] == "rate_limited"
        assert [t.text for t in items(inbox, 3, timeout=0.5)] == ["question 0", "question 1"]
        send(ws, "ping")
        assert receive(ws)["type"] == "pong"  # still connected


# --- approvals ---------------------------------------------------------------------------------------


def test_approval_dialog_round_trip(two_way) -> None:  # type: ignore[no-untyped-def]
    server, _, approvals, audit = two_way
    with open_ws(server) as ws:
        hello(ws, server)
        approval = approvals.open("Open budget.xlsx?", "open_file")
        request = receive(ws)
        assert request["type"] == "confirm_request"
        assert request["data"] == {"request_id": approval.request_id, "action": "Open file", "target": "",
                                   "summary": "Open budget.xlsx?", "tier": "CONFIRM", "expires_in_s": 15.0}
        send(ws, "confirm_response", {"request_id": approval.request_id, "approved": True})
        result = receive(ws)
    assert result["type"] == "confirm_result"
    assert result["data"] == {"request_id": approval.request_id, "outcome": "approved", "by": "screen"}
    assert approval.approved and audit[-1]["by"] == "screen"


def test_second_click_and_made_up_ids_change_nothing(two_way) -> None:  # type: ignore[no-untyped-def]
    server, _, approvals, audit = two_way
    with open_ws(server) as ws:
        hello(ws, server)
        approval = approvals.open("Open x?", "open_file")
        receive(ws)
        send(ws, "confirm_response", {"request_id": approval.request_id, "approved": False})
        assert receive(ws)["data"]["outcome"] == "denied"
        send(ws, "confirm_response", {"request_id": approval.request_id, "approved": True})  # too late
        send(ws, "confirm_response", {"request_id": "made-up", "approved": True})
        send(ws, "ping")
        assert receive(ws)["type"] == "pong"  # no second confirm_result
    assert approval.outcome == "denied"
    assert [a.get("status") for a in audit] == [None, "ignored", "ignored"]


def test_page_opened_mid_question_shows_the_dialog(two_way) -> None:  # type: ignore[no-untyped-def]
    server, _, approvals, _ = two_way
    approval = approvals.open("Open x?", "open_file", timeout=15)
    with open_ws(server) as ws:
        confirm = hello(ws, server)["data"]["confirm"]
        assert confirm["request_id"] == approval.request_id and 0 < confirm["expires_in_s"] <= 15
        approvals.answer(approval.request_id, True, "voice")
        assert receive(ws)["data"]["by"] == "voice"  # answered by voice: the dialog closes
    with open_ws(server) as ws:
        snapshot = hello(ws, server)["data"]
    assert snapshot["confirm"] is None
    assert snapshot["log"][-1] == {**snapshot["log"][-1], "role": "approval", "text": "Open x? approved by voice."}
