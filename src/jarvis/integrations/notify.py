"""Windows desktop notifications. The only module that imports `windows_toasts`.

Load order matters: the WinRT packages behind windows_toasts ship their own msvcp140.dll. If they
are imported before the faster-whisper model loads, loading the model crashes the process (access
violation). So `windows_toasts` is imported lazily, on the first notification, which in voice mode
is always after the speech model is loaded.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        """Show a desktop notification. Never raises."""
        ...


class WindowsNotifier:
    def __init__(self, app_name: str = "Jarvis") -> None:
        self._app_name = app_name
        self._toaster: Any = None  # created on first use (see module docstring)

    def notify(self, title: str, message: str) -> None:
        try:
            from windows_toasts import Toast, WindowsToaster

            if self._toaster is None:
                self._toaster = WindowsToaster(self._app_name)
            # show_toast hands the toast to Windows and returns immediately.
            self._toaster.show_toast(Toast(text_fields=[title, message]))
        except Exception as e:
            logger.error("Desktop notification failed: %s", type(e).__name__)
