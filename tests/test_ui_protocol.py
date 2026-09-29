"""UI-2B: HUD protocol checks on client messages, and server message envelopes."""

import json
import uuid
from datetime import datetime
from typing import Any

import pytest

from jarvis.ui.protocol import MAX_MESSAGE_BYTES, ProtocolError, envelope, parse_client


def msg(type: str = "ping", data: Any = None, **overrides: Any) -> str:
    message = {"v": 1, "type": type, "id": "abc", "ts": "2026-09-28T09:41:05Z", "data": {} if data is None else data}
    message.update(overrides)
    return json.dumps(message)


HELLO = {"token": "t0ken", "client": "hud"}


def test_valid_hello_and_ping() -> None:
    hello = parse_client(msg("hello", HELLO))
    assert hello is not None and hello.type == "hello" and hello.data == HELLO
    assert parse_client(msg("hello", {"token": "t", "client": "widget"})) is not None
    ping = parse_client(msg())
    assert ping is not None and ping.type == "ping" and ping.id == "abc"


@pytest.mark.parametrize("type", ["run_tool", "shutdown", "read_file", "set_config", "PTT"])
def test_unknown_types_are_ignored(type: str) -> None:
    assert parse_client(msg(type, {"anything": 1})) is None  # the page can't do anything else


def test_valid_ui3_messages() -> None:
    for action in ("start", "stop", "cancel"):
        assert parse_client(msg("ptt", {"action": action})).data == {"action": action}  # type: ignore[union-attr]
    assert parse_client(msg("text_input", {"text": "x" * 2000})) is not None
    reply = parse_client(msg("confirm_response", {"request_id": "abc", "approved": False}))
    assert reply is not None and reply.data == {"request_id": "abc", "approved": False}


@pytest.mark.parametrize(("type", "data"), [
    ("ptt", {}), ("ptt", {"action": "record"}), ("ptt", {"action": "start", "x": 1}), ("ptt", {"action": 1}),
    ("text_input", {}), ("text_input", {"text": ""}), ("text_input", {"text": "   "}),
    ("text_input", {"text": "x" * 2001}), ("text_input", {"text": 5}), ("text_input", {"text": "hi", "tool": "x"}),
    ("confirm_response", {"request_id": "abc"}), ("confirm_response", {"approved": True}),
    ("confirm_response", {"request_id": "abc", "approved": "yes"}),  # must be a real true/false
    ("confirm_response", {"request_id": "abc", "approved": 1}),
    ("confirm_response", {"request_id": "", "approved": True}),
    ("confirm_response", {"request_id": "x" * 65, "approved": True}),
    ("confirm_response", {"request_id": "abc", "approved": True, "by": "voice"}),  # the page can't claim a channel
])
def test_malformed_ui3_messages_are_rejected(type: str, data: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError) as error:
        parse_client(msg(type, data))
    assert error.value.code == "bad_message" and not error.value.fatal


@pytest.mark.parametrize("raw", [
    b'{"v": 1}',  # binary frame
    "not json",
    "[1, 2]",
    msg(extra="field"),
    json.dumps({"v": 1, "type": "ping", "id": "a", "data": {}}),  # no ts
    msg(type=5),
    msg(id=""),
    msg(id="x" * 65),
    msg(ts=12),
    msg(data=[]),
    msg("ping", {"x": 1}),
    msg("hello", {"token": "t"}),
    msg("hello", {"token": "t", "client": "browser"}),
    msg("hello", {"token": "", "client": "hud"}),
    msg("hello", {"token": 123, "client": "hud"}),
    msg("hello", {"token": "t" * 257, "client": "hud"}),
    msg("hello", {**HELLO, "admin": True}),
])
def test_malformed_messages_are_rejected(raw: str | bytes) -> None:
    with pytest.raises(ProtocolError) as error:
        parse_client(raw)
    assert error.value.code == "bad_message" and not error.value.fatal


@pytest.mark.parametrize("v", [2, 0, "1", True, None])
def test_version_mismatch_is_fatal(v: Any) -> None:
    with pytest.raises(ProtocolError) as error:
        parse_client(msg(v=v))
    assert error.value.code == "version_mismatch" and error.value.fatal


def test_size_limit() -> None:
    big = msg("hello", {"token": "t", "client": "hud"}, id="a", ts="x" * 10)
    assert parse_client(big) is not None
    with pytest.raises(ProtocolError):
        parse_client(msg(ts="x" * MAX_MESSAGE_BYTES))


def test_errors_never_echo_the_message() -> None:
    with pytest.raises(ProtocolError) as error:
        parse_client(msg("hello", {"token": "SECRET-TOKEN", "client": "evil"}))
    assert "SECRET-TOKEN" not in str(error.value)


def test_envelope() -> None:
    message = json.loads(envelope("state", {"mode": "idle"}))
    assert set(message) == {"v", "type", "id", "ts", "data"} and message["v"] == 1
    assert message["type"] == "state" and message["data"] == {"mode": "idle"}
    uuid.UUID(message["id"])
    assert message["ts"].endswith("Z") and datetime.fromisoformat(message["ts"])
    assert json.loads(envelope("pong"))["data"] == {}
    assert json.loads(envelope("reply", {"text": "<b>x</b> ü"}))["data"]["text"] == "<b>x</b> ü"  # text stays text
