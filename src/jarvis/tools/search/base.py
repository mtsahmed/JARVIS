"""Provider-neutral web search interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Topic = Literal["general", "news"]


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchResponse:
    results: tuple[SearchResult, ...]
    credits_used: int | None = None  # as reported by the provider, if it reports it


class SearchError(Exception):
    """A search failed. The message is safe to show the user (no secrets)."""


class SearchAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def search(self, query: str, max_results: int, topic: Topic = "general") -> SearchResponse:
        """Search the web. Raises SearchError on failure."""
        ...
