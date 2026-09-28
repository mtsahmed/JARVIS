"""Phase 7C: daily backups of memories and reminders, pruning, and safe restore."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from jarvis.core import backup
from jarvis.memory.store import MemoryStore
from jarvis.tools.reminders import ReminderStore, local_now

DAY = date(2026, 9, 28)


def make_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    memories = MemoryStore(data / "memory.db")
    memories.add("The user's car is a Toyota.")
    memories.close()
    reminders = ReminderStore(data / "jarvis.db")
    reminders.add("call Mom", local_now() + timedelta(days=1))
    reminders.close()
    (data / "files.db").write_bytes(b"rebuildable index, not backed up")
    return data


def facts(data: Path) -> list[str]:
    store = MemoryStore(data / "memory.db")
    try:
        return [m.text for m in store.all()]
    finally:
        store.close()


def test_backup_copies_the_two_databases_and_checks_them(tmp_path: Path) -> None:
    data = make_data(tmp_path)
    folder = backup.backup_now(data, DAY)
    assert folder == data / "backups" / "2026-09-28"
    assert sorted(p.name for p in folder.iterdir()) == ["jarvis.db", "memory.db"]  # not the file index
    assert facts(folder) == ["The user's car is a Toyota."]  # the backup copy itself (folder/memory.db)
    copy = ReminderStore(folder / "jarvis.db")
    assert [r.text for r in copy.pending()] == ["call Mom"]
    copy.close()
    assert backup.list_backups(data) == [DAY]


def test_backup_while_the_database_is_open_and_being_written(tmp_path: Path) -> None:
    data = make_data(tmp_path)
    live = MemoryStore(data / "memory.db")  # Jarvis running: connection open
    live.add("The user prefers tea.")
    try:
        folder = backup.backup_now(data, DAY)
    finally:
        live.close()
    copy = MemoryStore(folder / "memory.db")
    assert [m.text for m in copy.all()] == ["The user's car is a Toyota.", "The user prefers tea."]
    copy.close()


def test_same_day_backup_replaced_and_old_ones_pruned(tmp_path: Path) -> None:
    data = make_data(tmp_path)
    for offset in range(20):
        backup.backup_now(data, DAY - timedelta(days=offset))
    backup.backup_now(data, DAY)  # again today: replaced, not duplicated
    days = backup.list_backups(data)
    assert len(days) == backup.KEEP_DAYS and days[-1] == DAY and days[0] == DAY - timedelta(days=13)
    assert not any(p.name.endswith(".partial") for p in (data / "backups").iterdir())


def test_nothing_to_back_up(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="nothing to back up"):
        backup.backup_now(tmp_path / "empty", DAY)


def test_restore_replaces_data_and_keeps_the_previous_version(tmp_path: Path) -> None:
    data = make_data(tmp_path)
    backup.backup_now(data, DAY)
    later = MemoryStore(data / "memory.db")
    later.add("A fact added after the backup.")
    later.close()

    safety = backup.restore(data, DAY)
    assert facts(data) == ["The user's car is a Toyota."]
    assert safety.name.startswith("before-restore-")
    assert [m.text for m in MemoryStore(safety / "memory.db").all()][-1] == "A fact added after the backup."
    assert backup.list_backups(data) == [DAY]  # the safety copy is not counted as a dated backup


def test_damaged_backup_is_never_restored(tmp_path: Path) -> None:
    data = make_data(tmp_path)
    folder = backup.backup_now(data, DAY)
    (folder / "memory.db").write_bytes(b"this is not a database" * 100)
    with pytest.raises(backup.BackupError, match="memory.db"):
        backup.restore(data, DAY)
    assert facts(data) == ["The user's car is a Toyota."]  # nothing changed


def test_restore_unknown_date(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="no backup from 2020-01-01"):
        backup.restore(make_data(tmp_path), date(2020, 1, 1))


# --- commands ------------------------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from jarvis.core.config import _KEYS, load_config

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-x\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\n"
                                   "JARVIS_MAX_OUTPUT_TOKENS=1\nJARVIS_LLM_TIMEOUT_SECONDS=1\nJARVIS_HISTORY_TURNS=1\n",
                                   encoding="utf-8")
    make_data(tmp_path)
    return load_config(project_root=tmp_path)


def test_backup_and_restore_commands(config, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    import jarvis.cli as cli
    from jarvis.tools.briefing import JobState

    assert cli.backup_command(config) == 0
    assert JobState(config.data_dir / "jarvis.db").last_run("backup") == local_now().date()
    assert cli.restore_command(config, "list") == 0 and local_now().date().isoformat() in capsys.readouterr().out
    assert cli.restore_command(config, "not-a-date") == 1
    assert cli.restore_command(config, local_now().date().isoformat(), ask=lambda q: "n") == 0
    assert "Nothing changed." in capsys.readouterr().out
    assert cli.restore_command(config, local_now().date().isoformat(), ask=lambda q: "y") == 0
    assert "Restored" in capsys.readouterr().out


def test_restore_refused_while_jarvis_runs(config, monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
                                           capsys: pytest.CaptureFixture[str]) -> None:
    import jarvis.cli as cli

    cli.backup_command(config)
    monkeypatch.setattr(cli.InstanceLock, "acquire", lambda self: False)
    asked: list[str] = []
    assert cli.restore_command(config, local_now().date().isoformat(), ask=lambda q: asked.append(q) or "y") == 1
    assert "Stop it first" in capsys.readouterr().err and asked == []
