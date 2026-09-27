"""Tavily Search API over stdlib HTTP (no SDK). The only module that talks to Tavily.

API reference (docs.tavily.com): POST https://api.tavily.com/search, header
"Authorization: Bearer <key>", body {query, search_depth, max_results, topic};
response {results: [{title, url, content, score}], usage: {credits} (only with include_usage=true)}.
"basic" depth costs 1 credit; the free plan has 1,000 credits per month.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from jarvis.tools.search.base import SearchError, SearchResponse, SearchResult, Topic

logger = logging.getLogger(__name__)

API_URL = "https://api.tavily.com/search"
MAX_SNIPPET_CHARS = 400

_STATUS_MESSAGES = {
    401: "Tavily rejected the API key (401). Check TAVILY_API_KEY in .env.",
    429: "Tavily says too many requests (429). Try again in a moment.",
    432: "The Tavily plan's monthly search limit is reached (432).",
    433: "The Tavily pay-as-you-go limit is reached (433).",
}


class TavilyAdapter:
    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "tavily"

    def search(self, query: str, max_results: int, topic: Topic = "general") -> SearchResponse:
        body = {"query": query, "search_depth": "basic", "max_results": max_results, "topic": topic,
                "include_usage": True}  # docs: needed for the response to report credits used
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data: dict[str, Any] = json.loads(response.read())
        # Error bodies are not passed on; "from None" keeps them (and headers) out of tracebacks.
        except urllib.error.HTTPError as e:
            e.close()
            logger.warning("Tavily search -> HTTP %s", e.code)
            raise SearchError(_STATUS_MESSAGES.get(e.code, f"Tavily search failed (HTTP {e.code}).")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("Tavily connection problem: %s", type(e).__name__)
            raise SearchError("Could not reach the web search service. Check your internet connection.") from None
        except ValueError:
            raise SearchError("Unexpected response from the web search service.") from None

        results = tuple(
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=" ".join(str(item.get("content") or "").split())[:MAX_SNIPPET_CHARS],
            )
            for item in data.get("results", [])
            if item.get("url")
        )
        credits = (data.get("usage") or {}).get("credits")
        return SearchResponse(results, int(credits) if isinstance(credits, (int, float)) else None)
