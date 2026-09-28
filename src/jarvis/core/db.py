"""Shared behaviour for the small SQLite stores (reminders, memory, file index, daily jobs)."""

from __future__ import annotations


class ClosingDB:
    """Mixin for classes holding `self._db` (sqlite3 connection) and `self._lock`.

    An open database file stays locked on Windows, so connections are closed explicitly on exit
    (and, as a fallback, when the object is garbage-collected).
    """

    def close(self) -> None:
        db = getattr(self, "_db", None)
        if db is None:
            return
        lock = getattr(self, "_lock", None)
        if lock is not None:
            with lock:
                db.close()
        else:
            db.close()
        self._db = None  # type: ignore[assignment]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # interpreter shutdown: nothing useful to do
            pass
