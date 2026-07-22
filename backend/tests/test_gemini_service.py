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


def _mock_error_response(status: int, text: str = "error") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    return response


def test_extract_success():
    data = [
        {"data": "01/03/2024", "entrada_1": "08:00", "saida_1": "17:00",
         "entrada_2": None, "saida_2": None, "ocorrencia_raw": None},
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
    assert rows[0].entrada_1 == "08:00"


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
    data = [{"data": "01/03/2024", "entrada_1": "08:00", "saida_1": "12:00",
              "entrada_2": None, "saida_2": None, "ocorrencia_raw": None}]
    mock_response = _mock_response(200, data)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        rows = asyncio.run(normalize_text_with_gemini("some ocr text"))

    assert len(rows) == 1


def test_extract_with_occurrence():
    data = [{"data": "05/03/2024", "entrada_1": None, "saida_1": None,
              "entrada_2": None, "saida_2": None, "ocorrencia_raw": "FERIAS"}]
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
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00")]
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

    split_mock.assert_called_once_with(b"full-pdf", 5)
    assert gemini_mock.await_args_list[0].args == (b"chunk-1",)
    assert gemini_mock.await_args_list[1].args == (b"chunk-2",)
    assert result == rows


def test_adaptive_extract_retries_failed_chunk_as_single_pages():
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00")]
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
