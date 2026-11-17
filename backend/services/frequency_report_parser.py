from __future__ import annotations

import logging
import re
from datetime import date

import pdfplumber

from models.timesheet import TimesheetRow

logger = logging.getLogger(__name__)

# The accented letters of this title come out as U+FFFD in these PDFs, so the
# match stops before them.
_TITLE_RE = re.compile(r"ACOMPANHAMENTO\s+DE\s+FREQU", re.IGNORECASE)
_PERIOD_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*a\s*(\d{2})/(\d{2})/(\d{4})")
_DAY_RE = re.compile(r"^(\d{2})/(\d{2})$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")

_DAY_COLUMN_MAX_X = 40.0
_PUNCH_COLUMNS_START_X = 90.0
_PUNCH_COLUMNS_END_FALLBACK_X = 216.0
_SAME_LINE_TOLERANCE = 3.0
_MAX_PUNCHES = 4
_TITLE_SCAN_PAGES = 2


def is_frequency_report(pdf: pdfplumber.PDF) -> bool:
    return any(
        _TITLE_RE.search(page.extract_text() or "")
        for page in pdf.pages[:_TITLE_SCAN_PAGES]
    )


def parse_frequency_report(pdf: pdfplumber.PDF) -> list[TimesheetRow]:
    """Entrada/Saída of each worked day from "Relatório de Acompanhamento de Frequência".

    Each month is one table whose rows are "DD/MM weekday scale in out [in out]
    ..." with no year; the year comes from the "Período" on the month's first
    page. A day with more than two pairs repeats its date on a second line, so
    lines of the same date are joined in reading order. Only the Marcações columns are read - later columns repeat times that
    are differences, not punches - and the "Rubricas salariais" pages are not
    day tables, so they contribute nothing.
    """
    punches_by_date: dict[str, list[str]] = {}
    period: tuple[date, date] | None = None
    for page in pdf.pages:
        words = page.extract_words()
        period = _period_in(words) or period
        for row in _rows_from_words(words, period):
            punches_by_date.setdefault(row.data, []).extend(row.marcacoes)
    logger.info("frequency report: %d day(s) with punches", len(punches_by_date))
    return [TimesheetRow(data=day, marcacoes=punches) for day, punches in punches_by_date.items()]


def _period_in(words: list[dict]) -> tuple[date, date] | None:
    match = _PERIOD_RE.search(" ".join(word["text"] for word in words))
    if not match:
        return None
    d1, m1, y1, d2, m2, y2 = (int(group) for group in match.groups())
    try:
        return date(y1, m1, d1), date(y2, m2, d2)
    except ValueError:
        return None


def _punch_columns_end_x(words: list[dict]) -> float:
    pht_columns = [word["x0"] for word in words if word["text"] == "PHT"]
    return min(pht_columns) - 5 if pht_columns else _PUNCH_COLUMNS_END_FALLBACK_X


def _rows_from_words(words: list[dict], period: tuple[date, date] | None) -> list[TimesheetRow]:
    if period is None:
        return []
    punch_end_x = _punch_columns_end_x(words)
    anchors = [
        word for word in words
        if word["x0"] < _DAY_COLUMN_MAX_X and _DAY_RE.match(word["text"])
    ]
    rows: list[TimesheetRow] = []
    for anchor in anchors:
        punches = sorted(
            (
                word for word in words
                if _TIME_RE.match(word["text"])
                and _PUNCH_COLUMNS_START_X < word["x0"] < punch_end_x
                and abs(word["top"] - anchor["top"]) < _SAME_LINE_TOLERANCE
            ),
            key=lambda word: word["x0"],
        )
        resolved = _resolve_date(anchor["text"], period)
        if resolved is None or not punches:
            continue
        rows.append(TimesheetRow(
            data=resolved.strftime("%d/%m/%Y"),
            marcacoes=[word["text"] for word in punches[:_MAX_PUNCHES]],
        ))
    return rows


def _resolve_date(day_month: str, period: tuple[date, date]) -> date | None:
    day, month = (int(part) for part in day_month.split("/"))
    start, end = period
    for year in sorted({start.year, end.year}):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if start <= candidate <= end:
            return candidate
    return None
