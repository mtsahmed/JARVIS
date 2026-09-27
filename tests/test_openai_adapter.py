"""OpenAI adapter streaming, with a fake client (no network, no cost)."""

from types import SimpleNamespace
from typing import Any

import httpx2  # the HTTP library the installed openai SDK uses
import openai
import pytest

from jarvis.llm.base import LLMError, Message
from jarvis.llm.openai_adapter import OpenAIAdapter


def response(status: str = "completed", text: str = "Hello there.", reason: str | None = None) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, total_tokens=15,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    return SimpleNamespace(
        status=status, output_text=text, model="fake-model-001", usage=usage, error=None,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
    )


def delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


class FakeStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


class FakeResponses:
    def __init__(self, events: list[Any]) -> None:
        self.stream = FakeStream(events)
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return self.stream if request.get("stream") else response()


def make(events: list[Any]) -> tuple[OpenAIAdapter, FakeResponses]:
    adapter = OpenAIAdapter("sk-test", "test-model", max_output_tokens=100, timeout_seconds=5, reasoning_effort="low")
    fake = FakeResponses(events)
    adapter._client = SimpleNamespace(responses=fake)  # type: ignore[assignment]
    return adapter, fake


HELLO = [Message("user", "hi")]


def test_stream_passes_deltas_and_returns_full_reply() -> None:
    adapter, fake = make([delta("Hello"), delta(" there."), SimpleNamespace(type="response.completed", response=response())])
    pieces: list[str] = []
    reply = adapter.generate(HELLO, on_text=pieces.append)
    assert pieces == ["Hello", " there."]
    assert reply.text == "Hello there."
    assert reply.usage is not None and reply.usage.reasoning_tokens == 2
    request = fake.requests[0]
    assert request["stream"] is True and request["store"] is False
    assert request["reasoning"] == {"effort": "low"}
    assert fake.stream.closed


def test_without_callback_no_streaming() -> None:
    adapter, fake = make([])
    assert adapter.generate(HELLO).text == "Hello there."
    assert "stream" not in fake.requests[0]


def test_incomplete_stream_reports_reason() -> None:
    final = response(status="incomplete", reason="max_output_tokens")
    adapter, _ = make([delta("Hel"), SimpleNamespace(type="response.incomplete", response=final)])
    assert adapter.generate(HELLO, on_text=lambda _: None).incomplete_reason == "max_output_tokens"


def test_error_event_raises_llm_error() -> None:
    adapter, _ = make([delta("Hel"), SimpleNamespace(type="error", code="server_error", message="boom")])
    with pytest.raises(LLMError, match="server_error"):
        adapter.generate(HELLO, on_text=lambda _: None)


def test_failed_response_raises_llm_error() -> None:
    failed = response(status="failed")
    failed.error = SimpleNamespace(code="rate_limit_exceeded")
    adapter, _ = make([SimpleNamespace(type="response.failed", response=failed)])
    with pytest.raises(LLMError, match="rate_limit_exceeded"):
        adapter.generate(HELLO, on_text=lambda _: None)


def test_connection_drop_mid_stream_gives_clear_error() -> None:
    drop = openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    adapter, fake = make([delta("Hel"), drop])
    with pytest.raises(LLMError, match="Could not reach OpenAI"):
        adapter.generate(HELLO, on_text=lambda _: None)
    assert fake.stream.closed


def test_stream_ending_without_final_event_is_an_error() -> None:
    adapter, _ = make([delta("Hel")])
    with pytest.raises(LLMError, match="ended unexpectedly"):
        adapter.generate(HELLO, on_text=lambda _: None)
