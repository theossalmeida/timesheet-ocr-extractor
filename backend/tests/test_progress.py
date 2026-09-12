"""The progress contract every extraction mode has to honour.

The UI renders one bar from these frames without knowing which pipeline ran, so
the phases, their order, and the meaning of chunk/total have to be identical
across modes. These tests pin that down at the stream boundary.
"""
import io
import json
from unittest.mock import AsyncMock, patch

import pypdf
import pytest

from services import progress


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _pdf(pages: int = 2) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _events(stream) -> list[dict]:
    events = []
    async for chunk in stream:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _phases(events: list[dict]) -> list[str]:
    return [e["phase"] for e in events if e.get("type") == "progress"]


# ── frame shape ──────────────────────────────────────────────────────────────

def test_every_progress_frame_carries_a_phase_and_countable_units():
    frame = json.loads(progress.processing(2, 5, step="gemini")[6:])

    assert frame["type"] == "progress"
    assert frame["phase"] == progress.PROCESSING
    assert (frame["chunk"], frame["total"]) == (2, 5)
    assert frame["step"] == "gemini"


def test_a_stage_with_nothing_to_count_still_reports_one_unit():
    """The bar never needs a special case for an indeterminate stage."""

    for frame in (progress.detecting(), progress.building()):
        payload = json.loads(frame[6:])
        assert payload["total"] == 1


def test_phases_use_one_wording_regardless_of_the_pipeline():
    assert json.loads(progress.detecting()[6:])["message"] == progress.DETECTING_LABEL
    assert json.loads(progress.processing()[6:])["message"] == progress.PROCESSING_LABEL
    assert json.loads(progress.building()[6:])["message"] == progress.BUILDING_LABEL


def test_keep_alive_is_a_comment_and_never_moves_the_bar():
    assert progress.keep_alive().startswith(":")


# ── every mode reports the same phases, in the same order ────────────────────

@pytest.mark.anyio
async def test_guia_reports_the_standard_phases():
    from services.guia_ministerial_service import stream_guia_extraction
    from services.local_vision_ocr_service import GuiaLocalRun, GuiaPageOutcome

    run = GuiaLocalRun(1.0, True, [GuiaPageOutcome(1, {"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}, 1.0)], None)
    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=run)):
        events = await _events(stream_guia_extraction(_pdf(1), "doc"))

    assert _phases(events)[0] == progress.DETECTING
    assert progress.BUILDING in _phases(events)
    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_contracheque_reports_the_standard_phases():
    from services.contracheque_service import stream_contracheque_extraction

    page = {"competencia": "03/2024", "itens": [{"descricao": "Salario", "valor": 100.0}]}
    with patch("services.contracheque_service._extract_all_pdfplumber", return_value=([page], [])):
        events = await _events(stream_contracheque_extraction(_pdf(1), "doc"))

    phases = _phases(events)
    assert phases[0] == progress.DETECTING
    assert progress.BUILDING in phases
    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_frequencia_reports_the_standard_phases():
    from services import frequency_cycle_service as service

    from datetime import date

    day = service.FrequencyDay(
        date=date(2024, 3, 1), scale="T1", details="", pdf_line="01/03/2024 T1", page=1
    )
    with patch.object(service, "_extract_frequency_days_and_ocr_chunks", return_value=([day], [])):
        events = await _events(service.stream_frequency_cycle_extraction(_pdf(1), "doc"))

    phases = _phases(events)
    assert phases[0] == progress.DETECTING
    assert progress.BUILDING in phases
    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_a_stream_ends_as_soon_as_the_work_does():
    """Regression: waiting on the progress queue alone held the stream open for
    the whole keep-alive timeout after the pipeline had already finished, so a
    native PDF that parsed instantly still took 10s to reach the browser."""

    import asyncio
    import main
    from models.timesheet import TimesheetRow

    rows = [TimesheetRow(data="01/03/2024", marcacoes=["08:00", "17:00"])]
    with patch("main.detect_pdf_type", return_value="native"), \
         patch("main.extract_with_pdfplumber", return_value=rows), \
         patch("main.get_scanned_page_bytes", return_value=None), \
         patch("main.build_excel", return_value=b"PKfake"), \
         patch("main.build_csv", return_value="a,b"):
        events = await asyncio.wait_for(
            _events(main.stream_timesheet_extraction(_pdf(1), "doc")), timeout=5
        )

    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_an_ocr_chunk_does_not_wait_out_the_keep_alive_interval():
    """Regression: the keep-alive loop slept its full interval before
    re-checking, so every OCR chunk cost 15s even when it finished at once."""

    import asyncio
    from services import contracheque_service as cs

    page = {"competencia": "03/2024", "itens": [{"descricao": "Salario", "valor": 100.0}]}
    with patch.object(cs, "_extract_all_pdfplumber", return_value=([page], [1])), \
         patch.object(cs, "_split_pages_by_index", return_value=[b"x"]), \
         patch.object(cs, "_make_chunks", return_value=[b"x", b"y"]), \
         patch.object(cs, "_process_chunk_tesseract", return_value=[page]), \
         patch("services.contracheque_excel_builder.build_contracheque_excel", return_value=b"PK"):
        events = await asyncio.wait_for(
            _events(cs.stream_contracheque_extraction(_pdf(2), "doc")), timeout=5
        )

    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_processing_counts_never_go_backwards():
    """Chunks complete out of order; the count must still rise monotonically."""

    from services.guia_ministerial_service import stream_guia_extraction
    from services.local_vision_ocr_service import GuiaLocalRun

    weak = GuiaLocalRun(0.5, False, [], "probe confidence 50%")
    record = {"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}

    async def fake_gemini(chunk_bytes, semaphore=None):
        import asyncio

        await asyncio.sleep(0.01)
        return [record]

    with patch("services.guia_ministerial_service._run_local", new=AsyncMock(return_value=weak)), \
         patch("services.guia_ministerial_service._process_chunk_gemini", new=fake_gemini):
        events = await _events(stream_guia_extraction(_pdf(6), "doc", chunk_size=2))

    counts = [e["chunk"] for e in events if e.get("phase") == progress.PROCESSING]
    assert counts == sorted(counts)
    assert counts[0] == 0
    assert counts[-1] == max(e["total"] for e in events if e.get("phase") == progress.PROCESSING)
