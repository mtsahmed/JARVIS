"""Build the web search adapter selected in config."""

from __future__ import annotations

from jarvis.tools.search.base import SearchAdapter, SearchError


def create_search(provider: str, api_key: str | None) -> SearchAdapter:
    if provider == "tavily":
        from jarvis.tools.search.tavily_adapter import TavilyAdapter

        if not api_key:
            raise SearchError("Tavily needs TAVILY_API_KEY in .env.")
        return TavilyAdapter(api_key)
    raise SearchError(f"No adapter for search provider {provider!r}.")
