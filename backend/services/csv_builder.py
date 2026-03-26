from __future__ import annotations
from datetime import date, timedelta
from models.timesheet import ExtractionResult


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

    parsed_dates: list[date] = []
    for date_str in row_map:
        d = _parse_date(date_str)
        if d:
            parsed_dates.append(d)

    if not parsed_dates:
        return "Data\n"

    min_date = min(parsed_dates)
    max_date = max(parsed_dates)

    max_pairs = 1
    for row in result.rows:
        pairs = 0
        if row.entrada_1 or row.saida_1:
            pairs = max(pairs, 1)
        if row.entrada_2 or row.saida_2:
            pairs = max(pairs, 2)
        if pairs > max_pairs:
            max_pairs = pairs
    max_pairs = max(1, min(max_pairs, 6))

    header_parts = ["Data"]
    for i in range(1, max_pairs + 1):
        header_parts.append(f"Entrada {i}")
        header_parts.append(f"Saida {i}")
    lines = [";".join(header_parts)]

    current = min_date
    while current <= max_date:
        date_str = current.strftime("%d/%m/%Y")
        row = row_map.get(date_str)

        cols = [date_str]
        if row is not None:
            times = [
                row.entrada_1 or "",
                row.saida_1 or "",
                row.entrada_2 or "",
                row.saida_2 or "",
            ]
            for i in range(max_pairs * 2):
                cols.append(times[i] if i < len(times) else "")
        else:
            cols.extend([""] * (max_pairs * 2))

        lines.append(";".join(cols))
        current += timedelta(days=1)

    return "\n".join(lines)
