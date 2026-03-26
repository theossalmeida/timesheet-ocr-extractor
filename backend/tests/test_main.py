import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from models.timesheet import ExtractionResult, TimesheetRow

client = TestClient(app)

SAMPLE_ROWS = [
    TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00"),
    TimesheetRow(data="04/03/2024", ocorrencia_raw="FERIAS", ocorrencia_tipo="ferias"),
]

SAMPLE_RESULT = ExtractionResult(
    rows=SAMPLE_ROWS,
    provider="pdfplumber",
    pdf_type="native",
    warnings=[],
    total_rows=2,
)

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\n0 2\ntrailer<</Size 2/Root 1 0 R>>\nstartxref\n9\n%%EOF"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_extract_invalid_magic_bytes():
    data = io.BytesIO(b"not a pdf content here")
    r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})
    assert r.status_code == 400
    assert "inválido" in r.json()["error"].lower()


def test_extract_file_too_large():
    # Create a fake PDF header but oversized
    large_pdf = b"%PDF" + b"x" * (51 * 1024 * 1024)
    data = io.BytesIO(large_pdf)
    r = client.post("/extract", files={"file": ("big.pdf", data, "application/pdf")})
    assert r.status_code == 413


def test_extract_success_returns_xlsx():
    with patch("main.detect_pdf_type", return_value="native"), \
         patch("main.extract_with_pdfplumber", return_value=SAMPLE_ROWS), \
         patch("main.build_excel", return_value=b"PKfake_excel_bytes"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert r.headers["x-provider-used"] == "pdfplumber"
    assert r.headers["x-rows-extracted"] == "2"
    assert r.headers["x-pdf-type"] == "native"


def test_extract_fallback_to_gemini():
    gemini_rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    with patch("main.detect_pdf_type", return_value="scanned"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.extract_with_gemini", AsyncMock(return_value=gemini_rows)), \
         patch("main.build_excel", return_value=b"PKfake"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    assert r.headers["x-provider-used"] == "gemini"


def test_extract_fallback_to_mistral():
    from services.gemini_service import GeminiExtractionError
    mistral_rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    with patch("main.detect_pdf_type", return_value="scanned"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.extract_with_gemini", AsyncMock(side_effect=GeminiExtractionError("fail"))), \
         patch("main.extract_with_mistral", AsyncMock(return_value=mistral_rows)), \
         patch("main.build_excel", return_value=b"PKfake"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    assert r.headers["x-provider-used"] == "mistral"


def test_extract_all_providers_fail_returns_422():
    from services.gemini_service import GeminiExtractionError
    from services.mistral_service import MistralExtractionError
    with patch("main.detect_pdf_type", return_value="scanned"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.extract_with_gemini", AsyncMock(side_effect=GeminiExtractionError("fail"))), \
         patch("main.extract_with_mistral", AsyncMock(side_effect=MistralExtractionError("fail"))):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 422


def test_preview_returns_json():
    with patch("main.detect_pdf_type", return_value="native"), \
         patch("main.extract_with_pdfplumber", return_value=SAMPLE_ROWS):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/preview", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert body["provider"] == "pdfplumber"
    assert len(body["rows"]) == 2


def test_preview_invalid_pdf():
    data = io.BytesIO(b"garbage data")
    r = client.post("/preview", files={"file": ("test.pdf", data, "application/pdf")})
    assert r.status_code == 400
