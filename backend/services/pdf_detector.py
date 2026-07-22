from __future__ import annotations
import io
import re
from typing import Literal

import pdfplumber
import pypdf

_CID_RE = re.compile(r"\(cid:\d+\)")


def has_meaningful_text(text: str) -> bool:
    """True when `text` looks like real extracted content.

    False for both "no text" and for non-empty-but-meaningless text, which
    happens when a PDF uses an obfuscated/custom font encoding (no ToUnicode
    CMap) — tools like pypdf/pdfplumber then decode glyphs into placeholders
    such as "/0 /1 /2 ..." or "(cid:12)(cid:7)..." instead of real characters.
    """
    text = text.strip()
    if len(text) <= 50:
        return False
    cid_chars = sum(len(match.group(0)) for match in _CID_RE.finditer(text))
    if cid_chars / max(len(text), 1) > 0.25:
        return False
    alnum_chars = sum(1 for char in text if char.isalnum())
    return alnum_chars / max(len(text), 1) > 0.25


# Backwards-compatible alias (some modules imported the old private name).
_has_meaningful_text = has_meaningful_text


def page_has_raster_image(reader: pypdf.PdfReader, page_index: int) -> bool:
    """True when the page is visually rendered via an embedded raster image
    or otherwise has non-trivial drawing content — i.e. a human looking at it
    would see something, even if pypdf can't extract meaningful text from it.
    """
    if not 0 <= page_index < len(reader.pages):
        return False
    page = reader.pages[page_index]
    try:
        if any(True for _ in page.images):
            return True
    except Exception:
        pass
    try:
        content = page.get_contents()
        if content is None:
            return False
        raw = content.get_data() if hasattr(content, "get_data") else b""
        return len(raw) > 200
    except Exception:
        return False


def detect_garbled_pages(pdf_bytes: bytes) -> list[int]:
    """Return 0-based indices of pages that need OCR because their extracted
    text is not meaningful, even though the page is visually rendered.

    This specifically catches "encrypted"/obfuscated-font PDFs (e.g. produced
    by tools like Doro PDF Writer) where pypdf/pdfplumber return non-empty
    text that decodes to meaningless glyph placeholders. Because the text is
    non-empty, naive "page has no text -> scanned" checks miss these pages
    entirely; this function checks the *content* of the text, not just its
    presence, and cross-checks against whether the page actually renders a
    raster image (to avoid flagging genuinely blank/unrelated pages).
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return []

    garbled_indices: list[int] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""

            if not text.strip():
                continue  # truly empty text is handled by the scanned-PDF path
            if has_meaningful_text(text):
                continue  # real, readable content — nothing to do

            if page_has_raster_image(reader, i):
                garbled_indices.append(i)

    return garbled_indices


def detect_pdf_type(pdf_bytes: bytes) -> Literal["native", "scanned", "mixed"]:
    pdf = None
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        total_pages = len(pdf.pages)
        pages_to_check = min(5, total_pages)
        text_pages = 0
        for i in range(pages_to_check):
            text = pdf.pages[i].extract_text() or ""
            if has_meaningful_text(text):
                text_pages += 1
        ratio = text_pages / pages_to_check if pages_to_check > 0 else 0.0
        if ratio >= 0.8:
            return "native"
        elif ratio <= 0.2:
            return "scanned"
        else:
            return "mixed"
    finally:
        if pdf is not None:
            pdf.close()
