"""Health checks (Phase 7B): `jarvis --health`, the check at startup, and alerts on repeated failures.

Every check is free: no chat tokens, no search credits, no voice characters are used.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIN_FREE_DISK_GB = 2.0


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok", "warn" (works, but look at it), or "fail" (broken)
    detail: str


def ok(name: str, detail: str) -> Check:
    return Check(name, "ok", detail)


def warn(name: str, detail: str) -> Check:
    return Check(name, "warn", detail)


def fail(name: str, detail: str) -> Check:
    return Check(name, "fail", detail)


def run_checks(checks: list[tuple[str, Callable[[], Check]]]) -> list[Check]:
    """Run each check; a check that crashes is reported as failed, never raised."""
    results = []
    for name, check in checks:
        try:
            results.append(check())
        except Exception as e:
            logger.warning("Health check %s crashed: %s", name, type(e).__name__)
            results.append(fail(name, f"check could not run ({type(e).__name__})"))
    return results


def report(results: list[Check]) -> str:
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"  [{marks[c.status]}] {c.name:18} {c.detail}" for c in results]
    problems = sum(c.status == "fail" for c in results)
    lines.append(f"\n{'All good.' if not problems else f'{problems} problem(s) need attention.'}")
    return "Jarvis health check:\n" + "\n".join(lines)


# --- individual checks (each takes the thing to check; no hidden global state) ---------------------------


def check_openai(llm: Any) -> Check:
    ping = getattr(llm, "ping", None)
    if ping is None:
        return warn("OpenAI", "this provider has no health check")
    from jarvis.llm.base import LLMError

    try:
        ping()
    except LLMError as e:
        return fail("OpenAI", str(e))
    return ok("OpenAI", f"reachable, model {llm.model} available")


def check_search(search: Any) -> Check:
    if search is None:
        return warn("Web search", "off (JARVIS_SEARCH_PROVIDER is blank)")
    from jarvis.tools.search.base import SearchError

    try:
        used, limit = search.usage()
    except SearchError as e:
        return fail("Web search", str(e))
    if limit and used >= limit:
        return fail("Web search", f"monthly credits used up ({used} of {limit})")
    return ok("Web search", f"{search.name}: {used} of {limit or 'unlimited'} credits used this month")


def check_elevenlabs(characters_left: Callable[[], int] | None) -> Check:
    if characters_left is None:
        return warn("Premium voice", "off (ElevenLabs not enabled)")
    from jarvis.voice.tts.base import TTSError

    try:
        left = characters_left()
    except TTSError as e:
        return fail("Premium voice", f"{e} (the Windows voice is used instead)")
    if left < 1000:
        return warn("Premium voice", f"only {left:,} characters left this month (Windows voice will take over)")
    return ok("Premium voice", f"ElevenLabs: {left:,} characters left this month")


def check_gmail(gmail: Any) -> Check:
    if gmail is None:
        return warn("Gmail", "not set up (morning briefing email part is off)")
    from jarvis.integrations.gmail import GmailAuthError, GmailError

    try:
        gmail._credentials()  # refreshes the sign-in (free); does not read any email
    except GmailAuthError as e:
        return fail("Gmail", str(e))
    except GmailError as e:
        return fail("Gmail", str(e))
    return ok("Gmail", "signed in (read-only)")


def check_microphone() -> Check:
    import sounddevice as sd

    try:
        device = sd.query_devices(kind="input")
    except Exception:
        return fail("Microphone", "no input device found")
    return ok("Microphone", str(device.get("name", "default input")))


def check_disk(path: Path, min_free_gb: float = MIN_FREE_DISK_GB) -> Check:
    free_gb = shutil.disk_usage(path).free / 1024**3
    if free_gb < min_free_gb:
        return warn("Disk space", f"only {free_gb:.1f} GB free")
    return ok("Disk space", f"{free_gb:.0f} GB free")


def check_daily_job(name: str, label: str, last_run: date | None, expected_daily: bool, today: date) -> Check:
    if not expected_daily:
        return warn(label, "not scheduled")
    if last_run is None:
        return warn(label, "has not run yet")
    days = (today - last_run).days
    if days > 1:
        return warn(label, f"last ran {days} days ago ({last_run:%d %b})")
    return ok(label, f"last ran {'today' if days == 0 else 'yesterday'} ({last_run:%d %b})")


def check_autostart(status: str) -> Check:
    return ok("Auto-start", "on") if status == "on" else warn("Auto-start", status)


def start_background_check(checks: list[tuple[str, Callable[[], Check]]],
                           on_problems: Callable[[list[Check]], None]) -> threading.Thread:
    """Run the checks without delaying startup; report failures (not warnings) once."""

    def run() -> None:
        results = run_checks(checks)
        logger.info("Startup health check: %s", ", ".join(f"{c.name}={c.status}" for c in results))
        if problems := [c for c in results if c.status == "fail"]:
            on_problems(problems)

    thread = threading.Thread(target=run, name="health-check", daemon=True)
    thread.start()
    return thread


# --- repeated failures during use --------------------------------------------------------------------


class FailureWatcher:
    """Watches usage records; after `threshold` failures in a row of one service, alerts (at most hourly)."""

    LABELS = {"llm": "the language service (OpenAI)", "search": "web search", "tts": "the premium voice"}

    def __init__(self, alert: Callable[[str], None], threshold: int = 3, quiet_seconds: float = 3600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._alert, self._threshold, self._quiet, self._clock = alert, threshold, quiet_seconds, clock
        self._failures: dict[str, int] = {}
        self._last_alert: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, record: dict[str, Any]) -> None:
        kind = record.get("kind")
        if kind not in self.LABELS:
            return
        failed = record.get("status") == "error" or (kind == "tts" and record.get("fallback") is True)
        with self._lock:
            if not failed:
                self._failures[kind] = 0
                return
            self._failures[kind] = count = self._failures.get(kind, 0) + 1
            now = self._clock()
            if count < self._threshold or now - self._last_alert.get(kind, -self._quiet) < self._quiet:
                return
            self._last_alert[kind] = now
        self._alert(f"{self.LABELS[kind]} has failed {count} times in a row. Run: jarvis --health")
