from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.frequency_cycle_service import (
    DAY_OFF,
    EMBARKED,
    EMBARKED_START,
    WORK_ON_DAY_OFF,
    FrequencyDay,
    classify_frequency_days,
    extract_frequency_days_pdfplumber,
)


def _day(day: int, scale: str, details: str = "") -> FrequencyDay:
    return FrequencyDay(
        date=date(2022, 1, day),
        scale=scale,
        details=details,
        pdf_line=f"{day:02d}/01 seg {scale} {details} Sobreaviso",
        page=1,
    )


def _mock_pdf_with_text(*pages_text: str):
    pages = []
    for text in pages_text:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)

    pdf = MagicMock()
    pdf.__enter__ = MagicMock(return_value=pdf)
    pdf.__exit__ = MagicMock(return_value=False)
    pdf.pages = pages
    return pdf


def test_extracts_original_full_date_frequency_rows():
    pdf = _mock_pdf_with_text(
        """
        Periodo 01/01/2022
        01/01 Seg HS02 08:00 1082 Sobreaviso
        02/01 Ter FOLG 2025 Turno de 12 Horas
        """
    )

    with patch("pdfplumber.open", return_value=pdf):
        result = extract_frequency_days_pdfplumber(b"fake")

    assert [row.date for row in result] == [date(2022, 1, 1), date(2022, 1, 2)]
    assert result[0].scale == "HS02"
    assert result[0].details == "08:00 1082"
    assert result[1].scale == "FOLG"
    assert result[1].details == "2025"


def test_extracts_day_only_frequency_rows_from_new_petrobras_format():
    pdf = _mock_pdf_with_text(
        """
        RELATORIO DE ACOMPANHAMENTO DE FREQUENCIA
        Periodo : 01.04.2021 a 30.04.2021
        Dia ca lan P/A Obs Peso AF Regime
        01 Q HT51 00:20 2021 +1,50 +8,10 05
        13 T FOLG 12:00 **** -1,00 +23,60 05
        22 Q FOLG 2025 -0,50 +15,10 05
        Ajustes
        13 Ter 07:20 19:00 2026 Hora Extra em Folga 0022 Urgencia
        """
    )

    with patch("pdfplumber.open", return_value=pdf):
        result = extract_frequency_days_pdfplumber(b"fake")

    assert [row.date for row in result] == [
        date(2021, 4, 1),
        date(2021, 4, 13),
        date(2021, 4, 22),
    ]
    assert [row.scale for row in result] == ["HT51", "FOLG", "FOLG"]
    assert result[0].details == "00:20 2021 +1,50 +8,10 05"
    assert result[1].details == "12:00 **** -1,00 +23,60 05"
    assert result[2].details == "2025 -0,50 +15,10 05"

    classified = classify_frequency_days(result)
    assert classified[1].situation == WORK_ON_DAY_OFF
    assert classified[2].situation == DAY_OFF


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
