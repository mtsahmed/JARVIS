"""Multi-turn chat session: keeps trimmed history and logs usage per request."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from jarvis.core.logging_setup import log_usage
from jarvis.llm.base import LLMAdapter, LLMError, LLMReply, Message
from jarvis.tools.base import Confirmer
from jarvis.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_INSTRUCTIONS = (
    "You are Jarvis, a helpful personal assistant. "
    "Keep replies short, clear, and easy to read aloud. "
    "When an action needs the user's approval, just call the tool: Jarvis asks the user itself, "
    "so never ask for confirmation in your reply."
)


class ChatSession:
    def __init__(
        self,
        llm: LLMAdapter,
        history_turns: int,
        instructions: str = DEFAULT_INSTRUCTIONS,
        usage_sink: Callable[[dict[str, Any]], None] = log_usage,
        tools: ToolRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._llm = llm
        self._max_messages = history_turns * 2  # one turn = user message + assistant reply
        self._instructions = instructions
        self._usage_sink = usage_sink
        self._tools = tools
        self._clock = clock
        self._history: list[Message] = []

    @property
    def history(self) -> tuple[Message, ...]:
        return tuple(self._history)

    def reset(self) -> None:
        self._history.clear()
        logger.info("Chat history cleared")

    def ask(
        self,
        text: str,
        on_text: Callable[[str], None] | None = None,
        confirmer: Confirmer | None = None,
    ) -> LLMReply:
        """Send one user message with recent history. Raises LLMError on provider failure.

        `on_text` (optional) receives the reply in pieces as it streams in.
        `confirmer` asks the user before CONFIRM-tier tools run (without one, they are declined).
        """
        messages = [*self._history[-self._max_messages :], Message("user", text)]
        # The model has no clock; it needs "now" to resolve "tomorrow at 6" and similar.
        now = self._clock()
        instructions = f"{self._instructions}\n\nCurrent local date and time: {now:%A, %d %B %Y, %H:%M} (UTC{now:%z})."
        tools = self._tools
        started = time.perf_counter()
        try:
            reply = self._llm.generate(
                messages,
                instructions,
                on_text=on_text,
                tools=tools.specs() if tools else (),
                run_tool=(lambda name, args: tools.run(name, args, confirmer)) if tools else None,
            )
        except LLMError:
            self._record_usage(None, started, status="error")
            raise

        self._record_usage(reply, started, status="incomplete" if reply.incomplete_reason else "ok")
        # Keep history only for turns with a visible answer, so roles stay user/assistant pairs.
        if reply.text:
            self._history += [Message("user", text), Message("assistant", reply.text)]
            del self._history[: -self._max_messages]
        return reply

    def _record_usage(self, reply: LLMReply | None, started: float, status: str) -> None:
        """Write one usage line. Message content is never logged."""
        usage = reply.usage if reply else None
        self._usage_sink(
            {
                "kind": "llm",
                "model": reply.model if reply else self._llm.model,
                "status": status,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "reasoning_tokens": usage.reasoning_tokens if usage else 0,
                "cached_tokens": usage.cached_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
                "tool_calls": reply.tool_calls if reply else 0,
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }
        )
