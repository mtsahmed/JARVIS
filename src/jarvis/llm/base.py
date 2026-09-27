"""Provider-neutral LLM interface. Core code depends only on these types."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str  # model name reported by the provider
    usage: Usage | None
    # Set when the provider stopped early, e.g. "max_output_tokens".
    incomplete_reason: str | None = None
    tool_calls: int = 0  # how many tool calls the model made while producing this reply


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call, described without any provider format."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object: every property required, no additional properties


# Runs one tool call: (tool name, arguments as a JSON string) -> result text for the model. Must not raise.
ToolRunner = Callable[[str, str], str]


class LLMError(Exception):
    """A provider call failed. The message is safe to show the user (no secrets)."""


class LLMAdapter(Protocol):
    @property
    def model(self) -> str: ...

    def generate(
        self,
        messages: Sequence[Message],
        instructions: str | None = None,
        on_text: Callable[[str], None] | None = None,
        tools: Sequence[ToolSpec] = (),
        run_tool: ToolRunner | None = None,
    ) -> LLMReply:
        """Send the conversation and return the full reply. Raises LLMError on failure.

        If `on_text` is given, the reply is streamed and each text piece is passed to it as it arrives.
        If `tools` and `run_tool` are given, the model may call tools; each call is executed with
        `run_tool` and its result sent back, until the model answers (a few rounds at most).
        """
        ...


class Embedder(Protocol):
    """Turns text into vectors for semantic similarity (memory recall)."""

    @property
    def model(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order. Raises LLMError on failure."""
        ...
