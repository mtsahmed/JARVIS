"""Regression test: Jarvis must not load the WinRT libraries at startup.

They ship their own msvcp140.dll; if loaded before the faster-whisper model, loading the model
crashes the process (access violation). They may only load on the first notification.
"""

import subprocess
import sys

CHECK = """
import sys
import jarvis.cli
alerts = jarvis.cli.ReminderAlerts()
loaded = sorted(m for m in sys.modules if m.startswith(("windows_toasts", "winrt")))
print("LOADED:", loaded)
"""


def test_startup_does_not_load_winrt() -> None:
    # A fresh interpreter, so other tests' imports don't interfere.
    result = subprocess.run([sys.executable, "-c", CHECK], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "LOADED: []" in result.stdout
