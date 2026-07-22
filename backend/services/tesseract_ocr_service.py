from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)

TESSERACT_DPI = 200
TESSERACT_CONFIG = "--psm 6"

# Without the Portuguese ("por") Tesseract language pack installed, the
# English model frequently misreads accented characters — most importantly
# "Período" (the header line that tells the parser which month/year the page
# belongs to) commonly comes back as "Perfodo", "Perlodo", etc. The shared
# PERIOD_RE in frequency_cycle_service requires the literal word "Periodo"/
# "Período", so a misread silently drops every day row on that page (no
# current_year/current_month means DATE_ROW_RE matches are discarded).
# This loosely matches the header regardless of how the accented "í" OCRs,
# and injects a clean "Periodo DD/MM/YYYY" line the strict parser can read.
_PERIOD_LOOSE_RE = re.compile(
    r"per[a-zí]{0,4}odo.{0,20}?(\d{2}[./]\d{2}[./]\d{4})",
    re.IGNORECASE,
)


def _normalize_ocr_text(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        match = _PERIOD_LOOSE_RE.search(line)
        if match:
            out.append(f"Periodo {match.group(1)}")
        out.append(line)
    return "\n".join(out)


class TesseractOCRError(Exception):
    pass


def _configure_tesseract_cmd() -> None:
    """Point pytesseract at an explicit binary path if TESSERACT_CMD is set.

    Handles the common Windows case where Tesseract is installed but its
    install directory was never added to the system PATH (the installer's
    "Add to PATH" step is easy to miss, and PATH changes require a fresh
    terminal anyway). No-op if TESSERACT_CMD is unset — PATH resolution is
    the default and is what the Docker/Fly.io image relies on.
    """
    try:
        from config import settings

        cmd = getattr(settings, "TESSERACT_CMD", "") or ""
    except Exception:
        cmd = ""

    if cmd:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = cmd


def is_tesseract_available() -> bool:
    """Check whether the Tesseract binary is installed and reachable.

    This does not require network access or an API key — it only checks that
    the local `tesseract` executable (installed separately by the user, e.g.
    via https://github.com/UB-Mannheim/tesseract/wiki on Windows) is on PATH,
    or configured explicitly via the TESSERACT_CMD environment variable.
    """
    try:
        import pytesseract

        _configure_tesseract_cmd()
        pytesseract.get_tesseract_version()
        return True
    except Exception as e:
        logger.debug("Tesseract binary not available: %s", e)
        return False


def _pick_languages() -> str:
    """Prefer Portuguese+English, but fall back to whatever is installed.

    Petrobras frequency reports are in Portuguese, but the `por` language
    pack is a separate download or Windows install option and may be
    missing. Tesseract's English model still reads digits/times/dates
    reliably, which is most of what these tables contain.
    """
    import pytesseract

    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception as e:
        logger.debug("Could not list Tesseract languages, defaulting to 'eng': %s", e)
        return "eng"

    langs = [lang for lang in ("por", "eng") if lang in available]
    if not langs:
        logger.warning(
            "Neither 'por' nor 'eng' Tesseract language data found (available=%s); "
            "OCR quality may be degraded.",
            sorted(available),
        )
        return "eng"
    return "+".join(langs)


def _render_pdf_pages(pdf_bytes: bytes, dpi: int = TESSERACT_DPI):
    """Render every page of a PDF to a PIL Image using PyMuPDF (fitz).

    PyMuPDF bundles its own PDF renderer (no Poppler/pdftoppm install
    required on the host), which keeps the Windows setup to just:
    `pip install PyMuPDF pytesseract Pillow` + installing the Tesseract-OCR
    binary itself.
    """
    import fitz  # PyMuPDF
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    images = []
    try:
        for page_index in range(len(doc)):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(image)
    finally:
        doc.close()
    return images


def ocr_pdf_page_texts(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """OCR every page of a PDF and return (1-based page index, text) tuples.

    The output shape matches what `pypdf`/`pdfplumber` extraction produces
    elsewhere in this codebase (`list[tuple[int, str]]` of page number and
    page text), so it can be fed straight into the same parsing/classification
    logic used for text-extractable PDFs.
    """
    if not is_tesseract_available():
        raise TesseractOCRError(
            "Tesseract OCR nao encontrado no sistema. Instale o Tesseract-OCR "
            "(https://github.com/UB-Mannheim/tesseract/wiki) e garanta que o "
            "executavel esteja no PATH, ou configure "
            "pytesseract.pytesseract.tesseract_cmd apontando para tesseract.exe."
        )

    import pytesseract

    lang = _pick_languages()
    images = _render_pdf_pages(pdf_bytes)
    page_texts: list[tuple[int, str]] = []
    for page_index, image in enumerate(images, start=1):
        try:
            text = pytesseract.image_to_string(image, lang=lang, config=TESSERACT_CONFIG)
        except pytesseract.TesseractError as e:
            logger.warning("Tesseract OCR failed on page %d: %s", page_index, e)
            text = ""
        page_texts.append((page_index, _normalize_ocr_text(text)))
    return page_texts


def extract_frequency_days_tesseract(pdf_bytes: bytes) -> list:
    """OCR the PDF locally with Tesseract and parse it with the exact same
    line-parsing regexes used for pdfplumber-extracted text.

    Reusing `_extract_frequency_days_from_page_texts` guarantees the output
    (`list[FrequencyDay]`) is structurally identical to the pdfplumber path,
    so everything downstream (classification, Excel/CSV building) behaves
    the same regardless of which extraction method supplied the rows.
    """
    from services.frequency_cycle_service import _extract_frequency_days_from_page_texts

    page_texts = ocr_pdf_page_texts(pdf_bytes)
    return _extract_frequency_days_from_page_texts(page_texts)


def extract_frequency_day_texts_for_pages(
    pdf_bytes: bytes, page_numbers: Iterable[int]
) -> list[tuple[int, str]]:
    """OCR only the given 1-based page numbers of a PDF.

    Useful when the caller already knows which pages need OCR (e.g. a chunk
    built out of pages the pdfplumber pass flagged) and wants to avoid
    re-rendering the whole document.
    """
    wanted = set(page_numbers)
    all_texts = ocr_pdf_page_texts(pdf_bytes)
    return [(idx, text) for idx, text in all_texts if idx in wanted]


def extract_timesheet_rows_tesseract(pdf_bytes: bytes) -> list:
    """OCR the PDF locally with Tesseract and parse it with the same
    generic timesheet parsers `pdfplumber_service` uses for native text
    (fixed-width "DD/MM/YYYY WEEKDAY ..." rows, and merged multi-row cells).

    Returns `list[TimesheetRow]`, matching the shape `extract_with_pdfplumber`
    produces, so the rest of the pipeline (aggregation, Excel/CSV building)
    behaves identically regardless of which extraction method supplied rows.
    """
    from services.pdfplumber_service import (
        _MULTIROW_DATE_RE,
        _parse_multirow_cell,
        _parse_text_rows,
    )

    page_texts = ocr_pdf_page_texts(pdf_bytes)
    full_text = "\n".join(text for _, text in page_texts)

    rows = _parse_text_rows(full_text)
    if rows:
        return rows

    multirow_rows = []
    for _, text in page_texts:
        for line in text.split("\n"):
            if _MULTIROW_DATE_RE.search(line):
                multirow_rows.extend(_parse_multirow_cell(line))
    return multirow_rows


def extract_guia_records_tesseract(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """OCR the PDF locally and return raw (1-based page index, text) tuples
    for the Guia Ministerial flow, which parses free-form field labels
    ("hora entrada", "hora saída", etc.) rather than a fixed table format.

    Note: Guia Ministerial documents are frequently handwritten field forms.
    Tesseract is a printed-text OCR engine and does not read handwriting
    reliably — this path works well for typed/printed guias but will likely
    perform worse than a vision-capable model on handwritten ones.
    """
    return ocr_pdf_page_texts(pdf_bytes)
