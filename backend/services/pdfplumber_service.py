from __future__ import annotations
import io
import logging
import re

import pdfplumber
import pypdf

logger = logging.getLogger(__name__)

from models.timesheet import TimesheetRow
from utils.normalizers import normalize_date, normalize_time, normalize_ocorrencia, _PT_MONTHS

_DATE_RE = re.compile(r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}")
_TIME_RE = re.compile(r"\d{1,2}[:.]\d{2}")
# Matches labor-court multi-row cell lines: "23/jun/15 weekday ..."
_MULTIROW_DATE_RE = re.compile(
    r"^(\d{1,2})[/\-\.]([a-záéíóú]{3})[/\-\.](\d{2,4})\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"\b(segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo)"
    r"(\s*-\s*feira)?",
    re.IGNORECASE,
)
# Matches text-based FOLHA DE PONTO rows: "DD/MM/YYYY WEEKDAY ..."
_TEXT_ROW_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s+\w{3,}\s+(.*)",
    re.MULTILINE,
)
# Tokens to strip from occurrence text in text-based format
_NOISE_RE = re.compile(
    r"\b(\d{2}:\d{2}|\d{3}|PRESE|PRESENTE)\b",
    re.IGNORECASE,
)


def _parse_text_rows(full_text: str) -> list[TimesheetRow]:
    """Parse fixed-width text timesheets (FOLHA DE PONTO format).

    Row format: DD/MM/YYYY WEEKDAY [HH:MM HH:MM ...] [OCCURRENCE]
    """
    rows: list[TimesheetRow] = []
    for m in _TEXT_ROW_RE.finditer(full_text):
        date_str, rest = m.group(1), m.group(2).strip()
        normalized_date = normalize_date(date_str)
        if not normalized_date:
            continue
        times = re.findall(r"\b(\d{2}:\d{2})\b", rest)
        occ_text = _NOISE_RE.sub("", rest).strip()
        occ_raw, occ_tipo = normalize_ocorrencia(occ_text) if occ_text else (None, None)
        # Skip "trabalho_normal" occurrence — it's just a presence marker, not an absence
        if occ_tipo == "trabalho_normal":
            occ_raw, occ_tipo = None, None
        rows.append(TimesheetRow(
            data=normalized_date,
            entrada_1=normalize_time(times[0]) if len(times) > 0 else None,
            saida_1=normalize_time(times[1]) if len(times) > 1 else None,
            entrada_2=None,
            saida_2=None,
            ocorrencia_raw=occ_raw,
            ocorrencia_tipo=occ_tipo,
        ))
    return rows


def _parse_multirow_cell(cell_text: str) -> list[TimesheetRow]:
    """Parse cells where multiple daily records are concatenated with newlines.

    Format per line: DD/mmm/YY weekday HH:MM HH:MM [occurrence]
    Common in Brazilian labor-court PDFs.
    """
    rows: list[TimesheetRow] = []
    for line in cell_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _MULTIROW_DATE_RE.match(line)
        if not m:
            continue
        day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
        month_num = _PT_MONTHS.get(month_str)
        if month_num is None:
            continue
        year_int = (2000 + int(year)) if len(year) == 2 else int(year)
        if year_int <= 2000:
            continue
        normalized_date = f"{int(day):02d}/{month_num:02d}/{year_int}"

        rest = line[m.end():].strip()
        rest = _WEEKDAY_RE.sub("", rest).strip()

        times = re.findall(r"\d{1,2}:\d{2}", rest)
        # Remove times to get possible occurrence text
        occ_text = re.sub(r"\d{1,2}:\d{2}", "", rest).strip(" -,.")

        occ_raw, occ_tipo = normalize_ocorrencia(occ_text) if occ_text else (None, None)
        rows.append(TimesheetRow(
            data=normalized_date,
            entrada_1=normalize_time(times[0]) if len(times) > 0 else None,
            saida_1=normalize_time(times[1]) if len(times) > 1 else None,
            entrada_2=normalize_time(times[2]) if len(times) > 2 else None,
            saida_2=normalize_time(times[3]) if len(times) > 3 else None,
            ocorrencia_raw=occ_raw,
            ocorrencia_tipo=occ_tipo,
        ))
    return rows


# Column header patterns — used to identify roles by name before falling back to position
_HDR_DATE_RE = re.compile(r"\b(data|date|dia)\b", re.IGNORECASE)
_HDR_ENTRY_RE = re.compile(r"\bentrada\b|\bentrada\s*\d|\bin\b|\bchegada\b", re.IGNORECASE)
_HDR_EXIT_RE = re.compile(r"\bsa[íi]da\b|\bsa[íi]da\s*\d|\bout\b|\bpartida\b", re.IGNORECASE)
_HDR_OCC_RE = re.compile(r"\bocorr[êe]ncia|\bfalta|\bobs|\bsit|\bjustif|\bcodigo\b|\bcód", re.IGNORECASE)
# Time columns that are NOT entrada/saída (should be excluded from entry/exit detection)
_HDR_SKIP_TIME_RE = re.compile(
    r"\bacr[eé]scimo|\bextra|\badicional|\bintervalo|\balmo[cç]o|\bdescanso|\bhoras?\b",
    re.IGNORECASE,
)


def _detect_columns_by_header(table: list[list[str | None]]) -> dict[str, int | None] | None:
    """Try to map column roles from header row text.

    Returns a mapping if a header row with recognisable labels is found,
    otherwise returns None so the caller can fall back to pattern detection.
    """
    for row in table[:3]:
        if not row:
            continue
        cells = [str(c).strip() if c else "" for c in row]
        # A header row has mostly text cells (not dates or pure time values)
        text_cells = sum(
            1 for c in cells if c and not _DATE_RE.search(c) and not _TIME_RE.search(c)
        )
        if text_cells < 2:
            continue

        date_col: int | None = None
        entry_cols: list[int] = []
        exit_cols: list[int] = []
        occ_col: int | None = None

        for i, cell in enumerate(cells):
            if not cell:
                continue
            if _HDR_DATE_RE.search(cell):
                date_col = i
            elif _HDR_ENTRY_RE.search(cell):
                entry_cols.append(i)
            elif _HDR_EXIT_RE.search(cell):
                exit_cols.append(i)
            elif _HDR_OCC_RE.search(cell) and occ_col is None:
                occ_col = i
            # Columns matching _HDR_SKIP_TIME_RE are intentionally ignored

        if date_col is not None and (entry_cols or exit_cols):
            return {
                "date": date_col,
                "entry1": entry_cols[0] if len(entry_cols) > 0 else None,
                "exit1": exit_cols[0] if len(exit_cols) > 0 else None,
                "entry2": entry_cols[1] if len(entry_cols) > 1 else None,
                "exit2": exit_cols[1] if len(exit_cols) > 1 else None,
                "occ": occ_col,
            }
    return None


def _detect_columns(header_rows: list[list[str | None]]) -> dict[str, int | None]:
    """Scan up to 3 rows to detect column roles.

    Tries header-name matching first; falls back to content-pattern detection
    so that non-entrada/saída time columns (e.g. acréscimos) are not mistakenly
    mapped to entry/exit slots.
    """
    by_header = _detect_columns_by_header(header_rows)
    if by_header is not None:
        return by_header

    # Fallback: positional detection from data patterns.
    # Build a set of columns explicitly labelled as non-entry/exit time columns
    # to exclude them even in the fallback path.
    skip_cols: set[int] = set()
    for row in header_rows[:3]:
        if not row:
            continue
        cells = [str(c).strip() if c else "" for c in row]
        text_cells = sum(
            1 for c in cells if c and not _DATE_RE.search(c) and not _TIME_RE.search(c)
        )
        if text_cells >= 2:
            for i, cell in enumerate(cells):
                if cell and _HDR_SKIP_TIME_RE.search(cell):
                    skip_cols.add(i)

    date_col: int | None = None
    time_cols: list[int] = []
    occ_col: int | None = None

    for row in header_rows[:3]:
        for i, cell in enumerate(row):
            if not cell:
                continue
            cell = str(cell).strip()
            if date_col is None and _DATE_RE.search(cell):
                date_col = i
            elif _TIME_RE.search(cell) and not _DATE_RE.search(cell) and i not in skip_cols:
                if i not in time_cols:
                    time_cols.append(i)
            elif cell and not _DATE_RE.search(cell) and not _TIME_RE.search(cell):
                if re.search(r"[a-zA-ZÀ-ú]{2,}", cell) and occ_col is None:
                    occ_col = i
        if date_col is not None:
            break

    time_cols.sort()
    return {
        "date": date_col,
        "entry1": time_cols[0] if len(time_cols) > 0 else None,
        "exit1": time_cols[1] if len(time_cols) > 1 else None,
        "entry2": time_cols[2] if len(time_cols) > 2 else None,
        "exit2": time_cols[3] if len(time_cols) > 3 else None,
        "occ": occ_col,
    }


def extract_with_pdfplumber(pdf_bytes: bytes) -> list[TimesheetRow] | None:
    pdf = None
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        logger.info("pdfplumber: opened PDF — pages=%d", len(pdf.pages))
        all_rows: list[TimesheetRow] = []

        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                cols = _detect_columns(table)
                if cols["date"] is None:
                    continue
                for row in table:
                    if not row:
                        continue
                    date_cell = str(row[cols["date"]] or "").strip()
                    if not _DATE_RE.search(date_cell):
                        continue
                    normalized_date = normalize_date(date_cell)
                    if not normalized_date:
                        continue

                    def get(idx: int | None) -> str | None:
                        if idx is None or idx >= len(row):
                            return None
                        return str(row[idx] or "").strip() or None

                    occ_raw, occ_tipo = normalize_ocorrencia(get(cols["occ"]) or "")
                    all_rows.append(TimesheetRow(
                        data=normalized_date,
                        entrada_1=normalize_time(get(cols["entry1"]) or ""),
                        saida_1=normalize_time(get(cols["exit1"]) or ""),
                        entrada_2=normalize_time(get(cols["entry2"]) or ""),
                        saida_2=normalize_time(get(cols["exit2"]) or ""),
                        ocorrencia_raw=occ_raw,
                        ocorrencia_tipo=occ_tipo,
                    ))

        logger.info("pdfplumber: structured table — rows=%d", len(all_rows))
        if all_rows:
            return all_rows

        # Fallback: scan for multi-row merged cells (labor-court format)
        pdf.close()
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        multirow_rows: list[TimesheetRow] = []
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in (table or []):
                    for cell in (row or []):
                        if not cell:
                            continue
                        cell_str = str(cell)
                        if "\n" in cell_str and _MULTIROW_DATE_RE.search(cell_str):
                            multirow_rows.extend(_parse_multirow_cell(cell_str))
        logger.info("pdfplumber: multirow cell — rows=%d", len(multirow_rows))
        if multirow_rows:
            return multirow_rows

        # Fallback: plain text extraction (FOLHA DE PONTO fixed-width format)
        pdf.close()
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        full_text = "\n".join(
            (page.extract_text() or "") for page in pdf.pages
        )
        text_rows = _parse_text_rows(full_text)
        logger.info("pdfplumber: text fallback — rows=%d", len(text_rows))
        return text_rows if text_rows else None
    finally:
        if pdf is not None:
            pdf.close()


def get_scanned_page_bytes(pdf_bytes: bytes) -> bytes | None:
    """Return a sub-PDF containing pages whose timesheet content is in images.

    A page is considered "scanned" when it has at least one embedded image
    AND none of the three pdfplumber extraction strategies find any timesheet
    rows in its text/tables.  Returns None when no such pages are found.
    """
    scanned_indices: list[int] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            if not page.images:
                continue  # no images → cannot be a scanned page

            text = page.extract_text() or ""

            # Strategy 1: fixed-width text rows
            if _parse_text_rows(text):
                continue

            # Strategy 2: multirow merged cells
            has_multirow = any(
                _MULTIROW_DATE_RE.search(str(cell or ""))
                for table in (page.extract_tables() or [])
                for row in (table or [])
                for cell in (row or [])
                if cell and "\n" in str(cell)
            )
            if has_multirow:
                continue

            # Strategy 3: structured table with a date column
            has_date_table = any(
                _detect_columns(table)["date"] is not None
                for table in (page.extract_tables() or [])
                if table
            )
            if has_date_table:
                continue

            scanned_indices.append(i)

    if not scanned_indices:
        return None

    logger.info(
        "get_scanned_page_bytes: found %d scanned page(s) — indices %s",
        len(scanned_indices),
        scanned_indices,
    )
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    writer = pypdf.PdfWriter()
    for idx in scanned_indices:
        writer.add_page(reader.pages[idx])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
