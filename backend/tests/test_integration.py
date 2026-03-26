"""
Integration tests for CP5 — covers scenarios not in test_main.py:
- All occurrence types preserved through pipeline
- Mixed row (partial hours + occurrence)
- Rate limiting (429 after 10 req/min)
- Request logging (filename, size, provider, row count)
"""
from __future__ import annotations

import io
import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app, limiter
from models.timesheet import TimesheetRow

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
    b"xref\n0 2\ntrailer<</Size 2/Root 1 0 R>>\nstartxref\n9\n%%EOF"
)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset in-memory rate limit storage between tests to prevent cross-test pollution."""
    try:
        limiter._limiter.storage.reset()
    except AttributeError:
        pass
    yield


# ─── Occurrence types ────────────────────────────────────────────────────────

def test_all_occurrence_types_counted_in_rows():
    """All 5 main occurrence types must be reflected in X-Rows-Extracted."""
    rows = [
        TimesheetRow(data="01/03/2024", ocorrencia_raw="FERIAS", ocorrencia_tipo="ferias"),
        TimesheetRow(data="02/03/2024", ocorrencia_raw="DSR", ocorrencia_tipo="dsr"),
        TimesheetRow(data="03/03/2024", ocorrencia_raw="FERIADO", ocorrencia_tipo="feriado"),
        TimesheetRow(data="04/03/2024", ocorrencia_raw="FALTA", ocorrencia_tipo="falta_injustificada"),
        TimesheetRow(data="05/03/2024", ocorrencia_raw="LIC.MED.", ocorrencia_tipo="licenca_medica"),
    ]
    client = TestClient(app)
    with (
        patch("main.detect_pdf_type", return_value="scanned"),
        patch("main.extract_with_pdfplumber", return_value=None),
        patch("main.extract_with_gemini", AsyncMock(return_value=rows)),
        patch("main.build_excel", return_value=b"PKfake"),
    ):
        r = client.post(
            "/extract",
            files={"file": ("test.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
        )
    assert r.status_code == 200
    assert r.headers["x-rows-extracted"] == "5"
    assert r.headers["x-provider-used"] == "gemini"


# ─── Mixed row (partial hours + occurrence) ──────────────────────────────────

def test_mixed_row_partial_hours_and_occurrence():
    """A row with both entry/exit times AND an occurrence must be extracted."""
    mixed_rows = [
        TimesheetRow(
            data="01/03/2024",
            entrada_1="08:00",
            saida_1="12:00",
            ocorrencia_raw="ATESTADO",
            ocorrencia_tipo="licenca_medica",
        )
    ]
    client = TestClient(app)
    with (
        patch("main.detect_pdf_type", return_value="native"),
        patch("main.extract_with_pdfplumber", return_value=mixed_rows),
        patch("main.build_excel", return_value=b"PKfake"),
    ):
        r = client.post(
            "/extract",
            files={"file": ("test.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
        )
    assert r.status_code == 200
    assert r.headers["x-rows-extracted"] == "1"


# ─── Rate limiting ────────────────────────────────────────────────────────────

def test_rate_limit_triggers_429_on_extract():
    """The 11th /extract request within a minute must receive HTTP 429."""
    client = TestClient(app)
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]

    with (
        patch("main.detect_pdf_type", return_value="native"),
        patch("main.extract_with_pdfplumber", return_value=rows),
        patch("main.build_excel", return_value=b"PKfake"),
    ):
        responses = [
            client.post(
                "/extract",
                files={"file": ("test.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
            ).status_code
            for _ in range(11)
        ]

    assert all(s == 200 for s in responses[:10]), f"First 10 should be 200, got: {responses[:10]}"
    assert responses[10] == 429, f"11th request should be 429, got: {responses[10]}"


def test_rate_limit_triggers_429_on_preview():
    """The 11th /preview request within a minute must receive HTTP 429."""
    client = TestClient(app)
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]

    with (
        patch("main.detect_pdf_type", return_value="native"),
        patch("main.extract_with_pdfplumber", return_value=rows),
    ):
        responses = [
            client.post(
                "/preview",
                files={"file": ("test.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
            ).status_code
            for _ in range(11)
        ]

    assert responses[10] == 429, f"11th request should be 429, got: {responses[10]}"


# ─── Request logging ──────────────────────────────────────────────────────────

def test_extract_logs_request_start(caplog):
    """POST /extract must log filename and byte size at INFO level."""
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    client = TestClient(app)

    with (
        caplog.at_level(logging.INFO, logger="main"),
        patch("main.detect_pdf_type", return_value="native"),
        patch("main.extract_with_pdfplumber", return_value=rows),
        patch("main.build_excel", return_value=b"PKfake"),
    ):
        client.post(
            "/extract",
            files={"file": ("meu_ponto.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
        )

    assert "POST /extract" in caplog.text
    assert "meu_ponto.pdf" in caplog.text
    assert "size=" in caplog.text


def test_pipeline_logs_extraction_result(caplog):
    """_run_pipeline must log provider, row count, and pdf_type after extraction."""
    rows = [TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00")]
    client = TestClient(app)

    with (
        caplog.at_level(logging.INFO, logger="main"),
        patch("main.detect_pdf_type", return_value="native"),
        patch("main.extract_with_pdfplumber", return_value=rows),
        patch("main.build_excel", return_value=b"PKfake"),
    ):
        client.post(
            "/extract",
            files={"file": ("test.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
        )

    assert "provider=pdfplumber" in caplog.text
    assert "rows=1" in caplog.text
    assert "pdf_type=native" in caplog.text
