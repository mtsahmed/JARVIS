import json
from typing import Any

import pytest

from jarvis.tools.base import Tier, Tool, ToolError
from jarvis.tools.registry import DECLINED, MAX_RESULT_CHARS, ToolRegistry

NO_ARGS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
PATH_ARGS = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
             "additionalProperties": False}


class Answer:
    def __init__(self, yes: bool) -> None:
        self.yes, self.asked = yes, []

    def confirm(self, question: str) -> bool:
        self.asked.append(question)
        return self.yes


def make(*tools: Tool) -> tuple[ToolRegistry, list[dict[str, Any]]]:
    audit: list[dict[str, Any]] = []
    return ToolRegistry(tools, audit_sink=audit.append), audit


def auto(handler=lambda a: "done", **kw) -> Tool:  # type: ignore[no-untyped-def]
    return Tool("auto_tool", "An auto tool", kw.pop("parameters", NO_ARGS), Tier.AUTO, handler, **kw)


def confirm_tool(ran: list[str]) -> Tool:
    return Tool("open_file", "Open a file", PATH_ARGS, Tier.CONFIRM,
                handler=lambda a: ran.append(a["path"]) or "opened", describe=lambda a: f"Open {a['path']}?")


# --- tiers -------------------------------------------------------------------------------------


def test_auto_tool_runs_and_is_audited() -> None:
    registry, audit = make(auto())
    assert registry.run("auto_tool", "{}") == "done"
    assert audit[0]["tool"] == "auto_tool" and audit[0]["decision"] == "auto" and audit[0]["status"] == "ok"
    assert audit[0]["args"] == {} and isinstance(audit[0]["duration_ms"], int)


def test_confirm_tool_runs_only_after_yes() -> None:
    ran: list[str] = []
    registry, audit = make(confirm_tool(ran))
    answer = Answer(yes=True)
    assert registry.run("open_file", json.dumps({"path": "a.txt"}), answer) == "opened"
    assert answer.asked == ["Open a.txt?"] and ran == ["a.txt"]
    assert audit[0]["decision"] == "approved"


def test_confirm_tool_declined_does_not_run() -> None:
    ran: list[str] = []
    registry, audit = make(confirm_tool(ran))
    assert registry.run("open_file", json.dumps({"path": "a.txt"}), Answer(yes=False)) == DECLINED
    assert ran == [] and audit[0]["decision"] == "denied" and audit[0]["status"] == "denied"


def test_confirm_tool_without_confirmer_is_declined() -> None:
    ran: list[str] = []
    registry, _ = make(confirm_tool(ran))
    assert registry.run("open_file", json.dumps({"path": "a.txt"})) == DECLINED
    assert ran == []


def test_blocked_tool_never_runs_or_offered() -> None:
    ran: list[str] = []
    blocked = Tool("shell", "Run a command", NO_ARGS, Tier.BLOCKED, handler=lambda a: ran.append("x") or "")
    registry, audit = make(blocked, auto())
    assert [s.name for s in registry.specs()] == ["auto_tool"]
    assert registry.run("shell", "{}").startswith("Blocked")
    assert ran == [] and audit[0]["decision"] == "blocked"


def test_per_call_check_blocks_before_asking() -> None:
    ran: list[str] = []
    tool = Tool("open_file", "Open", PATH_ARGS, Tier.CONFIRM, handler=lambda a: ran.append("x") or "",
                describe=lambda a: "Open?", check=lambda a: "outside Documents" if a["path"].startswith("C:\\Windows") else None)
    registry, audit = make(tool)
    answer = Answer(yes=True)
    result = registry.run("open_file", json.dumps({"path": "C:\\Windows\\evil.exe"}), answer)
    assert result == "Blocked: outside Documents"
    assert answer.asked == [] and ran == [] and audit[0]["decision"] == "blocked"


# --- errors --------------------------------------------------------------------------------------


def test_unknown_tool_and_bad_json_are_errors_not_crashes() -> None:
    registry, audit = make(auto())
    assert "no tool named" in registry.run("nope", "{}")
    assert "not valid JSON" in registry.run("auto_tool", "{broken")
    assert [a["status"] for a in audit] == ["error", "error"]


def test_tool_error_message_returned() -> None:
    def fail(args: dict[str, Any]) -> str:
        raise ToolError("reminder not found")

    registry, audit = make(auto(handler=fail))
    assert registry.run("auto_tool", "{}") == "Error: reminder not found"
    assert audit[0]["status"] == "error"


def test_unexpected_exception_is_contained() -> None:
    registry, _ = make(auto(handler=lambda a: 1 / 0))
    assert registry.run("auto_tool", "{}") == "Error: the tool failed unexpectedly."


def test_long_result_truncated() -> None:
    registry, _ = make(auto(handler=lambda a: "x" * (MAX_RESULT_CHARS + 500)))
    assert len(registry.run("auto_tool", "{}")) == MAX_RESULT_CHARS


# --- audit privacy -------------------------------------------------------------------------------


def test_audit_redacts_secret_arguments_and_omits_results() -> None:
    params = {"type": "object", "properties": {"query": {"type": "string"}, "api_key": {"type": "string"}},
              "required": ["query", "api_key"], "additionalProperties": False}
    registry, audit = make(auto(handler=lambda a: "private result text", parameters=params))
    registry.run("auto_tool", json.dumps({"query": "weather", "api_key": "sk-123"}))
    assert audit[0]["args"] == {"query": "weather", "api_key": "[REDACTED]"}
    assert "private result text" not in json.dumps(audit)


# --- tool definitions ----------------------------------------------------------------------------


def test_confirm_tool_requires_read_back() -> None:
    with pytest.raises(ValueError, match="describe"):
        Tool("x", "x", NO_ARGS, Tier.CONFIRM, handler=lambda a: "")


def test_schema_must_be_strict() -> None:
    loose = {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False}
    with pytest.raises(ValueError, match="required"):
        Tool("x", "x", loose, Tier.AUTO, handler=lambda a: "")
    with pytest.raises(ValueError, match="additionalProperties"):
        Tool("x", "x", {"type": "object", "properties": {}, "required": []}, Tier.AUTO, handler=lambda a: "")


# --- approvals remembered per chat session -----------------------------------------------------------


def remembering_tool(ran: list[str]) -> Tool:
    return Tool("read_file", "Read a file", PATH_ARGS, Tier.CONFIRM,
                handler=lambda a: ran.append(a["path"]) or "text",
                describe=lambda a: f"Read {a['path']}?", remember_key=lambda a: a["path"].lower())


def test_yes_remembered_for_same_file_only() -> None:
    ran: list[str] = []
    registry, audit = make(remembering_tool(ran))
    answer = Answer(yes=True)
    for path in ("a.pdf", "A.PDF", "a.pdf", "b.pdf"):
        registry.run("read_file", json.dumps({"path": path}), answer)
    assert answer.asked == ["Read a.pdf?", "Read b.pdf?"]  # asked once per file
    assert len(ran) == 4
    assert [a["decision"] for a in audit] == ["approved", "remembered", "remembered", "approved"]


def test_no_is_not_remembered() -> None:
    ran: list[str] = []
    registry, _ = make(remembering_tool(ran))
    no = Answer(yes=False)
    registry.run("read_file", json.dumps({"path": "a.pdf"}), no)
    registry.run("read_file", json.dumps({"path": "a.pdf"}), no)
    assert len(no.asked) == 2 and ran == []


def test_forget_approvals_asks_again() -> None:
    ran: list[str] = []
    registry, _ = make(remembering_tool(ran))
    answer = Answer(yes=True)
    registry.run("read_file", json.dumps({"path": "a.pdf"}), answer)
    registry.forget_approvals()
    registry.run("read_file", json.dumps({"path": "a.pdf"}), answer)
    assert len(answer.asked) == 2


def test_tools_without_remember_key_always_ask() -> None:
    ran: list[str] = []
    registry, _ = make(confirm_tool(ran))
    answer = Answer(yes=True)
    registry.run("open_file", json.dumps({"path": "a.txt"}), answer)
    registry.run("open_file", json.dumps({"path": "a.txt"}), answer)
    assert len(answer.asked) == 2
