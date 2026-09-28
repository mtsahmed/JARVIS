"""Everything the user sends Jarvis, in one queue (UI-3A).

- Talk-key events: "start" / "stop" from the push-to-talk key (and, from UI-3C, the HUD's mic button),
  plus "cancel" (stop recording and throw it away).
- Typed requests: from the terminal (text mode) or the HUD's text box. A typed request is handled
  exactly like a spoken one (voice mode speaks the reply).

The voice loop or the text loop reads the queue on Jarvis's main thread; anything may put into it.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Literal, Union

KeyEvent = Literal["start", "stop", "cancel"]
KEY_EVENTS: frozenset[str] = frozenset({"start", "stop", "cancel"})
MAX_TYPED = 2000  # characters (docs/ui-protocol.md)
END = "end"  # the terminal closed (text mode)


@dataclass(frozen=True)
class Typed:
    text: str
    source: Literal["terminal", "hud"]


Item = Union[str, Typed]  # a KeyEvent, END, or a Typed request


class Inbox:
    def __init__(self) -> None:
        self.queue: queue.Queue[Item] = queue.Queue()

    def key(self, action: str) -> None:
        if action not in KEY_EVENTS:
            raise ValueError(f"unknown talk-key action: {action}")
        self.queue.put(action)

    def type(self, text: str, source: Literal["terminal", "hud"] = "hud") -> bool:
        """Queue a typed request. False (and nothing queued) if it is empty or too long."""
        text = text.strip()
        if not text or len(text) > MAX_TYPED:
            return False
        self.queue.put(Typed(text, source))
        return True
