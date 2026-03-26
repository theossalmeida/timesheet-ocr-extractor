from __future__ import annotations
import io
import re

import pdfplumber

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


def _detect_columns(header_rows: list[list[str | None]]) -> dict[str, int | None]:
    """Scan up to 3 rows to detect column roles by content patterns."""
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
            elif _TIME_RE.search(cell) and not _DATE_RE.search(cell):
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
        if multirow_rows:
            return multirow_rows

        # Fallback: plain text extraction (FOLHA DE PONTO fixed-width format)
        pdf.close()
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        full_text = "\n".join(
            (page.extract_text() or "") for page in pdf.pages
        )
        text_rows = _parse_text_rows(full_text)
        return text_rows if text_rows else None
    finally:
        if pdf is not None:
            pdf.close()
