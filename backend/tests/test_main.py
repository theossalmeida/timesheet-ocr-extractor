import io
import json
import pytest
from unittest.mock import MagicMock, patch
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


def test_extract_success_returns_bundle():
    with patch("main.detect_pdf_type", return_value="native"), \
         patch("main.extract_with_pdfplumber", return_value=SAMPLE_ROWS), \
         patch("main.build_excel", return_value=b"PKfake_excel_bytes"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    body = r.json()
    assert "excel_b64" in body
    assert "csv_b64" in body
    assert body["rows_extracted"] == 2
    assert body["provider"] == "pdfplumber"
    assert body["pdf_type"] == "native"
    assert r.headers["x-provider-used"] == "pdfplumber"
    assert r.headers["x-rows-extracted"] == "2"
    assert r.headers["x-pdf-type"] == "native"


def test_extract_uses_pdfplumber_even_when_type_not_native():
    """detect_pdf_type is only a hint; pdfplumber must run for every type and
    Tesseract must never be invoked when pdfplumber already extracted rows."""
    tesseract_mock = MagicMock(return_value=[])
    for detected_type in ("mixed", "scanned"):
        with patch("main.detect_pdf_type", return_value=detected_type), \
             patch("main.extract_with_pdfplumber", return_value=SAMPLE_ROWS), \
             patch("main.get_scanned_page_bytes", return_value=None), \
             patch("main._run_tesseract_timesheet", tesseract_mock), \
             patch("main.build_excel", return_value=b"PKfake"):
            data = io.BytesIO(MINIMAL_PDF)
            r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

        assert r.status_code == 200
        assert r.headers["x-provider-used"] == "pdfplumber"
        assert r.json()["rows_extracted"] == 2
    tesseract_mock.assert_not_called()


def test_extract_fallback_to_tesseract():
    tesseract_rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    with patch("main.detect_pdf_type", return_value="scanned"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.get_scanned_page_bytes", return_value=None), \
         patch("main._run_tesseract_timesheet", return_value=tesseract_rows), \
         patch("main.build_excel", return_value=b"PKfake"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    assert r.headers["x-provider-used"] == "tesseract"


def test_extract_fallback_sends_scanned_pages_to_tesseract():
    tesseract_rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    tesseract_mock = MagicMock(return_value=tesseract_rows)
    with patch("main.detect_pdf_type", return_value="native"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.get_scanned_page_bytes", return_value=b"scanned-pages"), \
         patch("main._run_tesseract_timesheet", tesseract_mock), \
         patch("main.build_excel", return_value=b"PKfake"):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post("/extract", files={"file": ("test.pdf", data, "application/pdf")})

    assert r.status_code == 200
    tesseract_mock.assert_called_once_with(b"scanned-pages")


def test_extract_tesseract_fail_returns_422():
    with patch("main.detect_pdf_type", return_value="scanned"), \
         patch("main.extract_with_pdfplumber", return_value=None), \
         patch("main.get_scanned_page_bytes", return_value=None), \
         patch("main._run_tesseract_timesheet", return_value=[]):
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


def test_contracheque_extra_hours_stream_route():
    async def fake_stream(pdf_bytes: bytes, original_stem: str):
        yield 'data: {"type":"done","months_extracted":1,"columns_extracted":1}\n\n'

    with patch("main.stream_contracheque_extra_hours_extraction", fake_stream):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post(
            "/contracheque/horas-extras",
            files={"file": ("contracheque.pdf", data, "application/pdf")},
        )

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert '"type":"done"' in r.text


def test_extract_frequencia_success_returns_excel_bundle():
    async def fake_stream(pdf_bytes: bytes, original_stem: str):
        yield 'data: {"type":"done","excel_b64":"UEtmYWtl","excel_filename":"frequencia_frequencia.xlsx","rows_extracted":1,"provider":"pdfplumber"}\n\n'

    with patch("main.stream_frequency_cycle_extraction", fake_stream):
        data = io.BytesIO(MINIMAL_PDF)
        r = client.post(
            "/extract/frequencia",
            files={"file": ("frequencia.pdf", data, "application/pdf")},
        )

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert '"excel_filename":"frequencia_frequencia.xlsx"' in r.text
    assert '"rows_extracted":1' in r.text
    assert '"provider":"pdfplumber"' in r.text
