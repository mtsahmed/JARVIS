"""Memory tools: remember (only when the user asks), list, and forget (asks every time)."""

from __future__ import annotations

import re
from typing import Any

from jarvis.memory.store import MAX_TEXT_CHARS, MemoryStore
from jarvis.tools.base import Tier, Tool, ToolError

# The user's own words must ask to remember. This stops text inside a file or web page from planting a
# "memory" that would keep influencing Jarvis in later sessions (persistent prompt injection).
_REMEMBER_REQUEST = re.compile(
    r"\b(remember|don'?t forget|do not forget|note that|make a note|keep in mind|memori[sz]e|save (this|that))\b",
    re.IGNORECASE,
)


def asks_to_remember(user_text: str) -> bool:
    return bool(_REMEMBER_REQUEST.search(user_text))


def memory_tools(store: MemoryStore) -> list[Tool]:
    def check_remember(args: dict[str, Any]) -> str | None:
        if not asks_to_remember(store.current_request):
            return "facts are only saved when the user explicitly asks you to remember something"
        return None

    def remember(args: dict[str, Any]) -> str:
        fact = str(args["fact"]).strip()
        if not fact:
            raise ToolError("There is nothing to remember.")
        if len(fact) > MAX_TEXT_CHARS:
            raise ToolError(f"That is too long to remember (max {MAX_TEXT_CHARS} characters); keep it to one fact.")
        memory, replaced = store.add(fact)
        if replaced is not None:
            return f"Already remembered as memory {memory.id}: '{memory.text}'."
        result = f"Saved memory {memory.id}: '{memory.text}'. Tell the user briefly what you will remember."
        if similar := store.similar_to(memory):
            olds = "; ".join(f"#{m.id}: '{m.text}'" for m in similar)
            result += (f" Similar older memories: {olds}. If the new fact replaces one of them, ask the user "
                       "whether to forget the old one (use forget_memory).")
        return result

    def list_all(args: dict[str, Any]) -> str:
        memories = store.all()
        if not memories:
            return "No memories are saved."
        return "Saved memories:\n" + "\n".join(f"- #{m.id} ({m.saved:%d %b %Y}): {m.text}" for m in memories)

    def describe_forget(args: dict[str, Any]) -> str:
        memory = store.get(int(args["id"]))
        return f"Forget '{memory.text}'?" if memory else f"Forget memory number {args['id']}?"

    def forget(args: dict[str, Any]) -> str:
        if not store.delete(int(args["id"])):
            raise ToolError(f"There is no memory number {args['id']}. List memories to find the right one.")
        return f"Memory {args['id']} deleted."

    return [
        Tool(
            "remember",
            "Save one fact about the user for future conversations. Only use this when the user explicitly asks "
            "you to remember something. Write the fact in third person, self-contained, e.g. "
            "\"The user's car is a 2021 Toyota Corolla.\"",
            {"type": "object",
             "properties": {"fact": {"type": "string", "description": "One short, self-contained fact."}},
             "required": ["fact"], "additionalProperties": False},
            Tier.AUTO,
            remember,
            check=check_remember,
            private_args=frozenset({"fact"}),  # "forget" must really forget: no copy in the audit log
        ),
        Tool(
            "list_memories",
            "List everything the user asked you to remember, with numbers.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            Tier.AUTO,
            list_all,
        ),
        Tool(
            "forget_memory",
            "Delete one saved memory by its number (list memories first if you don't know it).",
            {"type": "object",
             "properties": {"id": {"type": "integer", "description": "The memory number."}},
             "required": ["id"], "additionalProperties": False},
            Tier.CONFIRM,  # asked every time: no remember_key
            forget,
            describe=describe_forget,
        ),
    ]
