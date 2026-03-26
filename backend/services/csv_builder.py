from __future__ import annotations
from datetime import date, timedelta
from models.timesheet import ExtractionResult

_HEADER = "Data;Entrada1;Saída1;Entrada2;Saída2;Entrada3;Saída3;Entrada4;Saída4;Entrada5;Saída5;Entrada6;Saída6"
_EMPTY_TIMES = ";" * 12  # 12 empty time fields after the date


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
            times = [
                row.entrada_1 or "",
                row.saida_1 or "",
                row.entrada_2 or "",
                row.saida_2 or "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
            lines.append(date_str + ";" + ";".join(times))
        else:
            lines.append(date_str + _EMPTY_TIMES)

        current += timedelta(days=1)

    return "\n".join(lines)
