"""CLI wiring (Phase 6D coverage): which tools exist and at what tier, text chat loop, alerts, factories."""

import builtins
import json
import queue
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import jarvis.cli as cli
from jarvis.core.chat import ChatSession
from jarvis.core.config import load_config
from jarvis.llm.base import LLMError, LLMReply
from jarvis.memory.store import MemoryStore
from jarvis.tools.base import Tier
from jarvis.tools.briefing import Briefer, Briefing
from jarvis.tools.reminders import Reminder, ReminderStore
from jarvis.tools.web_search import Sources

ENV = ("OPENAI_API_KEY=sk-test\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\nJARVIS_MAX_OUTPUT_TOKENS=100\n"
       "JARVIS_LLM_TIMEOUT_SECONDS=10\nJARVIS_HISTORY_TURNS=3\n")


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from jarvis.core.config import _KEYS

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    return load_config(project_root=tmp_path)


class FakeLLM:
    def __init__(self, reply: str = "Hello.", fail: bool = False) -> None:
        self.reply, self.fail = reply, fail

    @property
    def model(self) -> str:
        return "fake"

    def generate(self, messages, instructions=None, on_text=None, tools=(), run_tool=None):  # type: ignore[no-untyped-def]
        if self.fail:
            raise LLMError("OpenAI did not answer.")
        return LLMReply(text=self.reply, model="fake", usage=None)


class FakeSearch:
    name = "fake"

    def search(self, query: str, max_results: int, topic: str = "general") -> Any:
        raise AssertionError("not called")


def services(tmp_path: Path, with_files: bool = True, with_search: bool = True) -> cli.Services:
    from jarvis.tools.files import FileIndex

    (tmp_path / "Files").mkdir(exist_ok=True)
    store = ReminderStore(tmp_path / "jarvis.db")
    return cli.Services(store, FileIndex(tmp_path / "files.db", (tmp_path / "Files",)) if with_files else None,
                        FakeSearch() if with_search else None, Sources(), MemoryStore(tmp_path / "memory.db"),
                        Briefer(None, store, FakeLLM()))


# --- the tool inventory: a security regression test ------------------------------------------------

EXPECTED_TIERS = {
    "create_reminder": Tier.AUTO, "list_reminders": Tier.AUTO, "cancel_reminder": Tier.CONFIRM,
    "remember": Tier.AUTO, "list_memories": Tier.AUTO, "forget_memory": Tier.CONFIRM,
    "morning_briefing": Tier.AUTO, "search_files": Tier.AUTO, "open_file": Tier.CONFIRM, "read_file": Tier.CONFIRM,
    "web_search": Tier.AUTO,
}


def test_exact_tool_inventory_and_tiers(tmp_path: Path) -> None:
    tools = services(tmp_path).build_tools()._tools
    assert {name: tool.tier for name, tool in tools.items()} == EXPECTED_TIERS  # nothing added silently


def test_injection_guards_are_wired(tmp_path: Path) -> None:
    tools = services(tmp_path).build_tools()._tools
    assert {n for n, t in tools.items() if t.untrusted_output} == {"read_file", "web_search", "morning_briefing"}
    assert {n for n, t in tools.items() if t.confirm_after_untrusted} == {"web_search", "create_reminder"}
    assert tools["remember"].check is not None and tools["remember"].private_args == {"fact"}
    assert tools["open_file"].check is not None and tools["read_file"].check is not None
    assert tools["open_file"].remember_key and tools["read_file"].remember_key  # once per file
    assert tools["cancel_reminder"].remember_key is None and tools["forget_memory"].remember_key is None


def test_optional_tools_absent_when_not_configured(tmp_path: Path) -> None:
    names = set(services(tmp_path, with_files=False, with_search=False).build_tools()._tools)
    assert names == set(EXPECTED_TIERS) - {"search_files", "open_file", "read_file", "web_search"}


def test_services_close_releases_databases(tmp_path: Path) -> None:
    svc = services(tmp_path)
    svc.close()
    assert svc.store._db is None and svc.memory._db is None and svc.files._db is None  # type: ignore[union-attr]


# --- text mode ---------------------------------------------------------------------------------------


def feed(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    answers = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)


@pytest.mark.parametrize(("typed", "approved"), [("y", True), ("YES", True), ("", False), ("no", False), (None, False)])
def test_text_confirmer_only_explicit_yes(monkeypatch: pytest.MonkeyPatch, typed: str | None, approved: bool) -> None:
    from jarvis.core.inbox import Inbox

    feed(monkeypatch, *([] if typed is None else [typed]))  # None = end of input
    inbox = Inbox()
    assert cli.TextConfirmer(inbox, cli.TerminalReader(inbox)).confirm("Open x.pdf?") is approved


def test_text_loop_commands_errors_and_sources(config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    from jarvis.tools.search.base import SearchResult

    sources = Sources()
    session = ChatSession(FakeLLM("Hi there\x1b[2J."), 3, usage_sink=lambda r: None)
    sources.add((SearchResult("A title", "https://a.example", "x"),))
    feed(monkeypatch, "hello", "/reset", "/help", "", "/exit")
    cli.run_text(config, session, sources)
    out = capsys.readouterr().out
    assert "jarvis> Hi there[2J." in out and "\x1b" not in out  # control characters stripped
    assert "Sources:\n  1. A title - https://a.example" in out and "(history cleared)" in out and "Commands:" in out

    feed(monkeypatch, "hello")
    cli.run_text(config, ChatSession(FakeLLM(fail=True), 3, usage_sink=lambda r: None), Sources())
    assert "[error] OpenAI did not answer." in capsys.readouterr().out  # an LLM failure does not crash the loop


# --- alerts ----------------------------------------------------------------------------------------


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))


def test_alerts_text_and_voice(capsys: pytest.CaptureFixture[str]) -> None:
    alerts = cli.ReminderAlerts()
    alerts._notifier = Notes()
    due = datetime.now(timezone.utc).astimezone() - timedelta(hours=2)
    alerts(Reminder(1, "take pills", due), missed=True)
    assert "Missed reminder from" in capsys.readouterr().out and alerts._notifier.sent[0][0] == "Jarvis reminder"

    alerts.voice_queue = queue.Queue()
    alerts(Reminder(2, "stretch", due), missed=False)
    alerts.briefing(Briefing("Good morning.", "Important unread emails:\n  (none)"))
    assert alerts.voice_queue.get_nowait() == "Reminder: stretch"
    assert alerts.voice_queue.get_nowait().spoken == "Good morning."
    assert alerts._notifier.sent[-1] == ("Jarvis briefing", "Your morning briefing is ready.")


# --- main entry points, config-dependent helpers, factories ------------------------------------------


def test_main_secrets_status(config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["jarvis", "--secrets", "status"])
    assert cli.main() == 0
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_main_config_error_exits_cleanly(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jarvis.core.config import ConfigError

    def broken() -> None:
        raise ConfigError("Missing required setting JARVIS_LLM_MODEL.")

    monkeypatch.setattr(cli, "load_config", broken)
    monkeypatch.setattr("sys.argv", ["jarvis"])
    assert cli.main() == 1 and "Config error" in capsys.readouterr().err


def test_gmail_client_only_when_set_up(config, isolated_secret_store) -> None:  # type: ignore[no-untyped-def]
    assert cli.gmail_client(config) is None
    isolated_secret_store.set("GOOGLE_CLIENT_ID", "cid")
    assert cli.gmail_client(config) is not None


def test_factories(config) -> None:  # type: ignore[no-untyped-def]
    from jarvis.llm.factory import create_embedder, create_llm
    from jarvis.tools.search.base import SearchError
    from jarvis.tools.search.factory import create_search

    assert create_llm(config).model == "m"
    assert create_embedder(config) is None  # no embedding model configured
    assert create_embedder(replace(config, embedding_model="text-embedding-3-small")).model == "text-embedding-3-small"
    assert create_search("tavily", "tvly-x").name == "tavily"
    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        create_search("tavily", None)
    with pytest.raises(SearchError, match="No adapter"):
        create_search("other", "k")
    with pytest.raises(LLMError, match="No adapter"):
        create_llm(replace(config, llm_provider="other"))
