"""Background thread that fires due reminders (works offline; only needs Jarvis to be running)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Protocol
from datetime import datetime, timedelta

from jarvis.tools.reminders import Clock, Reminder, ReminderStore, local_now

logger = logging.getLogger(__name__)

CHECK_SECONDS = 5.0
# A reminder that fires more than this late was missed (e.g. Jarvis was not running at the time).
MISSED_AFTER = timedelta(minutes=1)

OnDue = Callable[[Reminder, bool], None]  # (reminder, missed)


class DailyJobLike(Protocol):
    def tick(self) -> bool: ...


class ReminderScheduler:
    def __init__(self, store: ReminderStore, on_due: OnDue, clock: Clock = local_now,
                 check_seconds: float = CHECK_SECONDS, daily_jobs: Sequence[DailyJobLike] = ()) -> None:
        self._daily_jobs = tuple(daily_jobs)  # e.g. the morning briefing
        self._store = store
        self._on_due = on_due
        self._clock = clock
        self._check_seconds = check_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def check_now(self) -> int:
        """Fire everything due now. Returns how many fired. Called by the thread; public for tests."""
        now = self._clock()
        due = self._store.take_due(now)
        for reminder in due:
            missed = now - reminder.due > MISSED_AFTER
            logger.info("Reminder %s due%s", reminder.id, " (missed)" if missed else "")
            try:
                self._on_due(reminder, missed)
            except Exception:
                logger.exception("Reminder %s alert failed", reminder.id)
        return len(due)

    def _run(self) -> None:
        # First check immediately: fires reminders missed while Jarvis was off.
        while True:
            try:
                self.check_now()
                for job in self._daily_jobs:
                    job.tick()
            except Exception:
                logger.exception("Reminder check failed")
            if self._stop.wait(self._check_seconds):
                return
