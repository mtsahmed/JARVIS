"""Approvals for CONFIRM-tier actions, answerable by voice, typing, or the HUD (UI-3B; protocol §6).

1. Only the permission layer opens an approval (through a confirmer). It is published on the event bus,
   so the HUD shows it while the voice layer asks aloud.
2. The first answer wins, from any channel. Later answers, and answers to unknown or finished requests,
   are ignored and logged.
3. Each request id is single-use (a random uuid4).
4. No answer in time means denied (outcome "expired", by "timeout").
5. Every decision is published (confirm_result) and written to the audit log with who decided.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.core.events import EventBus
from jarvis.core.logging_setup import log_audit

logger = logging.getLogger(__name__)

VOICE_TIMEOUT_SECONDS = 15.0  # docs/ui-protocol.md: expires_in_s
TEXT_TIMEOUT_SECONDS = 60.0  # typing an answer takes longer than saying it
CHANNELS = frozenset({"voice", "screen", "terminal"})


@dataclass
class Approval:
    request_id: str
    question: str
    action: str
    tier: str
    expires_in_s: float
    deadline: float  # time.monotonic()
    outcome: str | None = None  # "approved" | "denied" | "expired"
    by: str | None = None  # "voice" | "screen" | "terminal" | "timeout"
    _decided: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def decided(self) -> bool:
        return self._decided.is_set()

    @property
    def approved(self) -> bool:
        return self.outcome == "approved"

    def wait(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a decision; True once decided."""
        return self._decided.wait(timeout)

    def seconds_left(self, now: float | None = None) -> float:
        return max(0.0, self.deadline - (time.monotonic() if now is None else now))


def action_label(tool_name: str) -> str:
    """'open_file' -> 'Open file' (shown on the HUD's approval dialog)."""
    return tool_name.replace("_", " ").strip().capitalize() or "Action"


class Approvals:
    def __init__(self, bus: EventBus | None = None, audit_sink: Callable[[dict[str, Any]], None] = log_audit,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus or EventBus()
        self._audit = audit_sink
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, Approval] = {}

    def open(self, question: str, action: str = "", tier: str = "CONFIRM",
             timeout: float = VOICE_TIMEOUT_SECONDS) -> Approval:
        approval = Approval(str(uuid.uuid4()), question, action_label(action), tier, timeout, self._clock() + timeout)
        with self._lock:
            self._pending[approval.request_id] = approval
        self._bus.confirm_request(approval.request_id, approval.action, approval.question, tier, timeout)
        return approval

    def answer(self, request_id: str, approved: bool, by: str) -> bool:
        """Record an answer. True if it decided the request; False if it was ignored (already decided,
        unknown, or expired)."""
        if by not in CHANNELS:
            raise ValueError(f"unknown channel: {by}")
        with self._lock:
            approval = self._pending.get(request_id)
            if approval is None:
                ignored = True
            elif self._clock() >= approval.deadline:
                ignored = True  # too late: expire() settles it as denied
            else:
                ignored = False
                del self._pending[request_id]
        if ignored:
            logger.info("Approval answer ignored (by %s): no such open request", by)
            self._audit({"approval": request_id, "status": "ignored", "by": by})
            if approval is not None:
                self.expire(approval)
            return False
        self._settle(approval, "approved" if approved else "denied", by)
        return True

    def expire(self, approval: Approval) -> None:
        """No answer in time: denied. Harmless if it was already decided."""
        with self._lock:
            if self._pending.pop(approval.request_id, None) is None:
                return
        self._settle(approval, "expired", "timeout")

    def pending(self) -> list[Approval]:
        with self._lock:
            return list(self._pending.values())

    def _settle(self, approval: Approval, outcome: str, by: str) -> None:
        approval.outcome, approval.by = outcome, by
        approval._decided.set()
        self._audit({"approval": approval.request_id, "action": approval.action, "outcome": outcome, "by": by})
        self._bus.confirm_result(approval.request_id, outcome, by)
