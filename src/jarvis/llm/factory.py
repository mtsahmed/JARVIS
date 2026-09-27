"""Build the LLM adapter selected in config."""

from __future__ import annotations

from jarvis.core.config import Config
from jarvis.llm.base import Embedder, LLMAdapter, LLMError


def create_llm(config: Config) -> LLMAdapter:
    if config.llm_provider == "openai":
        # Imported here so other providers never need the openai package loaded.
        from jarvis.llm.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(
            api_key=config.llm_api_key,
            model=config.llm_model,
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=config.llm_timeout_seconds,
            reasoning_effort=config.reasoning_effort,
        )
    raise LLMError(f"No adapter for LLM provider {config.llm_provider!r}.")


def create_embedder(config: Config) -> Embedder | None:
    """The embedding adapter for memory recall, or None if JARVIS_EMBEDDING_MODEL is blank."""
    if not config.embedding_model:
        return None
    if config.llm_provider == "openai":
        from jarvis.llm.openai_embeddings import OpenAIEmbedder

        return OpenAIEmbedder(config.llm_api_key, config.embedding_model)
    raise LLMError(f"No embedding adapter for provider {config.llm_provider!r}.")
