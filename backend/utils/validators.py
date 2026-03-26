from __future__ import annotations
import re
from datetime import datetime

from models.timesheet import TimesheetRow


def _parse_date(data: str) -> datetime | None:
    try:
        return datetime.strptime(data, "%d/%m/%Y")
    except (ValueError, TypeError):
        return None


def _parse_time(t: str) -> tuple[int, int] | None:
    if not t:
        return None
    m = re.match(r"^(\d{2}):(\d{2})$", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _time_minutes(t: str) -> int | None:
    parsed = _parse_time(t)
    if parsed is None:
        return None
    return parsed[0] * 60 + parsed[1]


def validate_row(row: TimesheetRow) -> list[str]:
    warnings: list[str] = []
    label = row.data or "data desconhecida"

    # Validate date format
    if row.data:
        parsed = _parse_date(row.data)
        if parsed is None:
            warnings.append(f"Data inválida: '{row.data}'")
        elif parsed.year < 2000:
            warnings.append(f"Ano implausível em {row.data}")

    # Validate time consistency: saida > entrada
    pairs = [
        ("entrada_1", "saida_1"),
        ("entrada_2", "saida_2"),
    ]
    for entrada_field, saida_field in pairs:
        entrada = getattr(row, entrada_field)
        saida = getattr(row, saida_field)
        if entrada and saida:
            e_min = _time_minutes(entrada)
            s_min = _time_minutes(saida)
            if e_min is not None and s_min is not None and s_min <= e_min:
                warnings.append(
                    f"{saida_field} ({saida}) antes ou igual a {entrada_field} ({entrada}) em {label}"
                )

    return warnings


def validate_result(rows: list[TimesheetRow]) -> list[str]:
    warnings: list[str] = []

    # Collect valid dates
    dated_rows: list[tuple[datetime, TimesheetRow]] = []
    for row in rows:
        if row.data:
            parsed = _parse_date(row.data)
            if parsed:
                dated_rows.append((parsed, row))

    if not dated_rows:
        return warnings

    dated_rows.sort(key=lambda x: x[0])

    # Detect duplicates
    seen_dates: dict[str, int] = {}
    for dt, _ in dated_rows:
        key = dt.strftime("%d/%m/%Y")
        seen_dates[key] = seen_dates.get(key, 0) + 1
    for date_str, count in seen_dates.items():
        if count > 1:
            warnings.append(f"Data duplicada: {date_str} aparece {count} vezes")

    # Detect gaps > 7 days (excluding weekends heuristic: just check calendar days)
    for i in range(1, len(dated_rows)):
        prev_dt = dated_rows[i - 1][0]
        curr_dt = dated_rows[i][0]
        delta = (curr_dt - prev_dt).days
        if delta > 7:
            warnings.append(
                f"Gap de {delta} dias sem registro entre {prev_dt.strftime('%d/%m/%Y')} e {curr_dt.strftime('%d/%m/%Y')}"
            )

    return warnings
