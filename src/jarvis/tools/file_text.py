"""Extract plain text from supported files. The only module that imports `pypdf`."""

from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv"})
PDF_EXTENSIONS = frozenset({".pdf"})
READABLE_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS

MAX_TEXT_FILE_BYTES = 2_000_000
MAX_PDF_BYTES = 25_000_000
MAX_PDF_PAGES = 300
MAX_PAGE_CONTENT_BYTES = 5_000_000  # pypdf docs: huge content streams can need a lot of memory
MAX_CHARS = 200_000


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
