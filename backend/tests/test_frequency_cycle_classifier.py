from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from services.frequency_cycle_service import (
    DAY_OFF,
    EMBARKED,
    EMBARKED_START,
    WORK_ON_DAY_OFF,
    FrequencyDay,
    classify_frequency_days,
)


def _day(day: int, scale: str, details: str = "") -> FrequencyDay:
    return FrequencyDay(
        date=date(2022, 1, day),
        scale=scale,
        details=details,
        pdf_line=f"{day:02d}/01 seg {scale} {details} Sobreaviso",
        page=1,
    )


def test_classifies_embarked_cycle_start_and_following_days():
    result = classify_frequency_days([
        _day(1, "FOLG"),
        _day(2, "HS02"),
        _day(3, "HS02"),
    ])

    assert result[0].cycle_day == 1
    assert result[0].situation == f"{DAY_OFF} - fim do ciclo"
    assert result[1].cycle_day == 1
    assert result[1].situation == EMBARKED_START
    assert result[2].cycle_day == 2
    assert result[2].situation == EMBARKED


def test_classifies_folg_with_work_markers_as_work_on_day_off():
    result = classify_frequency_days([
        _day(1, "HS02"),
        _day(2, "FOLG", "08:00 **** -1,00 -36,60"),
        _day(3, "FOLG", "1082 -1,00 -17,60"),
        _day(4, "FOLG", "2025 -0,50 -19,60"),
    ])

    assert result[1].situation == WORK_ON_DAY_OFF
    assert result[2].situation == WORK_ON_DAY_OFF
    assert result[3].situation == DAY_OFF


def test_classifies_sample_pdf_against_expected_excel():
    import openpyxl

    from services.frequency_cycle_service import (
        compare_with_expected,
        extract_frequency_days_pdfplumber,
        parse_excel_date,
    )

    if not Path("frequencia_exemplo.pdf").exists() or not Path("excel_exemplo.xlsx").exists():
        pytest.skip("sample PDF/Excel files are not present")

    pdf_bytes = open("frequencia_exemplo.pdf", "rb").read()
    rows = classify_frequency_days(extract_frequency_days_pdfplumber(pdf_bytes))

    wb = openpyxl.load_workbook("excel_exemplo.xlsx", data_only=True)
    ws = wb["Ciclos"]
    expected = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_date = parse_excel_date(row[0])
        if row_date and row[2]:
            expected[row_date] = (int(row[1]), str(row[2]).strip())

    compared = [
        row
        for row in compare_with_expected(rows, expected)
        if row.exact_match is not None
    ]

    assert len(compared) == 195
    assert all(row.exact_match for row in compared)
