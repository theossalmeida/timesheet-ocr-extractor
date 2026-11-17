import io

import pdfplumber
import pymupdf
import pytest

from services.frequency_report_parser import is_frequency_report, parse_frequency_report
from services.pdfplumber_service import extract_with_pdfplumber

TITLE = "RELATORIO DE ACOMPANHAMENTO DE FREQUENCIA"


def _text(page, x, y, value):
    page.insert_text((x, y), value, fontsize=8)


def _month_header(page, period):
    _text(page, 213, 40, TITLE)
    _text(page, 307, 80, "Periodo")
    _text(page, 488, 80, period.split()[0])
    _text(page, 528, 80, "a")
    _text(page, 533, 80, period.split()[-1])
    _text(page, 38, 200, "Dia")
    _text(page, 101, 200, "Entrada")
    _text(page, 133, 200, "Saida")
    _text(page, 221, 200, "PHT")


def _day(page, y, day_month, *, punches=(), dif=None, scale="HT05"):
    _text(page, 19, y, day_month)
    _text(page, 52, y, "sex")
    _text(page, 77, y, scale)
    for x, value in zip((105, 133, 158, 190), punches):
        _text(page, x, y, value)
    if dif:
        _text(page, 304, y, dif)
    _text(page, 418, y, "+0,67")


def _pdf(builder) -> bytes:
    doc = pymupdf.open()
    builder(doc)
    return doc.tobytes()


def _parse(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return parse_frequency_report(pdf)


def _october(doc):
    page = doc.new_page()
    _month_header(page, "01/10/2021 31/10/2021")
    _day(page, 220, "01/10", punches=("22:55", "07:30"), dif="00:35")
    _day(page, 240, "02/10", scale="FOLG")
    _day(page, 260, "04/10", punches=("07:06", "11:30", "12:30", "15:30"), dif="00:30")
    _day(page, 280, "21/10", scale="HT02")


def test_reads_only_entrada_and_saida_of_worked_days():
    rows = _parse(_pdf(_october))

    assert [(row.data, row.marcacoes) for row in rows] == [
        ("01/10/2021", ["22:55", "07:30"]),
        ("04/10/2021", ["07:06", "11:30", "12:30", "15:30"]),
    ]


def test_day_with_more_than_two_pairs_is_joined_from_its_repeated_lines():
    def build(doc):
        page = doc.new_page()
        _month_header(page, "01/12/2025 31/12/2025")
        _day(page, 220, "09/12", punches=("07:39", "11:49", "12:28", "12:33"))
        _day(page, 240, "09/12", punches=("12:34", "16:09"))

    assert [(row.data, row.marcacoes) for row in _parse(_pdf(build))] == [
        ("09/12/2025", ["07:39", "11:49", "12:28", "12:33", "12:34", "16:09"]),
    ]


def test_difference_columns_are_never_read_as_punches():
    rows = _parse(_pdf(_october))

    assert all("00:35" not in row.marcacoes and "00:30" not in row.marcacoes for row in rows)


def test_table_continuing_on_the_next_page_keeps_the_period_year():
    def build(doc):
        first = doc.new_page()
        _month_header(first, "01/10/2021 31/10/2021")
        _day(first, 220, "30/10", punches=("23:00", "07:32"))
        second = doc.new_page()
        _text(second, 221, 60, "PHT")
        _day(second, 80, "31/10", punches=("23:07", "07:30"))

    assert [row.data for row in _parse(_pdf(build))] == ["30/10/2021", "31/10/2021"]


def test_period_crossing_the_new_year_resolves_each_day_to_its_own_year():
    def build(doc):
        page = doc.new_page()
        _month_header(page, "26/12/2021 25/01/2022")
        _day(page, 220, "31/12", punches=("08:00", "17:00"))
        _day(page, 240, "02/01", punches=("08:00", "17:00"))

    assert [row.data for row in _parse(_pdf(build))] == ["31/12/2021", "02/01/2022"]


def test_rubricas_page_is_not_mistaken_for_a_day_table():
    def build(doc):
        _october(doc)
        page = doc.new_page()
        _text(page, 25, 40, "Rubricas salariais do mes")
        _text(page, 25, 100, "01/10/2021")
        _text(page, 100, 100, "0532")
        _text(page, 200, 100, "HE Troca de Turno")
        _text(page, 330, 100, "000:04")

    assert len(_parse(_pdf(build))) == 2


def test_other_documents_are_not_treated_as_frequency_reports():
    def build(doc):
        page = doc.new_page()
        _text(page, 50, 50, "FOLHA DE PONTO 01/03/2024 08:00 17:00")

    with pdfplumber.open(io.BytesIO(_pdf(build))) as pdf:
        assert is_frequency_report(pdf) is False


def test_pdfplumber_entry_point_prefers_the_day_tables_over_other_tables():
    rows = extract_with_pdfplumber(_pdf(_october))

    assert [row.data for row in rows] == ["01/10/2021", "04/10/2021"]
