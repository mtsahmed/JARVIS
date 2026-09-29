"""Bridge from the core's event bus (any thread) to connected HUD pages (the server's asyncio thread).

Keeps what a newly connected page needs (the snapshot: mode, last exchange, recent log) and hands each
event to every page's queue. It never blocks the publisher: a page that falls behind loses its oldest
messages, never slows voice.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from jarvis.core.events import CONFIRM_REQUEST, CONFIRM_RESULT, ERROR, REPLY, STATE, TRANSCRIPT, Event, EventBus
from jarvis.ui.protocol import envelope, now_utc

logger = logging.getLogger(__name__)

LOG_SIZE = 50  # conversation log entries kept (docs/ui-design.md)
QUEUE_SIZE = 256  # per page
MAX_TEXT = 4000  # characters of one text shown on the HUD


OUTCOME_TEXT = {"approved": "approved", "denied": "declined", "expired": "not answered in time: declined"}
BY_TEXT = {"voice": "by voice", "screen": "on the HUD", "terminal": "in the terminal", "timeout": ""}


class Hub:
    def __init__(self, bus: EventBus, voice_available: Callable[[], bool] = lambda: False) -> None:
        self._voice_available = voice_available  # voice mode running: the page's mic button works
        self._confirm: dict[str, Any] | None = None  # the open approval request, if any
        self._confirm_deadline = 0.0
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
        if event.type == STATE:
            data["voice"] = self._voice_available()  # voice mode can start or fall back after the page connects
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
            elif event.type == CONFIRM_REQUEST:
                self._confirm, self._confirm_deadline = data, time.monotonic() + data["expires_in_s"]
            elif event.type == CONFIRM_RESULT:
                if self._confirm is not None and self._confirm["request_id"] == data["request_id"]:
                    by = BY_TEXT.get(data["by"], "")
                    self._log.append({"role": "approval", "ts": now_utc(), "text": f"{self._confirm['summary']} "
                                      f"{OUTCOME_TEXT.get(data['outcome'], data['outcome'])}{' ' + by if by else ''}."})
                    self._confirm = None
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
                "mode": self._mode, "detail": self._detail, "voice": self._voice_available(),
                "confirm": self._pending_confirm(),
                "last_user": self._last_user, "last_reply": self._last_reply or {},
                "log": list(self._log),
                # Filled in UI-4.
                "reminders": [], "usage": [], "providers": [], "system": {},
            }
        return page, envelope("snapshot", snapshot)

    def _pending_confirm(self) -> dict[str, Any] | None:
        """The open approval with the time it has left (under the lock)."""
        if self._confirm is None:
            return None
        left = self._confirm_deadline - time.monotonic()
        return {**self._confirm, "expires_in_s": round(left, 1)} if left > 0 else None

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
    if event.type == CONFIRM_REQUEST:
        return {"request_id": d["request_id"], "action": _clip(d["action"]), "target": _clip(d.get("target", "")),
                "summary": _clip(d["summary"]), "tier": d.get("tier", "CONFIRM"), "expires_in_s": d["expires_in_s"]}
    if event.type == CONFIRM_RESULT:
        return {"request_id": d["request_id"], "outcome": d["outcome"], "by": d["by"]}
    logger.debug("Event type %s is not shown on the HUD", event.type)
    return None
