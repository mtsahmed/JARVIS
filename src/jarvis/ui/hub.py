"""Bridge from the core's event bus (any thread) to connected HUD pages (the server's asyncio thread).

Keeps what a newly connected page needs (the snapshot: mode, last exchange, recent log) and hands each
event to every page's queue. It never blocks the publisher: a page that falls behind loses its oldest
messages, never slows voice.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any

from jarvis.core.events import ERROR, REPLY, STATE, TRANSCRIPT, Event, EventBus
from jarvis.ui.protocol import envelope, now_utc

logger = logging.getLogger(__name__)

LOG_SIZE = 50  # conversation log entries kept (docs/ui-design.md)
QUEUE_SIZE = 256  # per page
MAX_TEXT = 4000  # characters of one text shown on the HUD


class Hub:
    def __init__(self, bus: EventBus) -> None:
        self._lock = threading.Lock()
        self._seq = 0  # numbers events so a page never gets one twice (snapshot + live)
        self._mode, self._detail = "idle", ""
        self._last_user = ""
        self._last_reply: dict[str, Any] | None = None
        self._log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: dict[asyncio.Queue[str], int] = {}  # page queue -> seq its snapshot already covers
        self._unsubscribe = bus.subscribe(self._on_event)

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def close(self) -> None:
        self._unsubscribe()

    # --- publisher's thread -------------------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        data = _for_display(event)
        if data is None:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
            if event.type == STATE:
                self._mode, self._detail = data["mode"], data["detail"]
            elif event.type == TRANSCRIPT and data["final"]:
                self._last_user = data["text"]
                self._log.append({"role": "user", "text": data["text"], "ts": now_utc()})
            elif event.type == REPLY:
                self._last_reply = data
                self._log.append({"role": "jarvis", **data, "ts": now_utc()})
            elif event.type == ERROR:
                self._log.append({"role": "error", "text": data["message"], "ts": now_utc()})
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._fan_out, seq, envelope(event.type, data))
            except RuntimeError:  # the loop closed in between (Jarvis shutting down)
                pass

    # --- server's asyncio thread --------------------------------------------------------------------

    def _fan_out(self, seq: int, message: str) -> None:
        for page, covered in list(self._clients.items()):
            if seq > covered:
                _put_dropping_oldest(page, message)

    def add_client(self) -> tuple[asyncio.Queue[str], str]:
        """A queue for a new page and its snapshot message (events after the snapshot follow in the queue)."""
        page: asyncio.Queue[str] = asyncio.Queue(QUEUE_SIZE)
        with self._lock:
            self._clients[page] = self._seq
            snapshot = {
                "mode": self._mode, "detail": self._detail,
                "last_user": self._last_user, "last_reply": self._last_reply or {},
                "log": list(self._log),
                # Filled in UI-4.
                "reminders": [], "usage": [], "providers": [], "system": {},
            }
        return page, envelope("snapshot", snapshot)

    def remove_client(self, page: asyncio.Queue[str]) -> None:
        self._clients.pop(page, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)


def send_to(page: asyncio.Queue[str], message: str) -> None:
    """Queue a message for one page (server's thread), e.g. a pong."""
    _put_dropping_oldest(page, message)


def _put_dropping_oldest(page: asyncio.Queue[str], message: str) -> None:
    if page.full():
        try:
            page.get_nowait()
        except asyncio.QueueEmpty:
            pass
    page.put_nowait(message)


def _clip(text: Any) -> str:
    text = str(text)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _for_display(event: Event) -> dict[str, Any] | None:
    """The event's data in protocol shape (texts clipped), or None for events the HUD does not show."""
    d = event.data
    if event.type == STATE:
        return {"mode": d["mode"], "detail": _clip(d.get("detail", ""))}
    if event.type == TRANSCRIPT:
        return {"text": _clip(d["text"]), "final": bool(d.get("final", True))}
    if event.type == REPLY:
        sources = [{"title": _clip(s.get("title", "")), "url": str(s.get("url", ""))}
                   for s in d.get("sources", []) if str(s.get("url", "")).startswith("https://")]
        return {"text": _clip(d["text"]), "sources": sources, "kind": d.get("kind", "answer")}
    if event.type == ERROR:
        return {"code": d["code"], "message": _clip(d["message"]), "recoverable": bool(d.get("recoverable", True))}
    logger.debug("Event type %s is not shown on the HUD", event.type)
    return None
