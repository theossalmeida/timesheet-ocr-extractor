from unittest.mock import patch
import io
import pypdf
import pytest

from services.guia_ministerial_service import (
    _aggregate,
    _date_sort_key,
    _extract_records_from_text,
    _split_pdf_chunks,
    extract_with_guia_ministerial,
)
from models.timesheet import TimesheetRow


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_minimal_pdf(n_pages: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── _date_sort_key ────────────────────────────────────────────────────────────

def test_date_sort_key_normal():
    assert _date_sort_key("01/03/2024") == (2024, 3, 1)


def test_date_sort_key_invalid():
    assert _date_sort_key("invalid") == (9999, 99, 99)


def test_date_sort_key_ordering():
    dates = ["15/06/2023", "01/01/2023", "31/12/2022"]
    assert sorted(dates, key=_date_sort_key) == ["31/12/2022", "01/01/2023", "15/06/2023"]


# ── _split_pdf_chunks ─────────────────────────────────────────────────────────

def test_split_single_chunk():
    pdf = _make_minimal_pdf(5)
    chunks = _split_pdf_chunks(pdf, chunk_size=10)
    assert len(chunks) == 1
    reader = pypdf.PdfReader(io.BytesIO(chunks[0]))
    assert len(reader.pages) == 5


def test_split_multiple_chunks():
    pdf = _make_minimal_pdf(25)
    chunks = _split_pdf_chunks(pdf, chunk_size=10)
    assert len(chunks) == 3
    page_counts = [len(pypdf.PdfReader(io.BytesIO(c)).pages) for c in chunks]
    assert page_counts == [10, 10, 5]


def test_split_exact_boundary():
    pdf = _make_minimal_pdf(20)
    chunks = _split_pdf_chunks(pdf, chunk_size=10)
    assert len(chunks) == 2


# ── _aggregate ────────────────────────────────────────────────────────────────

def test_aggregate_single_record():
    records = [{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"
    assert rows[0].entrada_1 == "08:00"
    assert rows[0].saida_1 == "17:00"


def test_aggregate_keeps_earliest_entrada():
    records = [
        {"data": "01/03/2024", "entrada": "13:00", "saida": "19:00"},
        {"data": "01/03/2024", "entrada": "06:00", "saida": "12:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].entrada_1 == "06:00"


def test_aggregate_keeps_latest_saida():
    records = [
        {"data": "01/03/2024", "entrada": "06:00", "saida": "12:00"},
        {"data": "01/03/2024", "entrada": "13:00", "saida": "20:30"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].saida_1 == "20:30"


def test_aggregate_same_date_merges_to_one_row():
    records = [
        {"data": "01/03/2024", "entrada": "06:00", "saida": "14:00"},
        {"data": "01/03/2024", "entrada": "14:00", "saida": "22:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].entrada_1 == "06:00"
    assert rows[0].saida_1 == "22:00"


def test_aggregate_skips_invalid_date():
    records = [{"data": "not-a-date", "entrada": "06:00", "saida": "14:00"}]
    rows = _aggregate(records)
    assert len(rows) == 0


def test_aggregate_sorted_by_date():
    records = [
        {"data": "15/03/2024", "entrada": "08:00", "saida": "17:00"},
        {"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"},
    ]
    rows = _aggregate(records)
    assert rows[0].data == "01/03/2024"
    assert rows[1].data == "15/03/2024"


def test_aggregate_hhmm_time_format():
    records = [{"data": "01/03/2024", "entrada": "0800", "saida": "1700"}]
    rows = _aggregate(records)
    assert rows[0].entrada_1 == "08:00"
    assert rows[0].saida_1 == "17:00"


# ── _extract_records_from_text (local OCR parser) ────────────────────────────

def test_extract_records_from_text_finds_date_and_times():
    text = "25/01/2024 Hora Entrada 06:30 Hora Saida 14:50"
    records = _extract_records_from_text(text)
    assert records == [{"data": "25/01/2024", "entrada": "06:30", "saida": "14:50"}]


def test_extract_records_from_text_uses_earliest_and_latest_time_on_line():
    text = "01/03/2024 08:00 12:00 13:00 17:00"
    records = _extract_records_from_text(text)
    assert records == [{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}]


def test_extract_records_from_text_expands_two_digit_year():
    text = "05/02/24 07:00 15:00"
    records = _extract_records_from_text(text)
    assert records[0]["data"] == "05/02/2024"


def test_extract_records_from_text_ignores_lines_without_times():
    text = "25/01/2024 apenas uma data sem horario\noutra linha qualquer"
    assert _extract_records_from_text(text) == []


def test_extract_records_from_text_ignores_lines_without_dates():
    text = "06:30 14:50 sem data nesta linha"
    assert _extract_records_from_text(text) == []


def test_extract_records_from_text_multiple_lines():
    text = "01/03/2024 06:00 14:00\n02/03/2024 07:00 15:00"
    records = _extract_records_from_text(text)
    assert len(records) == 2
    assert records[0]["data"] == "01/03/2024"
    assert records[1]["data"] == "02/03/2024"


# ── extract_with_guia_ministerial (mocked) ────────────────────────────────────

@pytest.mark.anyio
async def test_extract_calls_process_chunk_per_chunk():
    pdf = _make_minimal_pdf(25)  # 3 chunks of 10+10+5
    mock_records = [{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}]

    with patch("services.guia_ministerial_service._process_chunk_tesseract",
               return_value=mock_records) as mock_call:
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert mock_call.call_count == 3
    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"


@pytest.mark.anyio
async def test_extract_merges_chunks():
    pdf = _make_minimal_pdf(20)  # 2 chunks

    chunk_results = [
        [{"data": "01/03/2024", "entrada": "06:00", "saida": "14:00"}],
        [{"data": "02/03/2024", "entrada": "06:00", "saida": "14:00"}],
    ]

    with patch("services.guia_ministerial_service._process_chunk_tesseract",
               side_effect=chunk_results):
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert len(rows) == 2
    assert rows[0].data == "01/03/2024"
    assert rows[1].data == "02/03/2024"
