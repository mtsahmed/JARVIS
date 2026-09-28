"""Phase 7B: health checks, the startup check, and alerts on repeated failures."""

import io
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core import health
from jarvis.core.health import Check, FailureWatcher

TODAY = date(2026, 9, 28)


# --- the report --------------------------------------------------------------------------------------


def test_a_crashing_check_is_reported_not_raised() -> None:
    def boom() -> Check:
        raise RuntimeError("bug")

    results = health.run_checks([("Good", lambda: health.ok("Good", "fine")), ("Bad", boom)])
    assert [(c.name, c.status) for c in results] == [("Good", "ok"), ("Bad", "fail")]
    assert "could not run (RuntimeError)" in results[1].detail


def test_report_counts_problems() -> None:
    text = health.report([health.ok("A", "fine"), health.warn("B", "look"), health.fail("C", "broken")])
    assert "[OK  ] A" in text and "[WARN] B" in text and "[FAIL] C" in text and "1 problem(s) need attention." in text
    assert health.report([health.ok("A", "fine")]).endswith("All good.")


# --- individual checks ---------------------------------------------------------------------------------


def test_openai_check() -> None:
    from jarvis.llm.base import LLMError

    class Good:
        model = "gpt-5-mini"

        def ping(self) -> None:
            pass

    class Bad(Good):
        def ping(self) -> None:
            raise LLMError("OpenAI rejected the API key (401).")

    assert health.check_openai(Good()).status == "ok"
    assert health.check_openai(Bad()).detail == "OpenAI rejected the API key (401)."
    assert health.check_openai(object()).status == "warn"


def test_openai_ping_maps_errors() -> None:
    import httpx2
    import openai

    from jarvis.llm.base import LLMError
    from jarvis.llm.openai_adapter import OpenAIAdapter

    def missing(model: str) -> None:
        raise openai.NotFoundError("no", response=httpx2.Response(404, request=httpx2.Request("GET", "https://x")), body=None)

    adapter = OpenAIAdapter("sk-test", "gpt-9", max_output_tokens=1, timeout_seconds=1)
    adapter._client = SimpleNamespace(models=SimpleNamespace(retrieve=missing))  # type: ignore[assignment]
    with pytest.raises(LLMError, match="does not offer the model 'gpt-9'"):
        adapter.ping()


class Search:
    name = "tavily"

    def __init__(self, used: int = 15, limit: int | None = 1000, error: str | None = None) -> None:
        self.used, self.limit, self.error = used, limit, error

    def usage(self) -> tuple[int, int | None]:
        from jarvis.tools.search.base import SearchError

        if self.error:
            raise SearchError(self.error)
        return self.used, self.limit


def test_search_check() -> None:
    assert health.check_search(None).status == "warn"
    assert health.check_search(Search()).detail == "tavily: 15 of 1000 credits used this month"
    assert health.check_search(Search(used=1000)).status == "fail"
    assert health.check_search(Search(error="Tavily rejected the API key (401).")).status == "fail"


def test_tavily_usage_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.tools.search.tavily_adapter import USAGE_URL, TavilyAdapter

    body = json.dumps({"key": {"usage": 15, "limit": None}, "account": {"plan_usage": 15, "plan_limit": 1000}}).encode()
    sent: list[urllib.request.Request] = []

    class Response(io.BytesIO):
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Response(body))
    assert TavilyAdapter("tvly-k").usage() == (15, 1000)
    assert sent[0].full_url == USAGE_URL and sent[0].get_header("Authorization") == "Bearer tvly-k"


def test_elevenlabs_check() -> None:
    from jarvis.voice.tts.base import TTSError

    def unreachable() -> int:
        raise TTSError("Could not reach ElevenLabs.")

    assert health.check_elevenlabs(None).status == "warn"
    assert health.check_elevenlabs(lambda: 13630).detail == "ElevenLabs: 13,630 characters left this month"
    assert health.check_elevenlabs(lambda: 400).status == "warn"
    assert health.check_elevenlabs(unreachable).status == "fail"


def test_gmail_check() -> None:
    from jarvis.integrations.gmail import GmailAuthError

    class Expired:
        def _credentials(self) -> None:
            raise GmailAuthError("Gmail sign-in has expired. Run: jarvis --gmail-login")

    assert health.check_gmail(None).status == "warn"
    assert health.check_gmail(SimpleNamespace(_credentials=lambda: object())).status == "ok"
    assert health.check_gmail(Expired()).detail == "Gmail sign-in has expired. Run: jarvis --gmail-login"


def test_disk_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: SimpleNamespace(free=1 * 1024**3))
    assert health.check_disk(tmp_path).status == "warn"
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: SimpleNamespace(free=14 * 1024**3))
    assert health.check_disk(tmp_path).detail == "14 GB free"


@pytest.mark.parametrize(("last", "expected", "status", "text"), [
    (None, False, "warn", "not scheduled"),
    (None, True, "warn", "has not run yet"),
    (TODAY, True, "ok", "last ran today"),
    (date(2026, 9, 27), True, "ok", "last ran yesterday"),
    (date(2026, 9, 25), True, "warn", "last ran 3 days ago"),
])
def test_daily_job_check(last: date | None, expected: bool, status: str, text: str) -> None:
    check = health.check_daily_job("morning_briefing", "Morning briefing", last, expected, TODAY)
    assert check.status == status and text in check.detail


def test_background_check_reports_only_failures() -> None:
    reported: list[list[Check]] = []
    thread = health.start_background_check(
        [("A", lambda: health.ok("A", "fine")), ("B", lambda: health.warn("B", "look")),
         ("C", lambda: health.fail("C", "broken"))], reported.append)
    thread.join(timeout=5)
    assert [[c.name for c in r] for r in reported] == [["C"]]


# --- repeated failures during use ------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_three_failures_in_a_row_alert_once_per_hour() -> None:
    alerts: list[str] = []
    clock = Clock()
    watcher = FailureWatcher(alerts.append, clock=clock)
    for _ in range(2):
        watcher.observe({"kind": "llm", "status": "error"})
    assert alerts == []
    watcher.observe({"kind": "llm", "status": "error"})
    assert alerts == ["the language service (OpenAI) has failed 3 times in a row. Run: jarvis --health"]
    watcher.observe({"kind": "llm", "status": "error"})
    assert len(alerts) == 1  # quiet for an hour
    clock.now += 3601
    watcher.observe({"kind": "llm", "status": "error"})
    assert len(alerts) == 2


def test_a_success_resets_the_count_and_other_records_are_ignored() -> None:
    alerts: list[str] = []
    watcher = FailureWatcher(alerts.append)
    for record in ({"kind": "search", "status": "error"}, {"kind": "search", "status": "error"},
                   {"kind": "search", "status": "ok"}, {"kind": "search", "status": "error"},
                   {"kind": "voice_turn", "status": "error"}, {"kind": "embedding"}):
        watcher.observe(record)
    assert alerts == []


def test_premium_voice_fallbacks_count_as_failures() -> None:
    alerts: list[str] = []
    watcher = FailureWatcher(alerts.append)
    for _ in range(3):
        watcher.observe({"kind": "tts", "provider": "sapi", "fallback": True})
    assert alerts and alerts[0].startswith("the premium voice has failed 3 times")


def test_usage_records_reach_observers_and_a_broken_observer_is_contained() -> None:
    from jarvis.core import logging_setup

    seen: list[dict[str, Any]] = []

    def broken(record: dict[str, Any]) -> None:
        raise RuntimeError("observer bug")

    saved = list(logging_setup._usage_observers)
    try:
        logging_setup.add_usage_observer(broken)
        logging_setup.add_usage_observer(seen.append)
        logging_setup.log_usage({"kind": "llm", "status": "ok"})  # must not raise
        assert seen[0]["kind"] == "llm" and "ts" in seen[0]
    finally:
        logging_setup._usage_observers[:] = saved


def test_alerts_problem_text_and_voice(capsys: pytest.CaptureFixture[str]) -> None:
    import queue

    import jarvis.cli as cli

    notes: list[tuple[str, str]] = []
    alerts = cli.ReminderAlerts()
    alerts._notifier = SimpleNamespace(notify=lambda t, m: notes.append((t, m)))
    alerts.problems([health.fail("Gmail", "sign-in expired"), health.fail("OpenAI", "down"), health.fail("X", "y")])
    assert "[attention] Gmail: sign-in expired" in capsys.readouterr().out
    assert [m for _, m in notes] == ["Gmail: sign-in expired", "OpenAI: down"]  # at most two at startup
    alerts.voice_queue = queue.Queue()
    alerts.problem("web search has failed 3 times in a row.")
    assert alerts.voice_queue.get_nowait() == "Heads up: web search has failed 3 times in a row."
