"""OpenAI embeddings (for memory recall). Uses client.embeddings.create(model, input) -> data[i].embedding."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import openai
from openai import OpenAI

from jarvis.core.logging_setup import log_usage
from jarvis.llm.base import LLMError

logger = logging.getLogger(__name__)


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, timeout_seconds: float = 10.0,
                 usage_sink: Callable[[dict[str, Any]], None] = log_usage) -> None:
        self._model = model
        self._usage_sink = usage_sink
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=1)

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(model=self._model, input=list(texts))
        # Provider error text is not passed on (it can echo parts of the key).
        except openai.APIConnectionError:  # includes timeouts
            logger.warning("OpenAI embeddings: connection problem")
            raise LLMError("Could not reach OpenAI for memory recall.") from None
        except openai.APIStatusError as e:
            logger.error("OpenAI embeddings HTTP %s", e.status_code)
            raise LLMError(f"Memory recall failed (HTTP {e.status_code}).") from None
        except openai.OpenAIError as e:
            logger.error("OpenAI embeddings error: %s", type(e).__name__)
            raise LLMError("Memory recall failed.") from None
        self._usage_sink({"kind": "embedding", "model": self._model, "inputs": len(texts),
                          "total_tokens": response.usage.total_tokens})
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
