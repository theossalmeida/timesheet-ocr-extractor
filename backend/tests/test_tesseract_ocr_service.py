from __future__ import annotations

from unittest.mock import patch

from services.tesseract_ocr_service import (
    _normalize_ocr_text,
    extract_frequency_days_tesseract,
    extract_timesheet_rows_tesseract,
)


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


def test_extract_timesheet_rows_tesseract_aggregates_peg_larg_lines_by_day():
    ocr_pages = [(1, """\
Funcionario Funcao Data Dia Linha Carro Viag Lcto Peg Larg Prest Cont Trab
02/03/2026 Seg 114417 110285 2.0 X 04:40 09:35 00:00 04:58
02/03/2026 Seg 114417 110285 2.0 X 11:40 14:32 00:22 02:52
18/03/2026 Qua 110417 110039 3.0 X 04:55 08:25 00:00 03:31
18/03/2026 Qua 110417 110374 3.0 X 08:25 11:02 00:00 02:37
18/03/2026 Qua 110417 110374 3.0 X 15:30 17:25 00:17 01:55
""")]

    with patch(
        "services.tesseract_ocr_service.ocr_pdf_page_texts",
        return_value=ocr_pages,
    ):
        rows = extract_timesheet_rows_tesseract(b"fake-pdf-bytes")

    assert [(row.data, row.marcacoes) for row in rows] == [
        ("02/03/2026", ["04:40", "09:35", "11:40", "14:32"]),
        ("18/03/2026", ["04:55", "08:25", "08:25", "11:02", "15:30", "17:25"]),
    ]


def test_extract_timesheet_rows_tesseract_handles_days_off_and_ocr_glitches():
    ocr_pages = [(1, """\
Data Dia Linha Carro Viag Lcto Peg Larq Prest Cont Trab
14/03/2026 Sab FC 00:00 00:00 00:00 00:00
15/03/2026 Dom FO 00:00 00:00 00:00 00:00
46/03/2026 Seg 114417 110285 2.0 X 04:25 10:20 00:00 06:00
16/03/2026 Seg 114417 110285 2.0 X 1415 16:05 00:24 01:50
""")]

    with patch(
        "services.tesseract_ocr_service.ocr_pdf_page_texts",
        return_value=ocr_pages,
    ):
        rows = extract_timesheet_rows_tesseract(b"fake-pdf-bytes")

    assert rows[0].data == "14/03/2026"
    assert rows[0].marcacoes == []
    assert rows[0].ocorrencia_tipo == "folga"
    assert rows[1].ocorrencia_tipo == "folga"
    assert rows[2].data == "16/03/2026"
    assert rows[2].marcacoes == ["04:25", "10:20", "14:15", "16:05"]
