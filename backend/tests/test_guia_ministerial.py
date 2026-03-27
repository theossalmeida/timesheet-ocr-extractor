from unittest.mock import AsyncMock, patch
import io
import pypdf
import pytest

from services.guia_ministerial_service import (
    _aggregate,
    _date_sort_key,
    _parse_gemini_response,
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


def _gemini_payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


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


# ── _parse_gemini_response ────────────────────────────────────────────────────

def test_parse_gemini_response_records_key():
    payload = _gemini_payload('{"records": [{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}]}')
    result = _parse_gemini_response(payload)
    assert len(result) == 1
    assert result[0]["data"] == "01/03/2024"


def test_parse_gemini_response_top_level_array():
    payload = _gemini_payload('[{"data": "02/03/2024", "entrada": "09:00", "saida": "18:00"}]')
    result = _parse_gemini_response(payload)
    assert len(result) == 1


def test_parse_gemini_response_invalid_json():
    assert _parse_gemini_response(_gemini_payload("not json")) == []


def test_parse_gemini_response_markdown_fenced():
    assert _parse_gemini_response(_gemini_payload('```json\n{"records": []}\n```')) == []


# ── extract_with_guia_ministerial (mocked) ────────────────────────────────────

@pytest.mark.anyio
async def test_extract_calls_process_chunk_per_chunk():
    pdf = _make_minimal_pdf(25)  # 3 chunks of 10+10+5
    mock_records = [{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}]

    with patch("services.guia_ministerial_service._process_chunk",
               new=AsyncMock(return_value=mock_records)) as mock_call:
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

    with patch("services.guia_ministerial_service._process_chunk",
               new=AsyncMock(side_effect=chunk_results)):
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert len(rows) == 2
    assert rows[0].data == "01/03/2024"
    assert rows[1].data == "02/03/2024"
