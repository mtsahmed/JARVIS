import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

import jarvis.tools.file_text as file_text
from jarvis.tools.file_text import EXTRACTOR_VERSION, extract_text
from jarvis.tools.files import FileIndex, file_tools
from jarvis.tools.registry import ToolRegistry

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

PACKAGE = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="c2" href="Text/b%20chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="Text/z_intro.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""

INTRO = """<html><head><title>Ignore me</title><style>p {color: red}</style></head>
<body><h1>Chapter One</h1><p>The garden&nbsp;needs &amp; loves water.</p>
<script>alert("never included")</script></body></html>"""

SECOND = "<html><body><h1>Chapter Two</h1><p>Tomatoes grow best in full sun.</p></body></html>"


def make_epub(path: Path, with_spine: bool = True) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("mimetype", "application/epub+zip")
        if with_spine:
            book.writestr("META-INF/container.xml", CONTAINER)
            book.writestr("OEBPS/content.opf", PACKAGE)
        # file-name order (b before z) is the opposite of the spine order (intro first)
        book.writestr("OEBPS/Text/b chapter.xhtml", SECOND)
        book.writestr("OEBPS/Text/z_intro.xhtml", INTRO)
        book.writestr("OEBPS/style.css", "p { margin: 0 }")
    return path


def test_text_in_reading_order_without_scripts_or_styles(tmp_path: Path) -> None:
    text = extract_text(make_epub(tmp_path / "garden.epub")) or ""
    assert text.index("Chapter One") < text.index("Chapter Two")  # spine order, not file order
    assert "The garden needs & loves water." in text  # entities decoded, whitespace tidied
    assert "never included" not in text and "color: red" not in text and "Ignore me" not in text


def test_book_without_spine_falls_back_to_file_order(tmp_path: Path) -> None:
    text = extract_text(make_epub(tmp_path / "loose.epub", with_spine=False)) or ""
    assert "Tomatoes grow best" in text and "Chapter One" in text


def test_oversized_chapters_skipped_before_unpacking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_text, "MAX_EPUB_UNPACKED_BYTES", 50)  # stands in for a zip bomb
    assert extract_text(make_epub(tmp_path / "bomb.epub")) is None


def test_broken_epub_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "broken.epub"
    bad.write_bytes(b"not a zip file")
    assert extract_text(bad) is None


def test_epub_searchable_and_readable_with_yes(tmp_path: Path) -> None:
    root = tmp_path / "Files"
    root.mkdir()
    make_epub(root / "garden-guide.epub")
    index = FileIndex(tmp_path / "files.db", (root,))
    index.refresh()
    assert [h.name for h in index.search("tomatoes sun")] == ["garden-guide.epub"]

    class Yes:
        def confirm(self, question: str) -> bool:
            return True

    registry = ToolRegistry(file_tools(index), audit_sink=lambda r: None)
    result = registry.run("read_file", json.dumps({"path": str(root / "garden-guide.epub"), "focus": "tomatoes"}), Yes())
    assert "Tomatoes grow best in full sun." in result and "untrusted" in result


def test_old_index_is_rebuilt_once_after_extractor_upgrade(tmp_path: Path) -> None:
    root = tmp_path / "Files"
    root.mkdir()
    make_epub(root / "garden-guide.epub")
    db_path = tmp_path / "files.db"
    FileIndex(db_path, (root,)).refresh()
    db = sqlite3.connect(db_path)
    with db:  # simulate an index built by the old extractor (EPUB indexed by name only)
        db.execute("UPDATE files_fts SET content = ''")
        db.execute("PRAGMA user_version = 1")
    db.close()

    index = FileIndex(db_path, (root,))
    assert index.refresh() == (1, 0)  # re-read even though the file did not change
    assert [h.name for h in index.search("tomatoes")] == ["garden-guide.epub"]
    assert FileIndex(db_path, (root,)).refresh() == (0, 0)  # only once
    assert sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0] == EXTRACTOR_VERSION
