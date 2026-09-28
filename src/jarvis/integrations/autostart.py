"""Start Jarvis at Windows sign-in via the user's Startup folder (no admin rights, one file, easy to remove).

Windows starts anything in %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup when this user
signs in (Microsoft Support: "Configure Startup applications in Windows"). The entry opens
scripts\\run_jarvis.cmd in a minimized window; that launcher restarts Jarvis after a crash.
"""

from __future__ import annotations

import os
from pathlib import Path

ENTRY_NAME = "Jarvis.cmd"
MARKER = "rem Jarvis auto-start entry"  # identifies files this module created (never touch anything else)


def startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher(project_root: Path) -> Path:
    return project_root / "scripts" / "run_jarvis.cmd"


def entry_content(project_root: Path) -> str:
    lines = [
        "@echo off",
        f"{MARKER} (created by `jarvis --autostart on`; remove with `jarvis --autostart off`).",
        # start "<title>" /min: open the launcher in its own minimized console window, then this entry exits.
        f'start "Jarvis" /min "{launcher(project_root)}"',
    ]
    return "\r\n".join(lines) + "\r\n"


def status(project_root: Path, folder: Path | None = None) -> str:
    entry = (folder or startup_dir()) / ENTRY_NAME
    if not entry.is_file():
        return "off"
    text = entry.read_text(encoding="utf-8", errors="replace")
    if MARKER not in text:
        return "a different Jarvis.cmd exists (not created by Jarvis)"
    return "on" if str(launcher(project_root)) in text else "on, but pointing to another Jarvis folder"


def enable(project_root: Path, folder: Path | None = None) -> Path:
    if not launcher(project_root).is_file():
        raise FileNotFoundError(f"Launcher not found: {launcher(project_root)}")
    entry = (folder or startup_dir()) / ENTRY_NAME
    if entry.exists() and MARKER not in entry.read_text(encoding="utf-8", errors="replace"):
        raise FileExistsError(f"{entry} exists and was not created by Jarvis; not overwriting it.")
    entry.write_bytes(entry_content(project_root).encode("utf-8"))
    return entry


def disable(folder: Path | None = None) -> bool:
    """Remove the entry if Jarvis created it. Returns True if something was removed."""
    entry = (folder or startup_dir()) / ENTRY_NAME
    if not entry.is_file() or MARKER not in entry.read_text(encoding="utf-8", errors="replace"):
        return False
    entry.unlink()
    return True
