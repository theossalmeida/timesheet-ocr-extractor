from unittest.mock import AsyncMock, patch
import io
import pypdf
import pytest

from services.guia_ministerial_service import (
    _aggregate,
    _date_sort_key,
    _normalize_worker,
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


# ── _normalize_worker ─────────────────────────────────────────────────────────

def test_normalize_worker_name():
    assert _normalize_worker("João Silva", None) == "JOÃO SILVA"


def test_normalize_worker_strips_whitespace():
    assert _normalize_worker("  ana  ", None) == "ANA"


def test_normalize_worker_id_fallback():
    assert _normalize_worker(None, "29768") == "MOTORISTA 29768"
    assert _normalize_worker("", "29768") == "MOTORISTA 29768"


def test_normalize_worker_unknown():
    assert _normalize_worker(None, None) == "DESCONHECIDO"
    assert _normalize_worker("", "") == "DESCONHECIDO"


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
    records = [{"worker_name": "JOÃO", "worker_id": None, "data": "01/03/2024",
                "primeira_entrada": "08:00", "ultima_saida": "17:00"}]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"
    assert rows[0].entrada_1 == "08:00"
    assert rows[0].saida_1 == "17:00"
    assert rows[0].worker_name == "JOÃO"


def test_aggregate_keeps_earliest_entrada():
    records = [
        {"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "13:00", "ultima_saida": "19:00"},
        {"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "06:00", "ultima_saida": "12:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].entrada_1 == "06:00"


def test_aggregate_keeps_latest_saida():
    records = [
        {"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "06:00", "ultima_saida": "12:00"},
        {"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "13:00", "ultima_saida": "20:30"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].saida_1 == "20:30"


def test_aggregate_two_workers_same_date():
    records = [
        {"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "06:00", "ultima_saida": "14:00"},
        {"worker_name": "PEDRO", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "14:00", "ultima_saida": "22:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 2
    workers = {r.worker_name for r in rows}
    assert "ANA" in workers
    assert "PEDRO" in workers


def test_aggregate_skips_invalid_date():
    records = [
        {"worker_name": "ANA", "worker_id": None, "data": "not-a-date",
         "primeira_entrada": "06:00", "ultima_saida": "14:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 0


def test_aggregate_sorted_by_date():
    records = [
        {"worker_name": "X", "worker_id": None, "data": "15/03/2024",
         "primeira_entrada": "08:00", "ultima_saida": "17:00"},
        {"worker_name": "X", "worker_id": None, "data": "01/03/2024",
         "primeira_entrada": "08:00", "ultima_saida": "17:00"},
    ]
    rows = _aggregate(records)
    assert rows[0].data == "01/03/2024"
    assert rows[1].data == "15/03/2024"


def test_aggregate_hhmm_time_format():
    records = [{"worker_name": "X", "worker_id": None, "data": "01/03/2024",
                "primeira_entrada": "0800", "ultima_saida": "1700"}]
    rows = _aggregate(records)
    assert rows[0].entrada_1 == "08:00"
    assert rows[0].saida_1 == "17:00"


def test_aggregate_worker_name_from_id():
    records = [{"worker_name": None, "worker_id": "99999", "data": "01/03/2024",
                "primeira_entrada": "08:00", "ultima_saida": "17:00"}]
    rows = _aggregate(records)
    assert rows[0].worker_name == "MOTORISTA 99999"


# ── extract_with_guia_ministerial (mocked) ────────────────────────────────────

@pytest.mark.anyio
async def test_extract_calls_gemini_per_chunk():
    pdf = _make_minimal_pdf(25)  # 3 chunks of 10+10+5
    mock_records = [{"worker_name": "TESTE", "worker_id": None, "data": "01/03/2024",
                     "primeira_entrada": "08:00", "ultima_saida": "17:00"}]

    with patch("services.guia_ministerial_service._call_gemini_chunk",
               new=AsyncMock(return_value=mock_records)) as mock_call:
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert mock_call.call_count == 3
    # Same record from 3 chunks → aggregated into 1 row
    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"


@pytest.mark.anyio
async def test_extract_merges_chunks():
    pdf = _make_minimal_pdf(20)  # 2 chunks

    chunk_results = [
        [{"worker_name": "ANA", "worker_id": None, "data": "01/03/2024",
          "primeira_entrada": "06:00", "ultima_saida": "14:00"}],
        [{"worker_name": "ANA", "worker_id": None, "data": "02/03/2024",
          "primeira_entrada": "06:00", "ultima_saida": "14:00"}],
    ]

    with patch("services.guia_ministerial_service._call_gemini_chunk",
               new=AsyncMock(side_effect=chunk_results)):
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert len(rows) == 2
    assert rows[0].data == "01/03/2024"
    assert rows[1].data == "02/03/2024"
