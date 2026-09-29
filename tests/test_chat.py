from collections.abc import Sequence
from typing import Any

import pytest

from jarvis.core.chat import ChatSession
from jarvis.llm.base import LLMError, LLMReply, Message, Usage


class FakeLLM:
    """Records what it was sent; replies 'reply N'. No network, no cost."""

    def __init__(self, fail: bool = False, reply_text: str | None = None) -> None:
        self.calls: list[list[Message]] = []
        self._fail = fail
        self._reply_text = reply_text

    @property
    def model(self) -> str:
        return "fake-model"

    def generate(self, messages: Sequence[Message], instructions: str | None = None, on_text=None, tools=(), run_tool=None) -> LLMReply:
        self.calls.append(list(messages))
        if self._fail:
            raise LLMError("provider down")
        text = self._reply_text if self._reply_text is not None else f"reply {len(self.calls)}"
        return LLMReply(text=text, model="fake-model-001", usage=Usage(10, 5, 15, reasoning_tokens=2))


def make_session(llm: FakeLLM, history_turns: int = 10) -> tuple[ChatSession, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    return ChatSession(llm, history_turns=history_turns, usage_sink=records.append), records


def test_history_is_sent_on_next_turn() -> None:
    llm = FakeLLM()
    session, _ = make_session(llm)
    session.ask("hello")
    session.ask("again")
    assert llm.calls[1] == [
        Message("user", "hello"),
        Message("assistant", "reply 1"),
        Message("user", "again"),
    ]


def test_history_trimmed_to_configured_turns() -> None:
    llm = FakeLLM()
    session, _ = make_session(llm, history_turns=2)
    for i in range(5):
        session.ask(f"q{i}")
    # 2 previous turns (4 messages) + the new user message
    assert [m.content for m in llm.calls[-1]] == ["q2", "reply 3", "q3", "reply 4", "q4"]
    assert len(session.history) == 4


def test_reset_clears_history() -> None:
    llm = FakeLLM()
    session, _ = make_session(llm)
    session.ask("hello")
    session.reset()
    session.ask("fresh")
    assert llm.calls[-1] == [Message("user", "fresh")]


def test_usage_logged_per_request_without_content() -> None:
    session, records = make_session(FakeLLM())
    session.ask("my secret question")
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "llm"
    assert record["model"] == "fake-model-001"
    assert record["status"] == "ok"
    assert (record["input_tokens"], record["output_tokens"], record["total_tokens"]) == (10, 5, 15)
    assert record["reasoning_tokens"] == 2
    assert isinstance(record["latency_ms"], int)
    assert "my secret question" not in str(record)


def test_provider_error_is_logged_and_raised_history_unchanged() -> None:
    session, records = make_session(FakeLLM(fail=True))
    with pytest.raises(LLMError, match="provider down"):
        session.ask("hello")
    assert records[0]["status"] == "error"
    assert records[0]["model"] == "fake-model"
    assert session.history == ()


def test_empty_reply_not_added_to_history() -> None:
    session, _ = make_session(FakeLLM(reply_text=""))
    session.ask("hello")
    assert session.history == ()


def test_on_text_passed_through_to_llm() -> None:
    received: list[Any] = []

    class StreamingLLM(FakeLLM):
        def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
            received.append(on_text)
            return super().generate(messages, instructions)

    session, _ = make_session(StreamingLLM())
    callback = print
    session.ask("hi", on_text=callback)
    assert received == [callback]


def test_current_time_added_to_instructions() -> None:
    from datetime import datetime, timedelta, timezone

    seen: list[str] = []

    class Capture(FakeLLM):
        def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
            seen.append(instructions)
            return super().generate(messages, instructions)

    fixed = datetime(2026, 9, 27, 18, 30, tzinfo=timezone(timedelta(hours=3)))
    session = ChatSession(Capture(), history_turns=5, usage_sink=lambda r: None, clock=lambda: fixed)
    session.ask("hi")
    assert "Current local date and time: Sunday, 27 September 2026, 18:30 (UTC+0300)." in seen[0]


def test_tools_and_confirmer_reach_the_registry() -> None:
    from jarvis.tools.base import Tier, Tool
    from jarvis.tools.registry import ToolRegistry

    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    opened: list[str] = []
    registry = ToolRegistry([Tool("open_it", "Open it", schema, Tier.CONFIRM, handler=lambda a: opened.append("x") or "opened",
                                  describe=lambda a: "Open it?")], audit_sink=lambda r: None)

    class CallsTool(FakeLLM):
        def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
            assert [t.name for t in tools] == ["open_it"]
            result = run_tool("open_it", "{}")
            return LLMReply(text=result, model="fake", usage=None, tool_calls=1)

    class Yes:
        def confirm(self, question: str, action: str = "") -> bool:
            return True

    records: list[dict[str, Any]] = []
    session = ChatSession(CallsTool(), history_turns=5, usage_sink=records.append, tools=registry)
    assert session.ask("open it", confirmer=Yes()).text == "opened"
    assert opened == ["x"] and records[0]["tool_calls"] == 1


def test_reset_forgets_approvals() -> None:
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry([], audit_sink=lambda r: None)
    registry._approved.add(("read_file", "a.pdf"))
    session = ChatSession(FakeLLM(), history_turns=5, usage_sink=lambda r: None, tools=registry)
    session.reset()
    assert registry._approved == set()
