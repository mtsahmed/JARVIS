"""In-process event bus: the core publishes what Jarvis is doing; displays (e.g. the HUD) subscribe.

The core knows nothing about who listens. Publishing never blocks and never raises: subscribers are
called on the publisher's thread, so they must be quick (the HUD only queues the event) and a failing
subscriber is logged and skipped, never allowed to disturb voice.

Event types and their data (same shapes as docs/ui-protocol.md):
- "state":      {"mode": one of MODES, "detail": str}
- "transcript": {"text": str, "final": bool}             what the user said (or typed in the terminal)
- "reply":      {"text": str, "sources": [{"title", "url"}], "kind": "answer" | "reminder" | "briefing"}
- "error":      {"code": str, "message": str, "recoverable": bool}   user-friendly text only
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

STATE, TRANSCRIPT, REPLY, ERROR = "state", "transcript", "reply", "error"
MODES = frozenset({"idle", "listening", "thinking", "speaking", "confirm", "error"})


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Call `callback` for every event from now on. Returns a function that unsubscribes."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, type: str, **data: Any) -> None:
        event = Event(type, data)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception("Event subscriber failed on %s", type)

    # Small helpers so publishers can't get the shapes wrong.

    def state(self, mode: str, detail: str = "") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")  # a bug in Jarvis, not a runtime condition
        self.publish(STATE, mode=mode, detail=detail)

    def transcript(self, text: str, final: bool = True) -> None:
        self.publish(TRANSCRIPT, text=text, final=final)

    def reply(self, text: str, sources: list[dict[str, str]] | None = None, kind: str = "answer") -> None:
        self.publish(REPLY, text=text, sources=sources or [], kind=kind)

    def error(self, code: str, message: str, recoverable: bool = True) -> None:
        self.publish(ERROR, code=code, message=message, recoverable=recoverable)
