"""Extract plain text from supported files. The only module that imports `pypdf`."""

from __future__ import annotations

import logging
import posixpath
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from pypdf import PdfReader

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv"})
PDF_EXTENSIONS = frozenset({".pdf"})
EPUB_EXTENSIONS = frozenset({".epub"})
READABLE_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS
# Bump when extraction improves: the file index then re-reads every file once.
EXTRACTOR_VERSION = 2

MAX_TEXT_FILE_BYTES = 2_000_000
MAX_PDF_BYTES = 25_000_000
MAX_PDF_PAGES = 300
MAX_PAGE_CONTENT_BYTES = 5_000_000  # pypdf docs: huge content streams can need a lot of memory
MAX_CHARS = 200_000
MAX_EPUB_BYTES = 50_000_000
MAX_EPUB_UNPACKED_BYTES = 30_000_000  # total of chapter files, checked before unpacking (zip bombs)
MAX_EPUB_XML_BYTES = 1_000_000  # container.xml / package file


def extract_text(path: Path) -> str | None:
    """Plain text of a supported file, or None (unsupported, too large, encrypted, or unreadable).

    Scanned PDFs (images only) return little or no text: pypdf is not OCR software.
    """
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
        if ext in TEXT_EXTENSIONS and size <= MAX_TEXT_FILE_BYTES:
            return path.read_bytes().decode("utf-8-sig", errors="replace")[:MAX_CHARS]
        if ext in PDF_EXTENSIONS and size <= MAX_PDF_BYTES:
            return _pdf_text(path)
        if ext in EPUB_EXTENSIONS and size <= MAX_EPUB_BYTES:
            return _epub_text(path)
    except Exception as e:
        logger.info("No text from %s: %s", path.name, type(e).__name__)
    return None


def _pdf_text(path: Path) -> str | None:
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")  # many "encrypted" PDFs only restrict editing and open with an empty password
        except Exception:
            return None
    parts: list[str] = []
    length = 0
    for page in reader.pages[:MAX_PDF_PAGES]:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_PAGE_CONTENT_BYTES:
            continue
        text = page.extract_text() or ""
        parts.append(text)
        length += len(text)
        if length >= MAX_CHARS:
            break
    text = "\n".join(parts).strip()
    return text[:MAX_CHARS] or None


# --- EPUB (a ZIP of XHTML chapters; standard library only) --------------------------------------

_OPF = "{http://www.idpf.org/2007/opf}"
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_HTML_TYPES = ("application/xhtml+xml", "text/html")


def _epub_text(path: Path) -> str | None:
    with zipfile.ZipFile(path) as book:
        chapters = _epub_reading_order(book)
        if sum(book.getinfo(name).file_size for name in chapters) > MAX_EPUB_UNPACKED_BYTES:
            logger.info("EPUB %s skipped: unpacked chapters too large", path.name)
            return None
        parts: list[str] = []
        length = 0
        for name in chapters:
            text = html_to_text(book.read(name).decode("utf-8", errors="replace"))
            parts.append(text)
            length += len(text)
            if length >= MAX_CHARS:
                break
    text = "\n\n".join(p for p in parts if p).strip()
    return text[:MAX_CHARS] or None


def _epub_reading_order(book: zipfile.ZipFile) -> list[str]:
    """Chapter files in the book's reading order (its spine); file-name order if that can't be read."""
    names = set(book.namelist())
    try:
        container = _small_xml(book, "META-INF/container.xml")
        rootfile = container.find(f".//{_CONTAINER}rootfile")
        opf_path = rootfile.get("full-path", "") if rootfile is not None else ""
        package = _small_xml(book, opf_path)
        base = posixpath.dirname(opf_path)
        manifest = {item.get("id"): item for item in package.iter(f"{_OPF}item")}
        order = []
        for ref in package.iter(f"{_OPF}itemref"):
            item = manifest.get(ref.get("idref"))
            if item is None or item.get("media-type") not in _HTML_TYPES:
                continue
            name = posixpath.normpath(posixpath.join(base, unquote(item.get("href", ""))))
            if name in names:
                order.append(name)
        if order:
            return order
    except (KeyError, ElementTree.ParseError, ValueError):
        pass
    return sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))


def _small_xml(book: zipfile.ZipFile, name: str) -> ElementTree.Element:
    if book.getinfo(name).file_size > MAX_EPUB_XML_BYTES:
        raise ValueError(f"{name} too large")
    return ElementTree.fromstring(book.read(name))


class _TextOnly(HTMLParser):
    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote"})
    _SKIP = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document (scripts, styles and head skipped)."""
    parser = _TextOnly()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)

