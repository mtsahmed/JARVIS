"""Daily backups of memories and reminders (Phase 7C). Owner decision: on this laptop, last 14 days.

Uses SQLite's online backup (sqlite3.Connection.backup), which makes a consistent copy even while Jarvis
is using the database. Each copy is integrity-checked before it is kept. Not backed up: the file index
(rebuilt automatically), speech models (re-downloaded), secrets (Credential Manager + password manager).
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATABASES = ("memory.db", "jarvis.db")  # memories; reminders + daily-job state
KEEP_DAYS = 14
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BackupError(Exception):
    """A backup or restore could not be completed. Nothing was changed if a restore fails."""


def backups_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def list_backups(data_dir: Path) -> list[date]:
    folder = backups_dir(data_dir)
    if not folder.is_dir():
        return []
    return sorted(date.fromisoformat(p.name) for p in folder.iterdir() if p.is_dir() and _DATED.match(p.name))


def backup_now(data_dir: Path, today: date) -> Path:
    """Copy the databases into backups/<today>/ (replacing an earlier backup from today), then prune."""
    target = backups_dir(data_dir) / today.isoformat()
    staging = target.with_name(f".{today.isoformat()}.partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    copied = 0
    for name in DATABASES:
        source = data_dir / name
        if source.is_file():
            _copy_db(source, staging / name)
            copied += 1
    if not copied:
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError("There is nothing to back up yet.")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)  # the dated folder only ever holds a complete, checked backup
    prune(data_dir)
    logger.info("Backup saved: %s (%d databases)", target.name, copied)
    return target


def prune(data_dir: Path, keep: int = KEEP_DAYS) -> list[date]:
    """Delete dated backups older than the newest `keep`. Returns the dates removed."""
    dates = list_backups(data_dir)
    removed = dates[:-keep] if len(dates) > keep else []
    for day in removed:
        shutil.rmtree(backups_dir(data_dir) / day.isoformat(), ignore_errors=True)
    return removed


def restore(data_dir: Path, day: date) -> Path:
    """Replace the current databases with the backup from `day`. Jarvis must not be running.

    The current databases are first saved to backups/before-restore-<time>/, so a restore can be undone.
    Returns that safety folder.
    """
    source = backups_dir(data_dir) / day.isoformat()
    if not source.is_dir():
        raise BackupError(f"There is no backup from {day.isoformat()}.")
    for name in DATABASES:
        if (source / name).is_file():
            _check(source / name)  # never restore a damaged copy
    safety = backups_dir(data_dir) / f"before-restore-{datetime.now():%Y-%m-%d-%H%M%S}"
    safety.mkdir(parents=True)
    for name in DATABASES:
        if (data_dir / name).is_file():
            _copy_db(data_dir / name, safety / name)
    for name in DATABASES:
        if (source / name).is_file():
            _copy_db(source / name, data_dir / name)
    logger.info("Restored backup %s (previous data kept in %s)", day.isoformat(), safety.name)
    return safety


def _copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _check(target)


def _check(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        raise BackupError(f"{path.name} is not a valid database ({e}).") from None
    finally:
        db.close()
    if result != "ok":
        raise BackupError(f"{path.name} failed its integrity check.")
