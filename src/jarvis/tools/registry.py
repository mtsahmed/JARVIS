"""Runs tool calls from the model: enforces permission tiers and writes every call to the audit log."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from jarvis.core.logging_setup import log_audit
from jarvis.llm.base import ToolSpec
from jarvis.tools.base import Confirmer, Tier, Tool, ToolError

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000  # keeps tool output (and token cost) bounded
_SECRET_NAME = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)

DECLINED = "Cancelled: the user just answered no, so nothing was changed. Say briefly that you did not do it."


class DenyAll:
    """Default confirmer: nothing needing approval ever runs."""

    def confirm(self, question: str) -> bool:
        return False


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool], audit_sink: Callable[[dict[str, Any]], None] = log_audit) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._audit = audit_sink

    def specs(self) -> list[ToolSpec]:
        """Tools offered to the model. BLOCKED tools are never offered."""
        return [ToolSpec(t.name, t.description, t.parameters) for t in self._tools.values() if t.tier is not Tier.BLOCKED]

    def run(self, name: str, arguments: str, confirmer: Confirmer | None = None) -> str:
        """Execute one tool call and return text for the model. Never raises."""
        started = time.perf_counter()
        tool = self._tools.get(name)
        entry: dict[str, Any] = {"tool": name, "tier": tool.tier.value if tool else None}

        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return self._finish(entry, started, "rejected", "error", "Error: the tool arguments were not valid JSON.")
        entry["args"] = _redact(args)

        if tool is None:
            return self._finish(entry, started, "rejected", "error", f"Error: there is no tool named {name!r}.")
        if tool.tier is Tier.BLOCKED:
            return self._finish(entry, started, "blocked", "blocked", "Blocked: this action is not allowed.")
        if tool.check and (reason := tool.check(args)):
            return self._finish(entry, started, "blocked", "blocked", f"Blocked: {reason}")

        decision = "auto"
        if tool.tier is Tier.CONFIRM:
            assert tool.describe is not None  # enforced by Tool
            approved = (confirmer or DenyAll()).confirm(tool.describe(args))
            decision = "approved" if approved else "denied"
            if not approved:
                return self._finish(entry, started, decision, "denied", DECLINED)

        try:
            result = tool.handler(args)
            status = "ok"
        except ToolError as e:
            result, status = f"Error: {e}", "error"
        except Exception:
            logger.exception("Tool %s failed unexpectedly", name)
            result, status = "Error: the tool failed unexpectedly.", "error"
        return self._finish(entry, started, decision, status, result)

    def _finish(self, entry: dict[str, Any], started: float, decision: str, status: str, result: str) -> str:
        # The result text itself is not logged (it may contain personal data).
        self._audit({**entry, "decision": decision, "status": status,
                     "duration_ms": round((time.perf_counter() - started) * 1000)})
        return result[:MAX_RESULT_CHARS]


def _redact(value: Any, key: str = "") -> Any:
    """Hide values of secret-looking argument names (known secret values are also redacted by the log filter)."""
    if key and _SECRET_NAME.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
