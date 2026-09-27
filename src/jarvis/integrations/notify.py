"""Windows desktop notifications. The only module that imports `windows_toasts`."""

from __future__ import annotations

import logging
from typing import Protocol

from windows_toasts import Toast, WindowsToaster

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        """Show a desktop notification. Never raises."""
        ...


class WindowsNotifier:
    def __init__(self, app_name: str = "Jarvis") -> None:
        self._toaster = WindowsToaster(app_name)

    def notify(self, title: str, message: str) -> None:
        try:
            # show_toast hands the toast to Windows and returns immediately.
            self._toaster.show_toast(Toast(text_fields=[title, message]))
        except Exception as e:
            logger.error("Desktop notification failed: %s", type(e).__name__)
