"""Provider-neutral LLM interface. Core code depends only on these types."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

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


class LLMError(Exception):
    """A provider call failed. The message is safe to show the user (no secrets)."""


class LLMAdapter(Protocol):
    @property
    def model(self) -> str: ...

    def generate(self, messages: Sequence[Message], instructions: str | None = None) -> LLMReply:
        """Send the conversation and return the reply. Raises LLMError on failure."""
        ...
