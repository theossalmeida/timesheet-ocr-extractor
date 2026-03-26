import os
from unittest.mock import MagicMock, patch
from services.pdfplumber_service import extract_with_pdfplumber

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _mock_pdf_with_table(table: list[list[str | None]]):
    page = MagicMock()
    page.extract_tables.return_value = [table]
    page.extract_text.return_value = ""
    pdf = MagicMock()
    pdf.pages = [page]
    return pdf


def _mock_pdf_no_table():
    page = MagicMock()
    page.extract_tables.return_value = []
    page.extract_text.return_value = ""
    pdf = MagicMock()
    pdf.pages = [page]
    return pdf


def test_returns_none_when_no_table():
    pdf = _mock_pdf_no_table()
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is None


def test_returns_none_when_no_date_column():
    table = [
        ["08:00", "12:00", "13:00", "17:00", "FERIAS"],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is None


def test_extracts_rows_from_table():
    table = [
        ["01/03/2024", "08:00", "12:00", "13:00", "17:00", ""],
        ["04/03/2024", "08:30", "12:00", "13:00", "17:30", ""],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is not None
    assert len(result) == 2
    assert result[0].data == "01/03/2024"
    assert result[0].entrada_1 == "08:00"
    assert result[0].saida_1 == "12:00"


def test_includes_occurrence_only_rows():
    table = [
        ["05/03/2024", None, None, None, None, "FERIAS"],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is not None
    assert len(result) == 1
    assert result[0].data == "05/03/2024"
    assert result[0].entrada_1 is None
    assert result[0].ocorrencia_raw == "FERIAS"


def test_skips_rows_without_date():
    table = [
        ["HEADER", "ENT1", "SAI1", "ENT2", "SAI2", "OCC"],
        ["01/03/2024", "08:00", "12:00", "13:00", "17:00", ""],
        ["not-a-date", "08:00", "12:00", "13:00", "17:00", ""],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is not None
    assert len(result) == 1


def test_normalizes_date_format():
    table = [
        ["01-03-2024", "08:00", "12:00", None, None, None],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is not None
    assert result[0].data == "01/03/2024"


def test_fixture_native_pdf():
    path = os.path.join(FIXTURES_DIR, "native_table.pdf")
    if not os.path.exists(path):
        import pytest
        pytest.skip("native_table.pdf fixture not found")
    with open(path, "rb") as f:
        pdf_bytes = f.read()
    # Should not raise; may return None or list depending on PDF content
    result = extract_with_pdfplumber(pdf_bytes)
    assert result is None or isinstance(result, list)
