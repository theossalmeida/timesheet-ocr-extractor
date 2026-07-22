from __future__ import annotations

from unittest.mock import patch

from services.tesseract_ocr_service import _normalize_ocr_text, extract_frequency_days_tesseract


def test_normalize_ocr_text_injects_clean_period_line_for_misread_accent():
    # "Perfodo" is a realistic Tesseract misread of "Período" when the
    # Portuguese language pack isn't installed — the accented "i" is lost.
    text = "Plano de Horario RCTO65-3 (Grupo Qua1) _-'Perfodo 01/06/2021 a 30/06/2021"

    normalized = _normalize_ocr_text(text)

    assert "Periodo 01/06/2021" in normalized
    # Original (garbled) line is preserved too, in case other parsers need it.
    assert "Perfodo 01/06/2021" in normalized


def test_normalize_ocr_text_leaves_unrelated_lines_untouched():
    text = "01/06 ter FOLG -1,00 +28,00 Turno de 12 Horas"

    assert _normalize_ocr_text(text) == text


def test_extract_frequency_days_tesseract_reuses_shared_parser():
    ocr_pages = [
        (1, "Periodo 01/06/2021\n01/06 ter FOLG -1,00 +28,00 Turno de 12 Horas"),
    ]

    with patch(
        "services.tesseract_ocr_service.ocr_pdf_page_texts",
        return_value=ocr_pages,
    ):
        rows = extract_frequency_days_tesseract(b"fake-pdf-bytes")

    assert len(rows) == 1
    assert rows[0].scale == "FOLG"
    assert rows[0].date.isoformat() == "2021-06-01"
