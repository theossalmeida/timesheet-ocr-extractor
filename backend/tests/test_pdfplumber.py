import os
from unittest.mock import MagicMock, patch
from services.pdfplumber_service import extract_with_pdfplumber, _parse_weekday_first_rows

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
    assert result[0].marcacoes[0] == "08:00"
    assert result[0].marcacoes[1] == "12:00"


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
    assert result[0].marcacoes == []
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


def test_ignores_acrescimos_column():
    """Columns labelled 'Acréscimos' must not be mapped to entrada/saída slots."""
    table = [
        ["Data", "Entrada", "Saída", "Acréscimos"],
        ["01/03/2024", "08:00", "17:00", "00:30"],
        ["02/03/2024", "08:00", "17:00", "00:00"],
    ]
    pdf = _mock_pdf_with_table(table)
    with patch("pdfplumber.open", return_value=pdf):
        result = extract_with_pdfplumber(b"fake")
    assert result is not None
    assert len(result) == 2
    assert result[0].marcacoes == ["08:00", "17:00"]


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


# ── _parse_weekday_first_rows ("Cartao de Ponto ES." layout) ──────────────────

def test_weekday_first_normal_two_pair_day():
    text = "Seg 04/01/21 00307 466 19:08 22:51 00:50 07:00 Entrada em Atraso 00:08 DEBITO BANCO DE HORAS 09:00"
    rows = _parse_weekday_first_rows(text)
    assert len(rows) == 1
    assert rows[0].data == "04/01/2021"
    assert rows[0].marcacoes == ["19:08", "22:51", "00:50", "07:00"]


def test_weekday_first_overtime_three_pair_day():
    text = "Sab 02/01/21 00307 466 19:02 22:32 00:31 07:00 07:00 07:06 Hora Extra 00:06 CREDITO BANCO DE HORAS 09:06"
    rows = _parse_weekday_first_rows(text)
    assert len(rows) == 1
    assert rows[0].marcacoes == ["19:02", "22:32", "00:31", "07:00", "07:00", "07:06"]


def test_weekday_first_zero_mark_folga_day():
    text = "Dom 03/01/21 466 FOLGA"
    rows = _parse_weekday_first_rows(text)
    assert len(rows) == 1
    assert rows[0].data == "03/01/2021"
    assert rows[0].marcacoes == []
    assert rows[0].ocorrencia_tipo == "folga"


def test_weekday_first_two_digit_year_normalized():
    text = "Ter 12/01/21 00452 466 19:00 23:01 01:00 07:00"
    rows = _parse_weekday_first_rows(text)
    assert rows[0].data == "12/01/2021"


def test_weekday_first_no_discarding_odd_trailing_mark():
    """Days without irregularity text end with a trailing number (QTDE) -
    per the user's explicit call, it is kept as a mark, not dropped."""
    text = "Dom 10/01/21 00307 466 18:59 22:21 00:22 07:03 09:03"
    rows = _parse_weekday_first_rows(text)
    assert rows[0].marcacoes == ["18:59", "22:21", "00:22", "07:03", "09:03"]


def test_weekday_first_multiple_days():
    text = (
        "Sex 01/01/21 466 CONFRATERNIZACAO UNIVERSAL\n"
        "Sab 02/01/21 00307 466 19:02 22:32\n"
        "Dom 03/01/21 466 FOLGA\n"
    )
    rows = _parse_weekday_first_rows(text)
    assert [r.data for r in rows] == ["01/01/2021", "02/01/2021", "03/01/2021"]
