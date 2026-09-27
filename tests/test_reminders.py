import json
import queue
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.scheduler import ReminderScheduler
from jarvis.tools.registry import DECLINED, ToolRegistry
from jarvis.tools.reminders import Reminder, ReminderStore, describe_when, reminder_tools

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=TZ)  # Sunday


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Answer:
    def __init__(self, yes: bool) -> None:
        self.yes, self.asked = yes, []

    def confirm(self, question: str) -> bool:
        self.asked.append(question)
        return self.yes


@pytest.fixture
def store(tmp_path: Path) -> ReminderStore:
    return ReminderStore(tmp_path / "jarvis.db")


def setup(store: ReminderStore, clock: Clock | None = None) -> tuple[ToolRegistry, list[dict[str, Any]], Clock]:
    clock = clock or Clock()
    audit: list[dict[str, Any]] = []
    return ToolRegistry(reminder_tools(store, clock), audit_sink=audit.append), audit, clock


def create(registry: ToolRegistry, text: str, due: str) -> str:
    return registry.run("create_reminder", json.dumps({"text": text, "due": due}))


# --- tools ---------------------------------------------------------------------------------------


def test_create_and_list(store: ReminderStore) -> None:
    registry, audit, _ = setup(store)
    assert create(registry, "call Mom", "2026-09-27 18:30") == "Reminder 1 set: 'call Mom' at 6:30 PM today."
    create(registry, "dentist", "2026-09-28 09:00")
    listed = registry.run("list_reminders", "{}")
    assert "#1: 'call Mom' at 6:30 PM today" in listed and "#2: 'dentist' at 9:00 AM tomorrow" in listed
    assert [a["decision"] for a in audit] == ["auto", "auto", "auto"]


def test_past_time_and_bad_format_rejected(store: ReminderStore) -> None:
    registry, _, _ = setup(store)
    assert "already passed" in create(registry, "late", "2026-09-27 17:00")
    assert "must look like" in create(registry, "x", "tomorrow 6pm")
    assert "needs some text" in create(registry, "  ", "2026-09-27 19:00")
    assert store.pending() == []


def test_cancel_needs_spoken_yes(store: ReminderStore) -> None:
    registry, audit, _ = setup(store)
    create(registry, "call Mom", "2026-09-27 18:30")

    no = Answer(yes=False)
    assert registry.run("cancel_reminder", '{"id": 1}', no) == DECLINED
    assert no.asked == ["Cancel the reminder 'call Mom' at 6:30 PM today?"]
    assert len(store.pending()) == 1

    assert registry.run("cancel_reminder", '{"id": 1}', Answer(yes=True)) == "Reminder 1 cancelled."
    assert store.pending() == []
    assert [a["decision"] for a in audit if a["tool"] == "cancel_reminder"] == ["denied", "approved"]


def test_cancel_unknown_reminder_is_clear_error(store: ReminderStore) -> None:
    registry, _, _ = setup(store)
    assert "no upcoming reminder number 9" in registry.run("cancel_reminder", '{"id": 9}', Answer(yes=True))


def test_describe_when() -> None:
    assert describe_when(NOW.replace(hour=9, minute=5), NOW) == "9:05 AM today"
    assert describe_when(NOW + timedelta(days=1), NOW) == "6:00 PM tomorrow"
    assert describe_when(NOW + timedelta(days=2), NOW) == "Tuesday 29 September at 6:00 PM"


def test_store_survives_restart(tmp_path: Path) -> None:
    ReminderStore(tmp_path / "jarvis.db").add("persist me", NOW + timedelta(hours=1))
    assert [r.text for r in ReminderStore(tmp_path / "jarvis.db").pending()] == ["persist me"]


# --- scheduler -----------------------------------------------------------------------------------


def make_scheduler(store: ReminderStore, clock: Clock) -> tuple[ReminderScheduler, list[tuple[Reminder, bool]]]:
    fired: list[tuple[Reminder, bool]] = []
    return ReminderScheduler(store, lambda r, missed: fired.append((r, missed)), clock=clock), fired


def test_fires_when_due_and_only_once(store: ReminderStore) -> None:
    clock = Clock()
    store.add("stretch", NOW + timedelta(minutes=2))
    scheduler, fired = make_scheduler(store, clock)

    assert scheduler.check_now() == 0  # not yet
    clock.now = NOW + timedelta(minutes=2, seconds=3)
    assert scheduler.check_now() == 1
    assert fired[0][0].text == "stretch" and fired[0][1] is False
    assert scheduler.check_now() == 0  # never twice
    assert store.pending() == []


def test_reminder_missed_while_off_fires_as_missed(store: ReminderStore) -> None:
    store.add("take pills", NOW - timedelta(hours=2))  # due while Jarvis was not running
    scheduler, fired = make_scheduler(store, Clock())
    scheduler.check_now()
    assert fired[0][0].text == "take pills" and fired[0][1] is True


def test_cancelled_reminder_never_fires(store: ReminderStore) -> None:
    reminder = store.add("skip me", NOW + timedelta(minutes=1))
    store.cancel(reminder.id)
    scheduler, fired = make_scheduler(store, Clock(NOW + timedelta(minutes=5)))
    scheduler.check_now()
    assert fired == []


def test_alert_failure_does_not_stop_other_reminders(store: ReminderStore) -> None:
    store.add("first", NOW - timedelta(seconds=10))
    store.add("second", NOW - timedelta(seconds=5))
    seen: list[str] = []

    def flaky(reminder: Reminder, missed: bool) -> None:
        seen.append(reminder.text)
        if reminder.text == "first":
            raise RuntimeError("notification broke")

    ReminderScheduler(store, flaky, clock=Clock()).check_now()
    assert seen == ["first", "second"]


def test_background_thread_starts_and_stops(store: ReminderStore) -> None:
    store.add("now", NOW - timedelta(seconds=1))
    fired: queue.Queue[str] = queue.Queue()
    scheduler = ReminderScheduler(store, lambda r, m: fired.put(r.text), clock=Clock(), check_seconds=0.05)
    scheduler.start()
    assert fired.get(timeout=2) == "now"
    scheduler.stop()
