from unittest.mock import AsyncMock, patch
import io
import json
import pypdf
import pytest

from services.guia_ministerial_service import (
    _aggregate,
    _date_sort_key,
    _record_from_row,
    _single_page_pdf,
    _split_pdf_chunks,
    extract_with_guia_ministerial,
    stream_guia_extraction,
)
from services.local_vision_ocr_service import GuiaLocalRun, GuiaPageOutcome
from models.timesheet import TimesheetRow


def _local_run(pages, *, confidence=1.0, passed=True, reason=None):
    """A finished local read: `pages` is a list of (page_number, record)."""
    return GuiaLocalRun(
        confidence=confidence,
        passed=passed,
        outcomes=[GuiaPageOutcome(number, record, confidence) for number, record in pages],
        reason=reason,
    )


def _guia_record(date="01/03/2024", entrada="08:00", saida="17:00"):
    return {"data": date, "entrada": entrada, "saida": saida}


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    assert rows[0].marcacoes[0] == "08:00"
    assert rows[0].marcacoes[1] == "17:00"


def test_aggregate_keeps_earliest_entrada():
    records = [
        {"data": "01/03/2024", "entrada": "13:00", "saida": "19:00"},
        {"data": "01/03/2024", "entrada": "06:00", "saida": "12:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].marcacoes[0] == "06:00"


def test_aggregate_keeps_latest_saida():
    records = [
        {"data": "01/03/2024", "entrada": "06:00", "saida": "12:00"},
        {"data": "01/03/2024", "entrada": "13:00", "saida": "20:30"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].marcacoes[1] == "20:30"


def test_aggregate_same_date_merges_to_one_row():
    records = [
        {"data": "01/03/2024", "entrada": "06:00", "saida": "14:00"},
        {"data": "01/03/2024", "entrada": "14:00", "saida": "22:00"},
    ]
    rows = _aggregate(records)
    assert len(rows) == 1
    assert rows[0].marcacoes[0] == "06:00"
    assert rows[0].marcacoes[1] == "22:00"


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
    assert rows[0].marcacoes[0] == "08:00"
    assert rows[0].marcacoes[1] == "17:00"


# ── _single_page_pdf ──────────────────────────────────────────────────────────

def test_single_page_pdf_extracts_one_page():
    page = _single_page_pdf(_make_minimal_pdf(5), 3)
    assert len(pypdf.PdfReader(io.BytesIO(page)).pages) == 1


# ── routing: local probe decides, Gemini covers what it cannot ────────────────

@pytest.mark.anyio
async def test_reads_the_document_locally_when_the_probe_is_confident():
    pdf = _make_minimal_pdf(3)
    run = _local_run([
        (1, _guia_record("01/03/2024")),
        (2, _guia_record("02/03/2024")),
        (3, _guia_record("03/03/2024")),
    ])

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[])) as gemini_mock:
        rows = await extract_with_guia_ministerial(pdf)

    gemini_mock.assert_not_awaited()
    assert [row.data for row in rows] == ["01/03/2024", "02/03/2024", "03/03/2024"]


@pytest.mark.anyio
async def test_sends_the_whole_document_to_gemini_when_the_probe_is_weak():
    pdf = _make_minimal_pdf(20)
    run = _local_run([], confidence=0.83, passed=False, reason="probe confidence 83%")

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[_guia_record()])) as gemini_mock:
        rows = await extract_with_guia_ministerial(pdf, chunk_size=10)

    assert gemini_mock.await_count == 2  # one call per chunk of the whole file
    assert rows[0].marcacoes == ["08:00", "17:00"]


@pytest.mark.anyio
async def test_retries_only_the_page_the_local_model_could_not_read():
    """A late failure costs one page of Gemini, not the whole document."""

    pdf = _make_minimal_pdf(3)
    run = _local_run([
        (1, _guia_record("01/03/2024")),
        (2, None),
        (3, _guia_record("03/03/2024")),
    ])

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[_guia_record("02/03/2024")])) as gemini_mock:
        rows = await extract_with_guia_ministerial(pdf)

    gemini_mock.assert_awaited_once()
    retried = gemini_mock.await_args.args[0]
    assert len(pypdf.PdfReader(io.BytesIO(retried)).pages) == 1
    assert [row.data for row in rows] == ["01/03/2024", "02/03/2024", "03/03/2024"]


@pytest.mark.anyio
async def test_uses_gemini_when_local_vision_is_unavailable():
    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=None)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[_guia_record()])) as gemini_mock:
        rows = await extract_with_guia_ministerial(_make_minimal_pdf(1))

    gemini_mock.assert_awaited_once()
    assert len(rows) == 1


@pytest.mark.anyio
async def test_never_reads_a_guia_with_text_ocr():
    """Regression: a guia's only readable text is its signature footer.

    Scraping it produced the signing date and time as the work date and both
    punches, so the guia path must reach a vision model or return nothing.
    """

    import services.tesseract_ocr_service as tesseract

    footer = [(1, "Documento assinado eletronicamente por FULANO, em 28/08/2026, as 13:25:53")]
    with patch.object(tesseract, "ocr_pdf_page_texts", return_value=footer) as ocr_mock, \
         patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=None)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[])):
        rows = await extract_with_guia_ministerial(_make_minimal_pdf(1))

    ocr_mock.assert_not_called()
    assert rows == []


def test_record_from_row_maps_a_single_service_form():
    row = TimesheetRow(data="01/03/2024", marcacoes=["07:30", "12:00", "19:45"], ocr_confidence="low",
                       ocr_warning="Baixa confianca OCR: conferir esta linha no PDF original.")

    record = _record_from_row(row)

    assert record["data"] == "01/03/2024"
    assert record["entrada"] == "07:30"
    assert record["saida"] == "19:45"
    assert record["_confidence"] == "low"


def test_record_from_row_without_a_second_punch():
    assert _record_from_row(TimesheetRow(data="01/03/2024", marcacoes=["07:30"]))["saida"] is None


# ── streaming ────────────────────────────────────────────────────────────────

async def _stream_events(pdf, stem="504"):
    events = []
    async for chunk in stream_guia_extraction(pdf, stem):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


@pytest.mark.anyio
async def test_stream_reports_local_as_the_provider_when_the_probe_passes():
    run = _local_run([(1, _guia_record())])

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[])) as gemini_mock:
        events = await _stream_events(_make_minimal_pdf(1))

    gemini_mock.assert_not_awaited()
    assert events[-1]["provider"] == "local-vision-guia"
    assert events[-1]["rows_extracted"] == 1


@pytest.mark.anyio
async def test_stream_reports_both_providers_when_a_page_falls_back():
    run = _local_run([(1, _guia_record("01/03/2024")), (2, None)])

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)), \
         patch("services.guia_ministerial_service._process_chunk_gemini",
               new=AsyncMock(return_value=[_guia_record("02/03/2024")])):
        events = await _stream_events(_make_minimal_pdf(2))

    assert events[-1]["provider"] == "local-vision-guia+gemini-guia"
    assert events[-1]["rows_extracted"] == 2


@pytest.mark.anyio
async def test_stream_meters_gemini_and_reports_it_as_the_provider():
    from services import ai_usage

    async def fake_gemini(chunk_bytes):
        ai_usage.record("gemini", "gemini-3.8-flash", "extract",
                        {"prompt_tokens": 1000, "cached_tokens": 0, "output_tokens": 500,
                         "thought_tokens": 0, "total_tokens": 1500})
        return [_guia_record()]

    weak = _local_run([], confidence=0.5, passed=False, reason="probe confidence 50%")
    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=weak)), \
         patch("services.guia_ministerial_service._process_chunk_gemini", new=fake_gemini):
        events = await _stream_events(_make_minimal_pdf(1))

    done = events[-1]
    assert done["type"] == "done"
    assert done["provider"] == "gemini-guia"
    assert done["rows_extracted"] == 1
    # The cost of the call travels with the result so it can be billed.
    assert [call["prompt_tokens"] for call in done["ai_usage"]] == [1000]
