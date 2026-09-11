import json
import pytest
import asyncio
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from models.timesheet import TimesheetRow
from services.gemini_service import (
    GeminiExtractionError,
    extract_with_gemini,
    extract_with_gemini_adaptive,
    normalize_text_with_gemini,
    _gemini_url,
)


def _mock_response(status: int, data: list[dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    text_content = json.dumps(data)
    response.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{"text": text_content}]
            }
        }]
    }
    response.text = text_content
    return response


@pytest.fixture(autouse=True)
def configure_gemini(monkeypatch):
    from services import gemini_service as service

    monkeypatch.setattr(service.settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(service.settings, "GEMINI_MODEL", "gemini-3.1-pro-preview")


def _mock_error_response(status: int, text: str = "error") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    return response


def test_extract_success():
    data = [
        {"data": "01/03/2024", "marcacoes": ["08:00", "17:00"], "ocorrencia_raw": None},
    ]
    mock_response = _mock_response(200, data)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        rows = asyncio.run(extract_with_gemini(b"fake pdf"))

    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"
    assert rows[0].marcacoes[0] == "08:00"


def test_extract_raises_on_non_200():
    mock_response = _mock_error_response(429, "rate limit")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GeminiExtractionError):
            asyncio.run(extract_with_gemini(b"fake"))


def test_extract_wraps_timeout_as_gemini_error():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("services.gemini_service.asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(GeminiExtractionError):
            asyncio.run(extract_with_gemini(b"fake"))

    assert mock_client.post.await_count == 2


def test_extract_raises_on_invalid_json():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "not valid json {{{"}]}}]
    }
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GeminiExtractionError):
            asyncio.run(extract_with_gemini(b"fake"))


def test_normalize_text_success():
    data = [{"data": "01/03/2024", "marcacoes": ["08:00", "12:00"], "ocorrencia_raw": None}]
    mock_response = _mock_response(200, data)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        rows = asyncio.run(normalize_text_with_gemini("some ocr text"))

    assert len(rows) == 1


def test_extract_with_occurrence():
    data = [{"data": "05/03/2024", "marcacoes": [], "ocorrencia_raw": "FERIAS"}]
    mock_response = _mock_response(200, data)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        rows = asyncio.run(extract_with_gemini(b"fake"))

    assert rows[0].ocorrencia_tipo == "ferias"
    assert rows[0].ocorrencia_raw == "FERIAS"


def test_adaptive_extract_chunks_large_pdf_before_gemini():
    rows = [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])]
    reader = MagicMock()
    reader.pages = [object()] * 6

    with (
        patch("services.gemini_service.pypdf.PdfReader", return_value=reader),
        patch(
            "services.gemini_service._split_pdf_into_chunks",
            return_value=[b"chunk-1", b"chunk-2"],
        ) as split_mock,
        patch(
            "services.gemini_service.extract_with_gemini",
            AsyncMock(side_effect=[rows, []]),
        ) as gemini_mock,
    ):
        result = asyncio.run(extract_with_gemini_adaptive(b"full-pdf", chunk_size=5))

    split_mock.assert_called_once_with(b"full-pdf", 5, reader=reader)
    assert gemini_mock.await_args_list[0].args == (b"chunk-1",)
    assert gemini_mock.await_args_list[1].args == (b"chunk-2",)
    assert result == rows


def test_adaptive_extract_retries_failed_chunk_as_single_pages():
    rows = [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])]
    reader = MagicMock()
    reader.pages = [object()] * 3

    with (
        patch("services.gemini_service.pypdf.PdfReader", return_value=reader),
        patch(
            "services.gemini_service._split_pdf_into_chunks",
            side_effect=[[b"chunk"], [b"page-1", b"page-2"]],
        ) as split_mock,
        patch(
            "services.gemini_service.extract_with_gemini",
            AsyncMock(side_effect=[
                GeminiExtractionError("timeout"),
                rows,
                [],
            ]),
        ) as gemini_mock,
    ):
        result = asyncio.run(extract_with_gemini_adaptive(b"full-pdf", chunk_size=2))

    assert split_mock.call_count == 2
    assert gemini_mock.await_args_list[0].args == (b"chunk",)
    assert gemini_mock.await_args_list[1].args == (b"page-1",)
    assert gemini_mock.await_args_list[2].args == (b"page-2",)
    assert result == rows

def test_extract_parses_records_payload():
    response = MagicMock()
    response.status_code = 200
    response.text = '{"records": []}'
    response.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{"text": json.dumps({
                    "records": [
                        {"data": "08/08/2022", "entrada": "13:30", "saida": "23:02", "confidence": "high"},
                    ]
                })}]
            }
        }]
    }
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        rows = asyncio.run(extract_with_gemini(b"fake pdf"))

    assert len(rows) == 1
    assert rows[0].data == "08/08/2022"
    assert rows[0].marcacoes == ["13:30", "23:02"]
    assert rows[0].ocr_confidence == "high"


def test_gemini_url_uses_configured_model(monkeypatch):
    from services import gemini_service as service

    monkeypatch.setattr(service.settings, "GEMINI_MODEL", "gemini-custom")

    assert _gemini_url().endswith("/models/gemini-custom:generateContent")

@pytest.mark.asyncio
async def test_adaptive_chunks_overlap_with_bounded_concurrency_and_ordered_rows(monkeypatch):
    from services import ai_usage, gemini_service as service

    chunks = [bytes([index]) for index in range(7)]
    reader = MagicMock()
    reader.pages = [object()] * 14
    active = 0
    peak_active = 0
    completed = []

    async def extract(chunk, *, client=None):
        nonlocal active, peak_active
        index = chunk[0]
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.002 * (7 - index))
        ai_usage.record("gemini", "test-model", "extract", {"total_tokens": index + 1})
        completed.append(index)
        active -= 1
        return [TimesheetRow(data=f"{index + 1:02d}/03/2024", marcacoes=["08:00"])]

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda *args, **kwargs: chunks)
    monkeypatch.setattr(service, "extract_with_gemini", extract)

    with ai_usage.recording() as calls:
        rows = await service.extract_with_gemini_adaptive(b"pdf")

    assert peak_active == service.GEMINI_MAX_CONCURRENT_CHUNKS
    assert completed != list(range(7))
    assert [row.data for row in rows] == [f"{index + 1:02d}/03/2024" for index in range(7)]
    assert len(calls) == 7
    assert sum(call["total_tokens"] for call in calls) == 28


@pytest.mark.asyncio
async def test_adaptive_waits_for_started_calls_before_propagating_unexpected_error(monkeypatch):
    from services import ai_usage, gemini_service as service

    reader = MagicMock()
    reader.pages = [object()] * 4
    completed = asyncio.Event()

    async def extract(chunk, *, client=None):
        if chunk == b"bad":
            raise ValueError("invalid response")
        await asyncio.sleep(0.01)
        ai_usage.record("gemini", "test-model", "extract", {"total_tokens": 9})
        completed.set()
        return []

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda *args, **kwargs: [b"bad", b"good"])
    monkeypatch.setattr(service, "extract_with_gemini", extract)

    with ai_usage.recording() as calls:
        with pytest.raises(ValueError, match="invalid response"):
            await service.extract_with_gemini_adaptive(b"pdf")
        assert completed.is_set()
        assert len(calls) == 1


def test_adaptive_reads_source_pdf_once():
    from io import BytesIO
    import pypdf
    from services import gemini_service as service

    writer = pypdf.PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)

    with (
        patch.object(service.pypdf, "PdfReader", wraps=pypdf.PdfReader) as reader,
        patch.object(service, "extract_with_gemini", AsyncMock(return_value=[])) as extract,
    ):
        assert asyncio.run(service.extract_with_gemini_adaptive(output.getvalue())) == []

    assert reader.call_count == 1
    assert extract.await_count == 3


@pytest.mark.asyncio
async def test_adaptive_concurrent_fallback_preserves_partial_rows_and_page_order(monkeypatch):
    from services import gemini_service as service

    reader = MagicMock()
    reader.pages = [object()] * 6
    chunks = {
        b"pdf": [b"first", b"second", b"third"],
        b"first": [b"page-1", b"page-2"],
        b"third": [b"page-5", b"page-6"],
    }
    responses = {
        b"first": GeminiExtractionError("first chunk failed"),
        b"page-1": [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])],
        b"page-2": GeminiExtractionError("unreadable page"),
        b"second": [TimesheetRow(data="03/03/2024", marcacoes=["08:00"])],
        b"third": GeminiExtractionError("third chunk failed"),
        b"page-5": GeminiExtractionError("unreadable page"),
        b"page-6": [TimesheetRow(data="06/03/2024", marcacoes=["08:00"])],
    }

    async def extract(chunk, *, client=None):
        await asyncio.sleep(0)
        result = responses[chunk]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda pdf, *args, **kwargs: chunks[pdf])
    monkeypatch.setattr(service, "extract_with_gemini", extract)

    rows = await service.extract_with_gemini_adaptive(b"pdf")

    assert [row.data for row in rows] == ["01/03/2024", "03/03/2024", "06/03/2024"]
