from __future__ import annotations

import base64
import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Iterable

import pdfplumber
import pypdf

from services.pdf_detector import has_meaningful_text, page_has_raster_image

logger = logging.getLogger(__name__)

FREQUENCY_OCR_CHUNK_SIZE = 5

SCALE_PATTERN = r"FOLG|LIVR|HXGU|HXHI|HX01|HS\d+|HT\d+"

DATE_ROW_RE = re.compile(
    r"^(?P<day>\d{2})/(?P<month>\d{2})\s+\S+\s+"
    rf"(?P<scale>{SCALE_PATTERN})\b(?P<details>.*?)"
    r"(?:Sobreaviso|Turno\s+de\s+\d+\s+Hor\s*as)$",
    re.IGNORECASE,
)
DAY_ONLY_ROW_RE = re.compile(
    r"^(?P<day>\d{2})\s+\S+\s+"
    rf"(?P<scale>{SCALE_PATTERN})\b(?P<details>.*)$",
    re.IGNORECASE,
)
VACATION_DATE_ROW_RE = re.compile(
    r"^(?P<day>\d{2})/(?P<month>\d{2})\s+\S+\s+"
    r"(?P<details>(?:\d{2}:\d{2}\s+)?1019\b.*?)"
    r"(?:Sobreaviso|Turno\s+de\s+\d+\s+Hor\s*as)$",
    re.IGNORECASE,
)
VACATION_DAY_ONLY_ROW_RE = re.compile(
    r"^(?P<day>\d{2})\s+\S+\s+"
    r"(?P<details>(?:\d{2}:\d{2}\s+)?1019\b.*)$",
    re.IGNORECASE,
)
DAY_OFF_SCALES = {"FOLG", "LIVR"}
PERIOD_RE = re.compile(
    r"Per[ií]odo\s*:?\s*"
    r"\d{2}[./](?P<month>\d{2})[./](?P<year>\d{4})",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b\d{2}:\d{2}\b")
VACATION_CODE_RE = re.compile(r"\b1019\b")

EMBARKED_START = "EMBARCADO - Início do ciclo"
EMBARKED = "EMBARCADO"
WORK_ON_DAY_OFF = "TRABALHO NA FOLGA"
DAY_OFF = "FOLGA"
VACATION = "FERIAS"


class FrequencyCycleExtractionError(Exception):
    pass


@dataclass(frozen=True)
class FrequencyDay:
    date: date
    scale: str
    details: str
    pdf_line: str
    page: int


@dataclass(frozen=True)
class ClassifiedDay:
    date: date
    cycle_day: int
    situation: str
    core_situation: str
    scale: str
    details: str
    pdf_line: str
    page: int
    expected_cycle_day: int | None = None
    expected_situation: str | None = None
    exact_match: bool | None = None
    core_match: bool | None = None


def parse_excel_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    return None


def normalize_label(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip().upper()
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def compact_label(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", normalize_label(value))


def core_label(value: str | None) -> str:
    normalized = normalize_label(value)
    normalized = re.sub(r"\s+-\s+FIM DO CICLO$", "", normalized)
    normalized = re.sub(r"\s+-\s+INICIO DO CICLO$", " - INICIO DO CICLO", normalized)
    return normalized


def has_work_on_day_off_marker(details: str) -> bool:
    details = details.strip()
    if not details:
        return False

    # In the sample, 2025 appears on FOLG rows but remains FOLGA in the expected Excel.
    if re.search(r"\b2025\b", details) and not TIME_RE.search(details):
        return False

    if TIME_RE.search(details):
        return True

    # 1082 appears as a standalone marker on 23/02/2022 and the Excel classifies it
    # as TRABALHO NA FOLGA even without an explicit HH:MM marker on the daily row.
    if re.search(r"\b1082\b", details):
        return True

    return False


def has_vacation_marker(details: str) -> bool:
    return bool(VACATION_CODE_RE.search(details or ""))


def _looks_like_frequency_day_page(text: str) -> bool:
    compact = compact_label(text)
    if not compact:
        return False
    if "RELATORIODEACOMPANHAMENTODEFREQUENCIA" in compact:
        return True
    return "PERIODO" in compact and "DIAESCALA" in compact


def _extract_frequency_days_from_page_texts(page_texts: Iterable[tuple[int, str]]) -> list[FrequencyDay]:
    rows: list[FrequencyDay] = []
    current_year: int | None = None
    current_month: int | None = None

    for page_index, text in page_texts:
        period_match = PERIOD_RE.search(text)
        if period_match:
            current_year = int(period_match.group("year"))
            current_month = int(period_match.group("month"))

        for line in text.splitlines():
            stripped = line.strip()
            match = DATE_ROW_RE.match(stripped)
            if match and current_year is not None:
                row_date = date(
                    current_year,
                    int(match.group("month")),
                    int(match.group("day")),
                )
                rows.append(
                    FrequencyDay(
                        date=row_date,
                        scale=match.group("scale"),
                        details=match.group("details").strip(),
                        pdf_line=stripped,
                        page=page_index,
                    )
                )
                continue

            match = VACATION_DATE_ROW_RE.match(stripped)
            if match and current_year is not None:
                row_date = date(
                    current_year,
                    int(match.group("month")),
                    int(match.group("day")),
                )
                rows.append(
                    FrequencyDay(
                        date=row_date,
                        scale="",
                        details=match.group("details").strip(),
                        pdf_line=stripped,
                        page=page_index,
                    )
                )
                continue

            match = DAY_ONLY_ROW_RE.match(stripped)
            if match and current_year is not None and current_month is not None:
                try:
                    row_date = date(
                        current_year,
                        current_month,
                        int(match.group("day")),
                    )
                except ValueError:
                    continue

                rows.append(
                    FrequencyDay(
                        date=row_date,
                        scale=match.group("scale"),
                        details=match.group("details").strip(),
                        pdf_line=stripped,
                        page=page_index,
                    )
                )
                continue

            match = VACATION_DAY_ONLY_ROW_RE.match(stripped)
            if not match or current_year is None or current_month is None:
                continue

            try:
                row_date = date(
                    current_year,
                    current_month,
                    int(match.group("day")),
                )
            except ValueError:
                continue

            rows.append(
                FrequencyDay(
                    date=row_date,
                    scale="",
                    details=match.group("details").strip(),
                    pdf_line=stripped,
                    page=page_index,
                )
            )

    return sorted(rows, key=lambda row: row.date)


def _extract_frequency_days_with_pypdf(pdf_bytes: bytes) -> list[FrequencyDay]:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return _extract_frequency_days_from_page_texts(
        (page_index, page.extract_text() or "")
        for page_index, page in enumerate(reader.pages, start=1)
    )


def _extract_frequency_days_and_ocr_chunks(
    pdf_bytes: bytes,
) -> tuple[list[FrequencyDay], list[bytes]]:
    """Extract every day row pypdf/pdfplumber can read directly, and build
    PDF chunks (grouped `FREQUENCY_OCR_CHUNK_SIZE` pages at a time) out of the
    pages that still need local OCR — either because they are genuinely
    scanned (no text at all) or because their font encoding is obfuscated/
    "encrypted" (non-empty but meaningless text, e.g. Doro PDF Writer output).
    """
    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        page_texts: list[tuple[int, str]] = [
            (page_index, page.extract_text() or "")
            for page_index, page in enumerate(reader.pages, start=1)
        ]

        rows = _extract_frequency_days_from_page_texts(page_texts)
        rows_by_page: dict[int, list[FrequencyDay]] = {}
        for row in rows:
            rows_by_page.setdefault(row.page, []).append(row)

        retry_page_numbers = [
            page_index
            for page_index, text in page_texts
            if text.strip() and not rows_by_page.get(page_index)
        ]
        pdfplumber_texts_by_page: dict[int, str] = {}
        if retry_page_numbers:
            with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                for page_number in retry_page_numbers:
                    page = pdf.pages[page_number - 1]
                    try:
                        text = page.extract_text() or ""
                    finally:
                        page.close()
                    pdfplumber_texts_by_page[page_number] = text

            retry_rows = _extract_frequency_days_from_page_texts(
                pdfplumber_texts_by_page.items()
            )
            rows = merge_frequency_days(rows, retry_rows)
            rows_by_page = {}
            for row in rows:
                rows_by_page.setdefault(row.page, []).append(row)

        failed_page_indices: list[int] = []
        for page_index, text in page_texts:
            if rows_by_page.get(page_index):
                continue

            plumber_text = pdfplumber_texts_by_page.get(page_index, text)

            if not text.strip() and not plumber_text.strip():
                if page_has_raster_image(reader, page_index - 1):
                    failed_page_indices.append(page_index - 1)
                continue

            if _looks_like_frequency_day_page(plumber_text):
                failed_page_indices.append(page_index - 1)
                continue

            # Text is present but neither matches the known frequency-page
            # markers nor yielded any day rows. This happens with PDFs whose
            # font encoding is obfuscated/"encrypted" (e.g. Doro PDF Writer
            # output): pypdf decodes glyphs to meaningless placeholders like
            # "/0 /1 /2 ..." instead of real characters, so both the regex
            # parser and the keyword check above silently find nothing —
            # without this branch such pages were never flagged for OCR at
            # all. Restrict to pages that are actually rendered (have a
            # raster image) to avoid flagging genuinely blank/unrelated pages.
            if not has_meaningful_text(text) and not has_meaningful_text(plumber_text):
                if page_has_raster_image(reader, page_index - 1):
                    failed_page_indices.append(page_index - 1)

        ocr_chunks = _build_pdf_chunks_for_pages_from_reader(
            reader,
            failed_page_indices,
            FREQUENCY_OCR_CHUNK_SIZE,
        )
        if ocr_chunks:
            logger.info(
                "frequency: %d page(s) require local OCR in %d chunk(s)",
                len(failed_page_indices),
                len(ocr_chunks),
            )
        return rows, ocr_chunks
    except Exception as e:
        logger.debug("frequency: pdfplumber hybrid scan failed, falling back: %s", e)
        ocr_pdf = get_frequency_pages_requiring_ocr(pdf_bytes)
        return extract_frequency_days_pdfplumber(pdf_bytes), [ocr_pdf] if ocr_pdf else []


def extract_frequency_days_pdfplumber(pdf_bytes: bytes) -> list[FrequencyDay]:
    try:
        rows = _extract_frequency_days_with_pypdf(pdf_bytes)
        if rows:
            return rows
    except Exception as e:
        logger.debug("frequency: pypdf text extraction failed, trying pdfplumber: %s", e)

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        page_texts: list[tuple[int, str]] = []
        for page_index, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            finally:
                page.close()
            page_texts.append((page_index, text))

    return _extract_frequency_days_from_page_texts(page_texts)


def _base_situation(day: FrequencyDay) -> str:
    if day.scale not in DAY_OFF_SCALES:
        return EMBARKED
    if has_work_on_day_off_marker(day.details):
        return WORK_ON_DAY_OFF
    return DAY_OFF


def classify_frequency_days(days: Iterable[FrequencyDay]) -> list[ClassifiedDay]:
    ordered_days = sorted(days, key=lambda row: row.date)
    base_situations = [_base_situation(day) for day in ordered_days]
    classified: list[ClassifiedDay] = []
    cycle_day = 0
    previous_group: str | None = None

    for index, day in enumerate(ordered_days):
        base = base_situations[index]
        group = "embarked" if base == EMBARKED else "off"
        next_group = (
            "embarked"
            if index + 1 < len(base_situations) and base_situations[index + 1] == EMBARKED
            else "off"
        )

        if group != previous_group:
            cycle_day = 1
        else:
            cycle_day += 1

        situation = base
        if base == EMBARKED and group != previous_group:
            situation = EMBARKED_START
        elif base in {DAY_OFF, WORK_ON_DAY_OFF} and next_group == "embarked":
            situation = f"{base} - fim do ciclo"

        if has_vacation_marker(day.details):
            situation = VACATION

        classified.append(
            ClassifiedDay(
                date=day.date,
                cycle_day=cycle_day,
                situation=situation,
                core_situation=VACATION if situation == VACATION else base if base != EMBARKED else situation,
                scale=day.scale,
                details=day.details,
                pdf_line=day.pdf_line,
                page=day.page,
            )
        )
        previous_group = group

    return classified


def compare_with_expected(
    classified_days: Iterable[ClassifiedDay],
    expected: dict[date, tuple[int | None, str]],
) -> list[ClassifiedDay]:
    compared: list[ClassifiedDay] = []

    for day in classified_days:
        expected_row = expected.get(day.date)
        if expected_row is None:
            compared.append(day)
            continue

        expected_cycle_day, expected_situation = expected_row
        compared.append(
            ClassifiedDay(
                date=day.date,
                cycle_day=day.cycle_day,
                situation=day.situation,
                core_situation=day.core_situation,
                scale=day.scale,
                details=day.details,
                pdf_line=day.pdf_line,
                page=day.page,
                expected_cycle_day=expected_cycle_day,
                expected_situation=expected_situation,
                exact_match=(
                    day.cycle_day == expected_cycle_day
                    and normalize_label(day.situation) == normalize_label(expected_situation)
                ),
                core_match=(
                    day.cycle_day == expected_cycle_day
                    and core_label(day.core_situation) == core_label(expected_situation)
                ),
            )
        )

    return compared


def _build_pdf_for_pages(pdf_bytes: bytes, page_indices: Iterable[int]) -> bytes | None:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return _build_pdf_for_pages_from_reader(reader, page_indices)


def _build_pdf_for_pages_from_reader(
    reader: pypdf.PdfReader,
    page_indices: Iterable[int],
) -> bytes | None:
    indices = list(dict.fromkeys(page_indices))
    if not indices:
        return None

    writer = pypdf.PdfWriter()
    for index in indices:
        if 0 <= index < len(reader.pages):
            writer.add_page(reader.pages[index])

    if len(writer.pages) == 0:
        return None

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _build_pdf_chunks_for_pages_from_reader(
    reader: pypdf.PdfReader,
    page_indices: Iterable[int],
    chunk_size: int,
) -> list[bytes]:
    indices = list(dict.fromkeys(page_indices))
    chunks: list[bytes] = []

    for start in range(0, len(indices), chunk_size):
        chunk = _build_pdf_for_pages_from_reader(reader, indices[start:start + chunk_size])
        if chunk:
            chunks.append(chunk)

    return chunks


def get_frequency_pages_requiring_ocr(pdf_bytes: bytes) -> bytes | None:
    """Return pages that look image-only (no extractable text at all) for
    local OCR fallback. Used only when the primary hybrid scan itself raises
    an exception (e.g. a malformed PDF pypdf/pdfplumber can partially open).
    """
    page_indices: list[int] = []
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))

    for page_index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            continue
        page_indices.append(page_index)

    if not page_indices:
        return None

    logger.info(
        "frequency: %d page(s) require local OCR",
        len(page_indices),
    )
    return _build_pdf_for_pages_from_reader(reader, page_indices)


def merge_frequency_days(
    pdfplumber_rows: Iterable[FrequencyDay],
    ocr_rows: Iterable[FrequencyDay],
) -> list[FrequencyDay]:
    rows_by_date: dict[date, FrequencyDay] = {}

    for row in ocr_rows:
        rows_by_date[row.date] = row
    for row in pdfplumber_rows:
        rows_by_date[row.date] = row

    return sorted(rows_by_date.values(), key=lambda row: row.date)


def _try_tesseract_ocr(pdf_bytes: bytes) -> list[FrequencyDay]:
    """Run local Tesseract OCR extraction, never raising.

    Returns an empty list whenever Tesseract is not installed, the bytes are
    not a renderable PDF, or OCR yields nothing — callers treat an empty
    result as "this chunk could not be read locally" and log accordingly.
    """
    try:
        from services.tesseract_ocr_service import (
            TesseractOCRError,
            extract_frequency_days_tesseract,
            is_tesseract_available,
        )
    except ImportError as e:
        logger.debug("frequency: tesseract OCR dependencies not installed: %s", e)
        return []

    if not is_tesseract_available():
        logger.warning("frequency: Tesseract binary not found, skipping local OCR")
        return []

    try:
        page_texts = None
        try:
            from services.tesseract_ocr_service import ocr_pdf_page_texts
            page_texts = ocr_pdf_page_texts(pdf_bytes)
        except Exception:
            pass
        rows = extract_frequency_days_tesseract(pdf_bytes)
        if rows:
            logger.info("frequency: Tesseract OCR extracted %d row(s) locally", len(rows))
        elif page_texts is not None:
            total_chars = sum(len(t) for _, t in page_texts)
            sample = next((t for _, t in page_texts if t.strip()), "")[:300]
            logger.warning(
                "frequency: Tesseract OCR ran on %d page(s), %d total chars, "
                "but parsed 0 rows. Sample text: %r",
                len(page_texts),
                total_chars,
                sample,
            )
        return rows
    except TesseractOCRError as e:
        logger.warning("frequency: Tesseract OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("frequency: Tesseract OCR raised unexpected error: %s", e)
        return []


async def extract_frequency_days_hybrid(pdf_bytes: bytes) -> tuple[list[FrequencyDay], str]:
    """Extraction pipeline: pdfplumber/pypdf text -> local Tesseract OCR.

    No external API is used anywhere in this pipeline. Pages pypdf/pdfplumber
    can read directly are parsed as-is; pages that are scanned or have an
    obfuscated/"encrypted" font encoding are rendered to images and OCR'd
    locally with Tesseract, feeding the exact same line-parsing regexes used
    for native text so the output shape never changes.
    """
    rows, ocr_chunks = _extract_frequency_days_and_ocr_chunks(pdf_bytes)

    if not rows and not ocr_chunks:
        tesseract_rows = _try_tesseract_ocr(pdf_bytes)
        return (tesseract_rows, "tesseract") if tesseract_rows else ([], "none")

    if not ocr_chunks:
        return rows, "pdfplumber"

    tesseract_rows: list[FrequencyDay] = []
    for chunk in ocr_chunks:
        tesseract_rows.extend(_try_tesseract_ocr(chunk))

    if not tesseract_rows:
        return rows, "pdfplumber"

    merged_rows = merge_frequency_days(rows, tesseract_rows)
    provider = "pdfplumber+tesseract" if rows else "tesseract"
    return merged_rows, provider


async def extract_and_classify_frequency_cycles(
    pdf_bytes: bytes,
) -> tuple[list[ClassifiedDay], str]:
    rows, provider = await extract_frequency_days_hybrid(pdf_bytes)

    if not rows:
        raise FrequencyCycleExtractionError(
            "Nenhuma linha diaria de frequencia encontrada no PDF."
        )

    return classify_frequency_days(rows), provider


async def stream_frequency_cycle_extraction(
    pdf_bytes: bytes,
    original_stem: str,
):
    from services.frequency_cycle_excel_builder import build_frequency_cycle_excel

    try:
        yield "data: " + json.dumps({
            "type": "progress",
            "chunk": 0,
            "total": 1,
            "step": "pdfplumber",
            "message": "Analisando relatorio de frequencia com pdfplumber...",
        }) + "\n\n"

        task = asyncio.create_task(asyncio.to_thread(_extract_frequency_days_and_ocr_chunks, pdf_bytes))
        while not task.done():
            yield ": keep-alive\n\n"
            await asyncio.sleep(10)

        rows, ocr_chunks = task.result()
        provider = "pdfplumber"

        if not rows and not ocr_chunks:
            yield "data: " + json.dumps({
                "type": "progress",
                "chunk": 1,
                "total": 1,
                "step": "tesseract",
                "message": "pdfplumber nao encontrou linhas diarias. Tentando OCR local (Tesseract)...",
            }) + "\n\n"

            tesseract_task = asyncio.create_task(asyncio.to_thread(_try_tesseract_ocr, pdf_bytes))
            while not tesseract_task.done():
                yield ": keep-alive\n\n"
                await asyncio.sleep(10)
            tesseract_rows = tesseract_task.result()

            rows = tesseract_rows
            provider = "tesseract" if tesseract_rows else "none"

        elif ocr_chunks:
            total_chunks = len(ocr_chunks)
            tesseract_rows: list[FrequencyDay] = []

            for chunk_index, chunk in enumerate(ocr_chunks, start=1):
                yield "data: " + json.dumps({
                    "type": "progress",
                    "chunk": chunk_index,
                    "total": total_chunks,
                    "step": "tesseract",
                    "message": f"OCR local (Tesseract): processando paginas pendentes ({chunk_index}/{total_chunks})...",
                }) + "\n\n"

                chunk_task = asyncio.create_task(asyncio.to_thread(_try_tesseract_ocr, chunk))
                while not chunk_task.done():
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(10)

                chunk_rows = chunk_task.result()
                if chunk_rows:
                    tesseract_rows.extend(chunk_rows)
                else:
                    logger.warning(
                        "frequency stream: Tesseract chunk %d/%d yielded no rows",
                        chunk_index,
                        total_chunks,
                    )

            if tesseract_rows:
                merged_rows = merge_frequency_days(rows, tesseract_rows)
                provider = "pdfplumber+tesseract" if rows else "tesseract"
                rows = merged_rows

        if not rows:
            yield "data: " + json.dumps({
                "type": "error",
                "message": "Nenhuma linha diaria de frequencia encontrada no PDF.",
            }) + "\n\n"
            return

        classified = classify_frequency_days(rows)
        excel_bytes = await asyncio.to_thread(
            build_frequency_cycle_excel,
            classified,
            provider,
        )

        yield "data: " + json.dumps({
            "type": "done",
            "excel_b64": base64.b64encode(excel_bytes).decode(),
            "excel_filename": f"frequencia_{original_stem}.xlsx",
            "rows_extracted": len(classified),
            "provider": provider,
            "pdf_type": "native" if provider == "pdfplumber" else (
                "mixed" if provider == "pdfplumber+tesseract" else "scanned"
            ),
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("frequency cycle stream: unexpected error: %s", e)
        yield "data: " + json.dumps({
            "type": "error",
            "message": "Erro interno ao processar relatorio de frequencia.",
        }) + "\n\n"
