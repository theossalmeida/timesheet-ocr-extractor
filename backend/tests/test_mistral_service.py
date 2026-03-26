import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from services.mistral_service import extract_with_mistral, MistralExtractionError
from models.timesheet import TimesheetRow


def _make_upload_response(file_id: str = "file-123") -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"id": file_id}
    return r


def _make_ocr_response(markdown: str = "| 01/03/2024 | 08:00 | 17:00 |") -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"pages": [{"markdown": markdown, "index": 0}]}
    r.text = ""
    return r


def _make_error_response(status: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "error"
    return r


def test_full_pipeline_success():
    upload_r = _make_upload_response("file-abc")
    ocr_r = _make_ocr_response("| 01/03/2024 | 08:00 | 17:00 |")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[upload_r, ocr_r])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    expected_rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("services.mistral_service.normalize_text_with_gemini", AsyncMock(return_value=expected_rows)):
        rows = asyncio.run(extract_with_mistral(b"fake pdf"))

    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"


def test_raises_on_upload_failure():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_error_response(401))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(MistralExtractionError):
            asyncio.run(extract_with_mistral(b"fake"))


def test_raises_on_ocr_failure():
    upload_r = _make_upload_response()
    error_r = _make_error_response(500)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[upload_r, error_r])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(MistralExtractionError):
            asyncio.run(extract_with_mistral(b"fake"))


def test_raises_when_gemini_normalization_fails():
    from services.gemini_service import GeminiExtractionError

    upload_r = _make_upload_response()
    ocr_r = _make_ocr_response()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[upload_r, ocr_r])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client), \
         patch("services.mistral_service.normalize_text_with_gemini",
               AsyncMock(side_effect=GeminiExtractionError("fail"))):
        with pytest.raises(MistralExtractionError):
            asyncio.run(extract_with_mistral(b"fake"))
