"""Web search: Tavily adapter over faked HTTP, the web_search tool, sources, and config."""

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import ConfigError, load_config
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.search.base import SearchError, SearchResponse, SearchResult
from jarvis.tools.search.tavily_adapter import API_URL, TavilyAdapter
from jarvis.tools.web_search import Sources, format_sources, web_search_tool


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


TAVILY_BODY = {
    "query": "apple market cap",
    "results": [
        {"title": "Apple market cap", "url": "https://companiesmarketcap.com/apple/", "content": "  Apple is worth\n $3.8T ", "score": 0.9},
        {"title": "No URL", "url": "", "content": "dropped"},
    ],
    "usage": {"credits": 1},
}


# --- Tavily adapter ------------------------------------------------------------------------------


def test_tavily_request_and_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[urllib.request.Request] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: sent.append(req) or FakeResponse(TAVILY_BODY))
    response = TavilyAdapter("tvly-secret").search("apple market cap", 5, "news")

    req = sent[0]
    assert req.full_url == API_URL and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tvly-secret"
    assert json.loads(req.data) == {"query": "apple market cap", "search_depth": "basic", "max_results": 5,
                                    "topic": "news", "include_usage": True}
    assert response.results == (SearchResult("Apple market cap", "https://companiesmarketcap.com/apple/", "Apple is worth $3.8T"),)
    assert response.credits_used == 1


@pytest.mark.parametrize(("code", "message"), [(401, "rejected the API key"), (432, "monthly search limit"), (500, "HTTP 500")])
def test_tavily_errors_are_clear_and_leak_nothing(monkeypatch: pytest.MonkeyPatch, code: int, message: str) -> None:
    def fail(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(req.full_url, code, "err", None, io.BytesIO(b'{"detail": "key tvly-secret"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(SearchError, match=message) as exc:
        TavilyAdapter("tvly-secret").search("x", 5)
    assert "tvly-secret" not in str(exc.value) and exc.value.__cause__ is None


def test_tavily_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(SearchError, match="Could not reach"):
        TavilyAdapter("k").search("x", 5)


# --- web_search tool ------------------------------------------------------------------------------


class FakeSearch:
    def __init__(self, results: tuple[SearchResult, ...] = (), error: str | None = None) -> None:
        self.results, self.error, self.calls = results, error, []

    @property
    def name(self) -> str:
        return "fake"

    def search(self, query: str, max_results: int, topic: str = "general") -> SearchResponse:
        self.calls.append((query, max_results, topic))
        if self.error:
            raise SearchError(self.error)
        return SearchResponse(self.results, credits_used=1)


RESULTS = (SearchResult("Weather Riyadh", "https://weather.example/riyadh", "Sunny, 38 C"),
           SearchResult("Riyadh forecast", "https://forecast.example/r", "Hot and dry"))


def setup_tool(search: FakeSearch) -> tuple[ToolRegistry, Sources, list[dict[str, Any]], list[dict[str, Any]]]:
    sources, usage, audit = Sources(), [], []
    registry = ToolRegistry([web_search_tool(search, sources, usage_sink=usage.append)], audit_sink=audit.append)
    return registry, sources, usage, audit


def test_search_tool_is_auto_and_marks_content_untrusted() -> None:
    search = FakeSearch(RESULTS)
    registry, sources, usage, audit = setup_tool(search)
    result = registry.run("web_search", json.dumps({"query": "weather Riyadh", "topic": "general"}))
    assert "Sunny, 38 C" in result and "untrusted web content" in result
    assert search.calls == [("weather Riyadh", 5, "general")]
    assert audit[0]["decision"] == "auto" and audit[0]["args"]["query"] == "weather Riyadh"
    assert usage == [{"kind": "search", "provider": "fake", "status": "ok", "results": 2, "credits": 1,
                      "latency_ms": usage[0]["latency_ms"]}]
    assert "weather Riyadh" not in json.dumps(usage)  # the query is only in the audit log
    assert [s.url for s in sources.take()] == ["https://weather.example/riyadh", "https://forecast.example/r"]
    assert sources.take() == []  # taken once


def test_search_failure_is_a_clear_tool_error() -> None:
    registry, sources, usage, _ = setup_tool(FakeSearch(error="Could not reach the web search service."))
    result = registry.run("web_search", json.dumps({"query": "news", "topic": "news"}))
    assert result == "Error: Could not reach the web search service."
    assert usage[0]["status"] == "error" and sources.take() == []


def test_sources_deduplicated_and_formatted() -> None:
    sources = Sources()
    sources.add(RESULTS)
    sources.add(RESULTS[:1])
    text = format_sources(sources.take())
    assert text == ("Sources:\n  1. Weather Riyadh - https://weather.example/riyadh\n"
                    "  2. Riyadh forecast - https://forecast.example/r")


# --- config ---------------------------------------------------------------------------------------

BASE_ENV = ("OPENAI_API_KEY=sk-test\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\nJARVIS_MAX_OUTPUT_TOKENS=100\n"
            "JARVIS_LLM_TIMEOUT_SECONDS=10\nJARVIS_HISTORY_TURNS=3\n")


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.config import _KEYS

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def load(tmp_path: Path, extra: str):  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text(BASE_ENV + extra, encoding="utf-8")
    return load_config(project_root=tmp_path)


def test_search_off_by_default(tmp_path: Path) -> None:
    config = load(tmp_path, "TAVILY_API_KEY=tvly-x\n")
    assert config.search_provider is None and config.search_api_key is None


def test_tavily_key_loaded_hidden_and_redacted(tmp_path: Path) -> None:
    config = load(tmp_path, "JARVIS_SEARCH_PROVIDER=tavily\nTAVILY_API_KEY=tvly-secret\n")
    assert config.search_provider == "tavily" and config.search_api_key == "tvly-secret"
    assert "tvly-secret" not in repr(config) and "tvly-secret" in config.secrets


def test_tavily_without_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TAVILY_API_KEY"):
        load(tmp_path, "JARVIS_SEARCH_PROVIDER=tavily\n")
