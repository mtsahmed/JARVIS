import json
import os
from pathlib import Path
from typing import Any

import pytest

import jarvis.tools.files as files_module
from jarvis.tools.file_text import extract_text
from jarvis.tools.files import READ_CHARS, FileIndex, _relevant_excerpt, file_tools
from jarvis.tools.registry import DECLINED, ToolRegistry


def make_pdf(path: Path, text: str) -> None:
    """Write a minimal one-page PDF with a text layer (valid xref), for extraction tests."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


@pytest.fixture
def docs(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "Documents"
    (root / "Finance").mkdir(parents=True)
    (root / "Finance" / "budget.csv").write_text("item,amount\nrent,1200\ngroceries,300\n", encoding="utf-8")
    make_pdf(root / "car-insurance_2025.pdf", "Glass damage is covered up to 500 dollars")
    (root / "notes.md").write_text("Ideas for the garden: tomatoes and basil.", encoding="utf-8")
    (root / "setup.exe").write_bytes(b"MZ")
    # never indexed or usable:
    (root / ".hidden").mkdir()
    (root / ".hidden" / "passwords.txt").write_text("insurance password hunter2", encoding="utf-8")
    (root / "app" / "node_modules" / "lib").mkdir(parents=True)
    (root / "app" / "node_modules" / "lib" / "insurance.txt").write_text("insurance", encoding="utf-8")
    (root / "Jarvis").mkdir()
    (root / "Jarvis" / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    (root / "Jarvis" / "insurance_notes.txt").write_text("insurance", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("insurance outside", encoding="utf-8")
    return {"root": root, "project": root / "Jarvis", "outside": outside, "db": tmp_path / "data" / "files.db"}


def build(docs: dict[str, Path]) -> FileIndex:
    index = FileIndex(docs["db"], (docs["root"],), excluded=(docs["project"],))
    index.refresh()
    return index


# --- extraction ----------------------------------------------------------------------------------


def test_pdf_and_text_extraction(docs: dict[str, Path]) -> None:
    assert "Glass damage is covered" in (extract_text(docs["root"] / "car-insurance_2025.pdf") or "")
    assert "rent,1200" in (extract_text(docs["root"] / "Finance" / "budget.csv") or "")
    assert extract_text(docs["root"] / "setup.exe") is None


# --- index and search ----------------------------------------------------------------------------


def test_search_by_name_and_content(docs: dict[str, Path]) -> None:
    index = build(docs)
    assert [h.name for h in index.search("car insurance")] == ["car-insurance_2025.pdf"]
    assert [h.name for h in index.search("glass damage")] == ["car-insurance_2025.pdf"]  # PDF text
    assert [h.name for h in index.search("find my budget file")] == ["budget.csv"]  # filler words ignored
    assert [h.name for h in index.search("groceries")] == ["budget.csv"]  # CSV text
    assert index.search("budget", limit=5)[0].folder == str(Path("Documents") / "Finance")


def test_any_word_fallback(docs: dict[str, Path]) -> None:
    assert [h.name for h in build(docs).search("tomatoes spaceship")] == ["notes.md"]


def test_hidden_developer_and_project_files_never_indexed(docs: dict[str, Path]) -> None:
    index = build(docs)
    for query in ("insurance", "password", "sk-secret", "env"):
        names = [h.name for h in index.search(query)]
        assert "passwords.txt" not in names and "insurance.txt" not in names
        assert "insurance_notes.txt" not in names and ".env" not in names
    assert index.search("hunter2") == []


def test_refresh_is_incremental(docs: dict[str, Path]) -> None:
    index = build(docs)
    assert index.refresh() == (0, 0)  # nothing changed
    notes = docs["root"] / "notes.md"
    notes.write_text("Now about cucumbers.", encoding="utf-8")
    os.utime(notes, (notes.stat().st_atime, notes.stat().st_mtime + 5))
    (docs["root"] / "Finance" / "budget.csv").unlink()
    assert index.refresh() == (1, 1)
    assert [h.name for h in index.search("cucumbers")] == ["notes.md"]
    assert index.search("groceries") == []


def test_refusal_rules(docs: dict[str, Path]) -> None:
    index = build(docs)
    root = docs["root"]
    assert index.refusal(root / "notes.md") is None
    assert "outside" in (index.refusal(docs["outside"]) or "")
    assert "outside" in (index.refusal(root / ".." / "outside.txt") or "")  # no ../ escape
    assert "excluded" in (index.refusal(docs["project"] / ".env") or "")
    assert "hidden" in (index.refusal(root / ".hidden" / "passwords.txt") or "")
    assert "does not exist" in (index.refusal(root / "nope.txt") or "")


# --- tools through the registry (tiers + audit) ----------------------------------------------------


class Answer:
    def __init__(self, yes: bool) -> None:
        self.yes, self.asked = yes, []

    def confirm(self, question: str) -> bool:
        self.asked.append(question)
        return self.yes


def setup_tools(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> tuple[ToolRegistry, list[str], list[dict[str, Any]]]:
    opened: list[str] = []
    monkeypatch.setattr(files_module.os, "startfile", lambda p: opened.append(Path(p).name), raising=False)
    audit: list[dict[str, Any]] = []
    return ToolRegistry(file_tools(build(docs)), audit_sink=audit.append), opened, audit


def arg(path: Path, **extra: str) -> str:
    return json.dumps({"path": str(path), **extra})


def test_search_tool_returns_names_not_contents(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _, audit = setup_tools(docs, monkeypatch)
    result = registry.run("search_files", '{"query": "glass damage"}')
    assert "car-insurance_2025.pdf" in result and str(docs["root"]) in result
    assert "covered" not in result  # file text is not sent
    assert audit[0]["decision"] == "auto"


def test_open_needs_yes(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    registry, opened, audit = setup_tools(docs, monkeypatch)
    pdf = docs["root"] / "car-insurance_2025.pdf"
    assert registry.run("open_file", arg(pdf), Answer(False)) == DECLINED and opened == []
    answer = Answer(True)
    assert registry.run("open_file", arg(pdf), answer) == "Opened car-insurance_2025.pdf."
    assert opened == ["car-insurance_2025.pdf"] and answer.asked == ["Open car-insurance_2025.pdf?"]
    assert [a["decision"] for a in audit] == ["denied", "approved"]


def test_programs_and_outside_files_blocked_without_asking(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    registry, opened, audit = setup_tools(docs, monkeypatch)
    answer = Answer(True)
    assert registry.run("open_file", arg(docs["root"] / "setup.exe"), answer).startswith("Blocked: programs")
    assert registry.run("open_file", arg(docs["outside"]), answer).startswith("Blocked: it is outside")
    assert registry.run("read_file", arg(docs["project"] / ".env", focus=""), answer).startswith("Blocked")
    assert opened == [] and answer.asked == []
    assert all(a["decision"] == "blocked" for a in audit)


def test_read_needs_yes_and_marks_text_as_untrusted(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _, _ = setup_tools(docs, monkeypatch)
    pdf = docs["root"] / "car-insurance_2025.pdf"
    assert registry.run("read_file", arg(pdf, focus="glass"), Answer(False)) == DECLINED
    answer = Answer(True)
    result = registry.run("read_file", arg(pdf, focus="glass"), answer)
    assert answer.asked == ["Read car-insurance_2025.pdf and send its text to OpenAI?"]
    assert "Glass damage is covered" in result and "untrusted" in result


def test_read_unsupported_type_is_clear_error(docs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _, _ = setup_tools(docs, monkeypatch)
    (docs["root"] / "photo.jpg").write_bytes(b"\xff\xd8")
    assert "can only read" in registry.run("read_file", arg(docs["root"] / "photo.jpg", focus=""), Answer(True))


def test_excerpt_prefers_focus_chunks() -> None:
    text = ("filler text. " * 400) + "GLASS DAMAGE: covered up to 500. " + ("more filler. " * 400)
    excerpt = _relevant_excerpt(text, "glass damage")
    assert "GLASS DAMAGE" in excerpt and len(excerpt) <= READ_CHARS + 100
