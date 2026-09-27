"""Reminders (UC1): a local SQLite store and the create / list / cancel tools."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvis.tools.base import Tier, Tool, ToolError

Clock = Callable[[], datetime]  # returns an aware local datetime
MAX_TEXT_CHARS = 200
DUE_FORMAT = "%Y-%m-%d %H:%M"


def local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due: datetime  # aware, local time


class ReminderStore:
    """Reminders in SQLite. Cancelled reminders are marked, not deleted, so history stays auditable."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the chat and scheduler threads, guarded by a lock.
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS reminders (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       text TEXT NOT NULL,
                       due_utc TEXT NOT NULL,
                       created_utc TEXT NOT NULL,
                       fired_utc TEXT,
                       cancelled_utc TEXT)"""
            )

    def add(self, text: str, due: datetime) -> Reminder:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO reminders (text, due_utc, created_utc) VALUES (?, ?, ?)",
                (text, _to_utc(due), _to_utc(datetime.now(timezone.utc))),
            )
        return Reminder(int(cursor.lastrowid or 0), text, due)

    def pending(self) -> list[Reminder]:
        """Not yet fired and not cancelled, soonest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, due_utc FROM reminders WHERE fired_utc IS NULL AND cancelled_utc IS NULL "
                "ORDER BY due_utc"
            ).fetchall()
        return [_row(r) for r in rows]

    def get_pending(self, reminder_id: int) -> Reminder | None:
        return next((r for r in self.pending() if r.id == reminder_id), None)

    def cancel(self, reminder_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE reminders SET cancelled_utc = ? WHERE id = ? AND fired_utc IS NULL AND cancelled_utc IS NULL",
                (_to_utc(datetime.now(timezone.utc)), reminder_id),
            )
        return cursor.rowcount == 1

    def take_due(self, now: datetime) -> list[Reminder]:
        """Return reminders due at or before `now` and mark them fired, so each fires exactly once."""
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, text, due_utc FROM reminders WHERE fired_utc IS NULL AND cancelled_utc IS NULL "
                "AND due_utc <= ? ORDER BY due_utc",
                (_to_utc(now),),
            ).fetchall()
            self._db.executemany(
                "UPDATE reminders SET fired_utc = ? WHERE id = ?", [(_to_utc(now), r[0]) for r in rows]
            )
        return [_row(r) for r in rows]


def _to_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # sorts correctly as text


def _row(row: tuple[Any, ...]) -> Reminder:
    due = datetime.strptime(row[2], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    return Reminder(int(row[0]), str(row[1]), due)


def describe_when(due: datetime, now: datetime) -> str:
    """Speakable time: '6:05 PM today', '9:00 AM tomorrow', 'Monday 29 September at 9:00 AM'."""
    due = due.astimezone(now.tzinfo)
    clock = f"{due:%I:%M %p}".lstrip("0")
    days = (due.date() - now.date()).days
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{due:%A} {due.day} {due:%B} at {clock}"


def reminder_tools(store: ReminderStore, clock: Clock = local_now) -> list[Tool]:
    def create(args: dict[str, Any]) -> str:
        text = str(args["text"]).strip()
        if not text:
            raise ToolError("The reminder needs some text.")
        if len(text) > MAX_TEXT_CHARS:
            raise ToolError(f"The reminder text is too long (max {MAX_TEXT_CHARS} characters).")
        now = clock()
        try:
            due = datetime.strptime(str(args["due"]).strip(), DUE_FORMAT).replace(tzinfo=now.tzinfo)
        except ValueError:
            raise ToolError(f"The time must look like 2026-09-28 18:30 (format {DUE_FORMAT}).") from None
        if due <= now - timedelta(seconds=30):
            raise ToolError(f"That time ({describe_when(due, now)}) has already passed. Ask the user for a future time.")
        reminder = store.add(text, due)
        return f"Reminder {reminder.id} set: '{text}' at {describe_when(due, now)}."

    def list_all(args: dict[str, Any]) -> str:
        reminders = store.pending()
        if not reminders:
            return "There are no upcoming reminders."
        now = clock()
        return "Upcoming reminders:\n" + "\n".join(
            f"- #{r.id}: '{r.text}' at {describe_when(r.due, now)}" for r in reminders
        )

    def describe_cancel(args: dict[str, Any]) -> str:
        reminder = store.get_pending(int(args["id"]))
        if reminder is None:
            return f"Cancel reminder number {args['id']}?"
        return f"Cancel the reminder '{reminder.text}' at {describe_when(reminder.due, clock())}?"

    def cancel(args: dict[str, Any]) -> str:
        if not store.cancel(int(args["id"])):
            raise ToolError(f"There is no upcoming reminder number {args['id']}. List reminders to find the right one.")
        return f"Reminder {args['id']} cancelled."

    return [
        Tool(
            "create_reminder",
            "Set a reminder for the user at a specific local date and time. Work out the exact time from the "
            "current date and time given in your instructions.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to remind the user about, short, e.g. 'call Mom'."},
                    "due": {"type": "string", "description": "Local date and time, 24-hour format YYYY-MM-DD HH:MM."},
                },
                "required": ["text", "due"],
                "additionalProperties": False,
            },
            Tier.AUTO,
            create,
        ),
        Tool(
            "list_reminders",
            "List the user's upcoming reminders with their numbers and times.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            Tier.AUTO,
            list_all,
        ),
        Tool(
            "cancel_reminder",
            "Cancel one upcoming reminder by its number. If you don't know the number, list reminders first.",
            {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "The reminder number, e.g. 3."}},
                "required": ["id"],
                "additionalProperties": False,
            },
            Tier.CONFIRM,
            cancel,
            describe=describe_cancel,
        ),
    ]
