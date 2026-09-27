"""Tool definitions and permission tiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Tier(StrEnum):
    AUTO = "auto"  # runs without asking (read-only, or local and harmless)
    CONFIRM = "confirm"  # runs only after the user explicitly says yes
    BLOCKED = "blocked"  # never runs


class ToolError(Exception):
    """A tool could not do its job. The message is shown to the model (and may be spoken): no secrets."""


class Confirmer(Protocol):
    def confirm(self, question: str) -> bool:
        """Ask the user; True only for an explicit yes. Silence or anything unclear is False."""
        ...


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # what the model reads to decide when to call it
    parameters: dict[str, Any]  # JSON Schema object: every property required, additionalProperties false
    tier: Tier
    handler: Callable[[dict[str, Any]], str]  # returns text for the model; raises ToolError on failure
    # CONFIRM tools: the question read back to the user, e.g. "Open budget.xlsx?"
    describe: Callable[[dict[str, Any]], str] | None = None
    # Optional per-call rule: returns a reason to BLOCK this call (e.g. a path outside approved folders).
    check: Callable[[dict[str, Any]], str | None] | None = None

    def __post_init__(self) -> None:
        if self.tier is Tier.CONFIRM and self.describe is None:
            raise ValueError(f"CONFIRM tool {self.name!r} needs a describe() read-back.")
        schema = self.parameters
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError(f"Tool {self.name!r}: parameters must be an object with additionalProperties false.")
        if set(schema.get("required", [])) != set(schema.get("properties", {})):
            raise ValueError(f"Tool {self.name!r}: every property must be listed in 'required'.")
