"""OpenAI adapter using the Responses API. The only module that imports `openai`."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import openai
from openai import OpenAI
from openai.types.responses import Response, ResponseUsage

from jarvis.llm.base import LLMError, LLMReply, Message, ToolRunner, ToolSpec, Usage

logger = logging.getLogger(__name__)

# Tool-call rounds per reply; the last round forbids tools so the model must answer.
MAX_TOOL_ROUNDS = 4


class OpenAIAdapter:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_output_tokens: int,
        timeout_seconds: float,
        reasoning_effort: str | None = None,
        max_retries: int = 1,
    ) -> None:
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._reasoning_effort = reasoning_effort
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=max_retries)

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        messages: Sequence[Message],
        instructions: str | None = None,
        on_text: Callable[[str], None] | None = None,
        tools: Sequence[ToolSpec] = (),
        run_tool: ToolRunner | None = None,
    ) -> LLMReply:
        base: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "max_output_tokens": self._max_output_tokens,
            "store": False,  # do not keep conversations on OpenAI's servers
        }
        if self._reasoning_effort is not None:
            base["reasoning"] = {"effort": self._reasoning_effort}
        use_tools = bool(tools) and run_tool is not None
        if use_tools:
            base["tools"] = [_to_openai_tool(t) for t in tools]
            # With store=False, reasoning items must be sent back (encrypted) alongside tool results.
            base["include"] = ["reasoning.encrypted_content"]

        items: list[Any] = [{"role": m.role, "content": m.content} for m in messages]
        usages: list[ResponseUsage | None] = []
        tool_calls = 0
        try:
            for round_number in range(MAX_TOOL_ROUNDS + 1):
                request = {**base, "input": items}
                if use_tools and round_number == MAX_TOOL_ROUNDS:
                    request["tool_choice"] = "none"  # enough tool calls: answer now
                response = self._client.responses.create(**request) if on_text is None else self._stream(request, on_text)
                usages.append(response.usage)
                calls = [item for item in response.output if item.type == "function_call"]
                if not (use_tools and calls and response.status == "completed"):
                    break
                # Function-calling guide: pass back the model's output items (reasoning + calls), then the results.
                items += response.output
                for call in calls:
                    tool_calls += 1
                    items.append({"type": "function_call_output", "call_id": call.call_id,
                                  "output": run_tool(call.name, call.arguments)})
        # Provider error text is not passed on (it can echo parts of the key); "from None" drops it from tracebacks.
        except openai.APITimeoutError:
            logger.warning("OpenAI request timed out after %ss", self._timeout_seconds)
            raise LLMError(f"OpenAI did not answer within {self._timeout_seconds:g}s. Try again.") from None
        except openai.APIConnectionError:
            logger.warning("OpenAI connection error")
            raise LLMError("Could not reach OpenAI. Check your internet connection.") from None
        except openai.AuthenticationError:
            logger.error("OpenAI authentication failed (401)")
            raise LLMError("OpenAI rejected the API key (401). Check OPENAI_API_KEY in .env.") from None
        except openai.RateLimitError:
            logger.warning("OpenAI rate limit / quota (429)")
            raise LLMError("OpenAI rate limit or quota reached (429). Check usage and billing.") from None
        except openai.APIStatusError as e:
            logger.error("OpenAI HTTP %s, request id %s", e.status_code, e.request_id)
            raise LLMError(f"OpenAI returned an error (HTTP {e.status_code}).") from None
        except openai.OpenAIError as e:
            logger.error("OpenAI client error: %s", type(e).__name__)
            raise LLMError(f"OpenAI request failed ({type(e).__name__}).") from None

        if response.status == "failed":
            code = response.error.code if response.error else "unknown"
            logger.error("OpenAI response failed: %s", code)
            raise LLMError(f"OpenAI could not complete the reply ({code}).")

        incomplete_reason = None
        if response.status == "incomplete":
            details = response.incomplete_details
            incomplete_reason = (details.reason if details else None) or "unknown"
            logger.warning("OpenAI response incomplete: %s", incomplete_reason)

        return LLMReply(
            text=response.output_text,
            model=response.model,
            usage=_sum_usage(usages),
            incomplete_reason=incomplete_reason,
            tool_calls=tool_calls,
        )


    def _stream(self, request: dict[str, Any], on_text: Callable[[str], None]) -> Response:
        """Stream the reply, passing text deltas to on_text; return the final Response."""
        with self._client.responses.create(**request, stream=True) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    on_text(event.delta)
                elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                    return event.response
                elif event.type == "error":
                    logger.error("OpenAI stream error: %s", event.code)
                    raise LLMError(f"OpenAI stopped the reply ({event.code or 'stream error'}).")
        logger.error("OpenAI stream ended without a final response")
        raise LLMError("OpenAI's reply ended unexpectedly. Try again.")


def _to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    # strict=True: the model's arguments always match the schema (all fields required, no extras).
    return {"type": "function", "name": tool.name, "description": tool.description,
            "parameters": tool.parameters, "strict": True}


def _sum_usage(usages: Sequence[ResponseUsage | None]) -> Usage | None:
    """Total usage over all tool-call rounds of one reply."""
    parts = [u for u in (_to_usage(x) for x in usages) if u is not None]
    if not parts:
        return None
    return Usage(*(sum(getattr(p, f) for p in parts) for f in
                   ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")))


def _to_usage(usage: ResponseUsage | None) -> Usage | None:
    if usage is None:
        return None
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_tokens=usage.input_tokens_details.cached_tokens if usage.input_tokens_details else 0,
        reasoning_tokens=usage.output_tokens_details.reasoning_tokens if usage.output_tokens_details else 0,
    )
