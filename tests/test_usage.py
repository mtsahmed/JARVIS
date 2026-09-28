"""Phase 7D: estimated OpenAI spend, the running monthly total, budget alerts, free allowances."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import PROJECT_ROOT
from jarvis.core.usage_report import CostMeter, Prices, allowance_warnings, cost_of, report

PRICES = Prices.load(PROJECT_ROOT / "config" / "prices.json")
TZ = timezone(timedelta(hours=3))


class Clock:
    def __init__(self, when: datetime) -> None:
        self.now = when

    def __call__(self) -> datetime:
        return self.now


def llm(input_tokens: int, output_tokens: int, cached: int = 0, model: str = "gpt-5-mini-2025-08-07") -> dict[str, Any]:
    return {"kind": "llm", "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_tokens": cached, "status": "ok"}


# --- prices and costs -------------------------------------------------------------------------------------


def test_prices_file_has_source_and_the_models_in_use() -> None:
    assert "developers.openai.com/api/docs/pricing" in PRICES.source
    assert PRICES.llm["gpt-5-mini"] == {"input": 0.25, "cached_input": 0.025, "output": 2.00}
    assert PRICES.transcription_per_minute["gpt-4o-mini-transcribe"] == 0.003


def test_costs() -> None:
    assert cost_of(llm(1000, 200), PRICES) == (pytest.approx(0.00065), None)  # dated snapshot -> gpt-5-mini
    assert cost_of(llm(1000, 0, cached=1000), PRICES)[0] == pytest.approx(0.000025)  # cached input is cheaper
    assert cost_of({"kind": "stt", "model": "gpt-4o-mini-transcribe", "audio_seconds": 30}, PRICES)[0] == pytest.approx(0.0015)
    assert cost_of({"kind": "stt", "model": "base.en", "audio_seconds": 30}, PRICES) == (0.0, None)  # local: free
    assert cost_of({"kind": "embedding", "model": "text-embedding-3-small", "total_tokens": 1_000_000}, PRICES)[0] == pytest.approx(0.02)
    assert cost_of({"kind": "search", "credits": 1}, PRICES) == (0.0, None)


def test_unknown_models_are_flagged_not_silently_free() -> None:
    assert cost_of(llm(10, 10, model="gpt-9"), PRICES) == (0.0, "gpt-9")
    assert cost_of({"kind": "embedding", "model": "new-embedder", "total_tokens": 5}, PRICES) == (0.0, "new-embedder")


# --- the running monthly total --------------------------------------------------------------------------


def write_log(path: Path, records: list[tuple[str, dict[str, Any]]]) -> Path:
    path.write_text("\n".join(json.dumps({"ts": ts, **r}) for ts, r in records) + "\nnot json\n", encoding="utf-8")
    return path


def meter(tmp_path: Path, clock: Clock, alerts: list[str], logs: list[Path] | None = None, budget: float = 5.0) -> CostMeter:
    return CostMeter(tmp_path / "jarvis.db", PRICES, budget, 80, alerts.append, usage_logs=logs, clock=clock)


def test_backfills_this_month_once_then_keeps_a_running_total(tmp_path: Path) -> None:
    clock, alerts = Clock(datetime(2026, 9, 28, 9, 0, tzinfo=TZ)), []
    log = write_log(tmp_path / "usage.jsonl", [("2026-08-31T23:00:00+00:00", llm(1_000_000, 0)),  # last month
                                               ("2026-09-02T10:00:00+00:00", llm(1_000_000, 0))])  # $0.25
    m = meter(tmp_path, clock, alerts, [log])
    assert m.month_total()[0] == pytest.approx(0.25)
    m.observe(llm(0, 1_000_000))  # +$2.00
    m.close()
    again = meter(tmp_path, clock, alerts, [log])  # restart: no double counting of the log
    assert again.month_total()[0] == pytest.approx(2.25)
    again.close()


def test_unpriced_models_are_listed(tmp_path: Path) -> None:
    m = meter(tmp_path, Clock(datetime(2026, 9, 28, tzinfo=TZ)), [])
    m.observe(llm(5, 5, model="gpt-9"))
    assert m.month_total()[1] == ["gpt-9"]
    m.close()


def test_budget_warns_at_80_once_a_day_and_at_once_at_100(tmp_path: Path) -> None:
    clock, alerts = Clock(datetime(2026, 9, 28, 9, 0, tzinfo=TZ)), []
    m = meter(tmp_path, clock, alerts, budget=1.0)
    m.observe(llm(0, 300_000))  # $0.60: below 80%
    assert alerts == []
    m.observe(llm(0, 100_000))  # $0.80: 80%
    assert alerts == ["estimated spending this month is $0.80, 80% of your $1.00 budget."]
    m.observe(llm(0, 10))
    m.check_budget()
    assert len(alerts) == 1  # not again the same day
    clock.now += timedelta(days=1)
    m.check_budget()
    assert len(alerts) == 2  # once a day while over the warning level
    m.observe(llm(0, 100_000))  # $1.00: 100% -> at once, the same day
    assert len(alerts) == 3 and "100% of your $1.00 budget" in alerts[-1]
    clock.now = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    m.check_budget()
    assert len(alerts) == 3 and m.month_total()[0] == 0.0  # new month starts from zero
    m.close()


# --- free allowances and the report ---------------------------------------------------------------------


def test_allowance_warnings() -> None:
    def unreachable() -> tuple[int, int]:
        raise OSError("offline")

    warnings = allowance_warnings([("Premium voice characters", lambda: (117_370, 131_000)),
                                   ("Web search credits", lambda: (15, 1000)),
                                   ("Unlimited", lambda: (5, None)), ("Broken", unreachable)], 80)
    assert warnings == ["Premium voice characters: 117,370 of 131,000 used this month (90%)."]


def test_report(tmp_path: Path) -> None:
    m = meter(tmp_path, Clock(datetime(2026, 9, 28, tzinfo=TZ)), [])
    m.observe(llm(0, 36_400))  # about $0.07
    text = report(m, 5.0, [("Web search credits (Tavily)", lambda: (15, 1000))], "Hard cap: $20.")
    assert "OpenAI (estimated): $0.0728 of $5.00 budget (1.5%)" in text
    assert "Web search credits (Tavily): 15 of 1,000 used" in text and text.endswith("Hard cap: $20.")
    m.close()


def test_elevenlabs_quota_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.voice.tts.elevenlabs_adapter as el

    adapter = el.ElevenLabsAdapter("k", "v", "m")
    monkeypatch.setattr(adapter, "_request", lambda method, path: json.dumps(
        {"character_count": 117370, "character_limit": 131000}).encode())
    assert adapter.quota() == (117370, 131000) and adapter.characters_left() == 13630


def test_budget_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.config import _KEYS, ConfigError, load_config

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    base = ("OPENAI_API_KEY=sk-x\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\nJARVIS_MAX_OUTPUT_TOKENS=1\n"
            "JARVIS_LLM_TIMEOUT_SECONDS=1\nJARVIS_HISTORY_TURNS=1\n")
    (tmp_path / ".env").write_text(base, encoding="utf-8")
    config = load_config(project_root=tmp_path)
    assert (config.monthly_budget_usd, config.budget_warn_percent) == (5.0, 80.0)
    (tmp_path / ".env").write_text(base + "JARVIS_MONTHLY_BUDGET_USD=-3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="JARVIS_MONTHLY_BUDGET_USD"):
        load_config(project_root=tmp_path)
