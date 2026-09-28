"""Monthly usage, estimated cost, and budget alerts (Phase 7D). Owner: $5/month total, warn at 80% and 100%.

- OpenAI spend is ESTIMATED from Jarvis's own usage records and config/prices.json (OpenAI's cost API
  needs an organization admin key, which Jarvis deliberately does not hold). A running monthly total is
  kept in data/jarvis.db so the estimate survives log rotation. The hard cap is the owner's OpenAI
  dashboard spending limit ($20).
- Tavily credits and ElevenLabs characters come straight from their own usage APIs (free allowances).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.db import ClosingDB

logger = logging.getLogger(__name__)

LEVELS = (100.0,)  # plus the configured warning percent (80)


# --- prices ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prices:
    llm: dict[str, dict[str, float]]  # model -> {"input", "cached_input"?, "output"} per 1M tokens
    transcription_per_minute: dict[str, float]
    embedding: dict[str, float]  # per 1M tokens
    source: str

    @classmethod
    def load(cls, path: Path) -> Prices:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("llm_per_million_tokens", {}), data.get("transcription_per_minute", {}),
                   data.get("embedding_per_million_tokens", {}), data.get("source", ""))


def _price_for(table: dict[str, Any], model: str) -> Any:
    """Exact model name, else the longest price key it starts with ('gpt-5-mini-2025-08-07' -> 'gpt-5-mini')."""
    if model in table:
        return table[model]
    matches = [key for key in table if model.startswith(key)]
    return table[max(matches, key=len)] if matches else None


def cost_of(record: dict[str, Any], prices: Prices) -> tuple[float, str | None]:
    """(estimated OpenAI cost in USD, name of an OpenAI model with no known price or None)."""
    kind, model = record.get("kind"), str(record.get("model") or "")
    if kind == "llm":
        price = _price_for(prices.llm, model)
        if price is None:
            return 0.0, model or "unknown chat model"
        cached = int(record.get("cached_tokens") or 0)
        fresh = max(int(record.get("input_tokens") or 0) - cached, 0)
        output = int(record.get("output_tokens") or 0)  # includes reasoning tokens
        cost = (fresh * price["input"] + cached * price.get("cached_input", price["input"]) + output * price["output"])
        return cost / 1_000_000, None
    if kind == "stt":
        per_minute = _price_for(prices.transcription_per_minute, model)
        # Models without a transcription price are the local ones (e.g. base.en): free.
        return (float(record.get("audio_seconds") or 0) / 60 * per_minute if per_minute else 0.0), None
    if kind == "embedding":
        price = _price_for(prices.embedding, model)
        if price is None:
            return 0.0, model or "unknown embedding model"
        return int(record.get("total_tokens") or 0) * price / 1_000_000, None
    return 0.0, None  # search / premium voice are free allowances, tracked separately


# --- the running monthly total and alerts ----------------------------------------------------------------


class CostMeter(ClosingDB):
    """Adds each usage record's estimated cost to this month's total; alerts per `check_budget`."""

    def __init__(self, db_path: Path, prices: Prices, budget_usd: float, warn_percent: float,
                 alert: Callable[[str], None], usage_logs: list[Path] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._prices, self._budget, self._alert, self._clock = prices, budget_usd, alert, clock
        self._levels = tuple(sorted({warn_percent, *LEVELS}))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS usage_months (month TEXT PRIMARY KEY, openai_usd REAL, "
                             "unpriced TEXT, alerted_percent REAL, alerted_day TEXT)")
        self._backfill(self._month(), usage_logs or [])

    def _month(self) -> str:
        return f"{self._clock():%Y-%m}"

    def _backfill(self, month: str, logs: list[Path]) -> None:
        """First run in a month: count what was already logged this month before the meter existed."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM usage_months WHERE month = ?", (month,)).fetchone():
                return
        total, unpriced = 0.0, set()
        for path in logs:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if str(record.get("ts", ""))[:7] == month:
                    cost, missing = cost_of(record, self._prices)
                    total += cost
                    if missing:
                        unpriced.add(missing)
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO usage_months VALUES (?, ?, ?, 0, '')",
                             (month, total, ",".join(sorted(unpriced))))

    def observe(self, record: dict[str, Any]) -> None:
        cost, missing = cost_of(record, self._prices)
        if not cost and not missing:
            return
        month = self._month()
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO usage_months VALUES (?, 0, '', 0, '')", (month,))
            self._db.execute("UPDATE usage_months SET openai_usd = openai_usd + ? WHERE month = ?", (cost, month))
            if missing:
                row = self._db.execute("SELECT unpriced FROM usage_months WHERE month = ?", (month,)).fetchone()
                names = set(filter(None, (row[0] or "").split(","))) | {missing}
                self._db.execute("UPDATE usage_months SET unpriced = ? WHERE month = ?", (",".join(sorted(names)), month))
        self.check_budget()

    def month_total(self, month: str | None = None) -> tuple[float, list[str]]:
        with self._lock:
            row = self._db.execute("SELECT openai_usd, unpriced FROM usage_months WHERE month = ?",
                                   (month or self._month(),)).fetchone()
        return (row[0], list(filter(None, (row[1] or "").split(",")))) if row else (0.0, [])

    def check_budget(self) -> None:
        """Owner decision: warn once a day while past the warning level, and at once when a higher level
        (100%) is reached."""
        now = self._clock()
        month, today = f"{now:%Y-%m}", f"{now:%Y-%m-%d}"
        spent, _ = self.month_total(month)
        percent = spent / self._budget * 100 if self._budget else 0
        reached = max((level for level in self._levels if percent >= level), default=0)
        if not reached:
            return
        with self._lock, self._db:
            row = self._db.execute("SELECT alerted_percent, alerted_day FROM usage_months WHERE month = ?",
                                   (month,)).fetchone()
            already, last_day = (row[0] or 0, row[1] or "") if row else (0, "")
            if reached <= already and last_day == today:
                return
            self._db.execute("UPDATE usage_months SET alerted_percent = ?, alerted_day = ? WHERE month = ?",
                             (max(reached, already), today, month))
        logger.warning("Budget alert: %.0f%% of $%.2f used this month", percent, self._budget)
        self._alert(f"estimated spending this month is ${spent:.2f}, {percent:.0f}% of your ${self._budget:.2f} budget.")


# --- free allowances (Tavily, ElevenLabs) -----------------------------------------------------------------


def allowance_warnings(checks: list[tuple[str, Callable[[], tuple[int, int | None]]]], warn_percent: float) -> list[str]:
    """Warnings for free allowances past `warn_percent`. A service that can't be reached is skipped (health
    checks report that)."""
    warnings = []
    for label, usage in checks:
        try:
            used, limit = usage()
        except Exception:
            continue
        if limit and used / limit * 100 >= warn_percent:
            warnings.append(f"{label}: {used:,} of {limit:,} used this month ({used / limit:.0%}).")
    return warnings


def report(meter: CostMeter, budget_usd: float, allowances: list[tuple[str, Callable[[], tuple[int, int | None]]]],
           hard_cap_note: str) -> str:
    spent, unpriced = meter.month_total()
    lines = [f"Usage this month ({meter._month()}):",
             f"  OpenAI (estimated): ${spent:.4f} of ${budget_usd:.2f} budget ({spent / budget_usd:.1%})"]
    if unpriced:
        lines.append(f"  No price known for: {', '.join(unpriced)} (add it to config/prices.json)")
    for label, usage in allowances:
        try:
            used, limit = usage()
            lines.append(f"  {label}: {used:,} of {limit:,} used" if limit else f"  {label}: {used:,} used")
        except Exception as e:
            lines.append(f"  {label}: could not check ({type(e).__name__})")
    lines.append(f"  {hard_cap_note}")
    return "\n".join(lines)
