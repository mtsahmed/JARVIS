"""OpenAI adapter using the Responses API. The only module that imports `openai`."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import openai
from openai import OpenAI
from openai.types.responses import ResponseUsage

from jarvis.llm.base import LLMError, LLMReply, Message, Usage

logger = logging.getLogger(__name__)


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

    def generate(self, messages: Sequence[Message], instructions: str | None = None) -> LLMReply:
        extra: dict[str, Any] = {}
        if self._reasoning_effort is not None:
            extra["reasoning"] = {"effort": self._reasoning_effort}
        try:
            response = self._client.responses.create(
                model=self._model,
                instructions=instructions,
                input=[{"role": m.role, "content": m.content} for m in messages],
                max_output_tokens=self._max_output_tokens,
                store=False,  # do not keep conversations on OpenAI's servers
                **extra,
            )
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
            usage=_to_usage(response.usage),
            incomplete_reason=incomplete_reason,
        )


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
