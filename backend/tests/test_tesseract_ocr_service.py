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


def test_selected_page_ocr_renders_only_requested_pages_and_preserves_numbers():
    import fitz
    from services.tesseract_ocr_service import extract_frequency_day_texts_for_pages

    with fitz.open() as doc:
        for _ in range(4):
            doc.new_page(width=72, height=72)
        pdf_bytes = doc.tobytes()

    rendered_pages = []
    with (
        patch("services.tesseract_ocr_service.is_tesseract_available", return_value=True),
        patch("services.tesseract_ocr_service._pick_languages", return_value="eng"),
        patch("pytesseract.image_to_string", side_effect=["second", "fourth"]) as ocr,
        patch("fitz.Page.get_pixmap", autospec=True) as render,
    ):
        from unittest.mock import MagicMock
        pixmap = MagicMock(width=1, height=1, samples=b"\xff\xff\xff")
        render.side_effect = lambda page, **kwargs: rendered_pages.append(page.number) or pixmap
        result = extract_frequency_day_texts_for_pages(pdf_bytes, [4, 2, 2, 0, 8])

    assert result == [(2, "second"), (4, "fourth")]
    assert ocr.call_count == 2
    assert rendered_pages == [1, 3]


def test_ocr_releases_each_image_before_rendering_next_page():
    from unittest.mock import MagicMock
    from services.tesseract_ocr_service import ocr_pdf_page_texts

    first = MagicMock()
    second = MagicMock()

    def render_pages(*args, **kwargs):
        yield 1, first
        first.close.assert_called_once()
        yield 2, second

    with (
        patch("services.tesseract_ocr_service.is_tesseract_available", return_value=True),
        patch("services.tesseract_ocr_service._pick_languages", return_value="eng"),
        patch("services.tesseract_ocr_service._iter_pdf_page_images", side_effect=render_pages),
        patch("pytesseract.image_to_string", side_effect=["first", "second"]),
    ):
        assert ocr_pdf_page_texts(b"fake") == [(1, "first"), (2, "second")]

    second.close.assert_called_once()


def test_ocr_continues_after_page_error_and_closes_images():
    import pytesseract
    from unittest.mock import MagicMock
    from services.tesseract_ocr_service import ocr_pdf_page_texts

    first = MagicMock()
    second = MagicMock()

    def render_pages(*args, **kwargs):
        yield 1, first
        yield 2, second

    with (
        patch("services.tesseract_ocr_service.is_tesseract_available", return_value=True),
        patch("services.tesseract_ocr_service._pick_languages", return_value="eng"),
        patch("services.tesseract_ocr_service._iter_pdf_page_images", side_effect=render_pages),
        patch("pytesseract.image_to_string", side_effect=[pytesseract.TesseractError(1, "failed"), "second"]),
    ):
        assert ocr_pdf_page_texts(b"fake") == [(1, ""), (2, "second")]

    first.close.assert_called_once()
    second.close.assert_called_once()
