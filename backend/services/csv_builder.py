from __future__ import annotations
import logging
from datetime import date, timedelta
from models.timesheet import ExtractionResult, TimesheetRow

logger = logging.getLogger(__name__)

# PJeCalc's import format is a fixed external contract: exactly 6 entrada/
# saída pairs (12 time columns) after the date. Unlike the Excel sheet, this
# cannot grow — a day with more than 6 pairs gets truncated (see build_csv).
_HEADER = "Data;Entrada1;Saída1;Entrada2;Saída2;Entrada3;Saída3;Entrada4;Saída4;Entrada5;Saída5;Entrada6;Saída6"
_EMPTY_TIMES = ";" * 12  # 12 empty time fields after the date
_CSV_MARK_SLOTS = 12


def _merge_contiguous_marks(marks: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0

    while i < len(marks):
        entrada = marks[i]
        if i + 1 >= len(marks):
            merged.append(entrada)
            break

        saida = marks[i + 1]
        next_pair = i + 2
        while next_pair + 1 < len(marks) and saida == marks[next_pair]:
            saida = marks[next_pair + 1]
            next_pair += 2

        merged.extend([entrada, saida])
        i = next_pair

    return merged


def _parse_date(date_str: str) -> date | None:
    try:
        d, m, y = date_str.split("/")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def build_csv(result: ExtractionResult) -> str:
    row_map: dict[str, object] = {}
    for row in result.rows:
        if row.data:
            row_map[row.data] = row

    parsed_dates = [d for ds in row_map if (d := _parse_date(ds))]

    if not parsed_dates:
        return _HEADER + "\n"

    min_date = min(parsed_dates)
    max_date = max(parsed_dates)

    lines = [_HEADER]
    current = min_date
    while current <= max_date:
        date_str = current.strftime("%d/%m/%Y")
        row = row_map.get(date_str)

        if row is not None:
            marks = _merge_contiguous_marks(row.marcacoes)
            if len(marks) > _CSV_MARK_SLOTS:
                logger.warning(
                    "csv_builder: %s has %d entrada/saida marks, PJeCalc format "
                    "only supports %d - truncating extras: %s",
                    date_str, len(marks), _CSV_MARK_SLOTS, marks[_CSV_MARK_SLOTS:],
                )
            times = marks[:_CSV_MARK_SLOTS] + [""] * max(0, _CSV_MARK_SLOTS - len(marks))
            lines.append(date_str + ";" + ";".join(times))
        else:
            lines.append(date_str + _EMPTY_TIMES)

        current += timedelta(days=1)

    return "\n".join(lines)


def _build_csv_for_rows(rows: list[TimesheetRow]) -> str:
    """Build a PJeCalc CSV string for a single worker's rows."""
    from models.timesheet import ExtractionResult
    result = ExtractionResult(rows=rows, provider="gemini-guia", pdf_type="scanned")
    return build_csv(result)


def build_guia_csv(rows: list[TimesheetRow]) -> tuple[bytes, str]:
    """Build a PJeCalc CSV for Guia Ministerial rows (single worker assumed)."""
    csv_text = _build_csv_for_rows(rows)
    return csv_text.encode("utf-8-sig"), "text/csv"
