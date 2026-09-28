"""One Jarvis at a time: with auto-start, a second copy would also answer the talk key and double alerts.

Uses a byte-range lock on data/jarvis.lock (msvcrt.locking, Python standard library on Windows).
Windows releases the lock when the process ends, even after a crash, so a stale lock cannot block
the next start.
"""

from __future__ import annotations

import msvcrt
import os
from pathlib import Path
from typing import IO


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        """True if this is the only running Jarvis; False if another one holds the lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # non-blocking: fail at once if held
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())  # informational only
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._file.close()
        self._file = None
