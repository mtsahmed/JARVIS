"""Text safety helpers."""

from __future__ import annotations

import re

# C0 controls except tab and newline, DEL, and C1 controls. This includes ESC (\x1b), which starts terminal
# escape sequences that could hide or rewrite what is shown in the console (Phase 6C threat T16).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def for_terminal(text: str) -> str:
    """Remove control characters from outside text (web, email, files, model output) before printing it."""
    return _CONTROL.sub("", text)
