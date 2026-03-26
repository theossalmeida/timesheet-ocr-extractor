from unittest.mock import MagicMock, patch
from services.pdf_detector import detect_pdf_type


def _make_page(text: str):
    page = MagicMock()
    page.extract_text.return_value = text
    return page


def _mock_pdf(pages_text: list[str]):
    pdf = MagicMock()
    pdf.__enter__ = MagicMock(return_value=pdf)
    pdf.__exit__ = MagicMock(return_value=False)
    pdf.pages = [_make_page(t) for t in pages_text]
    return pdf


def test_native_pdf():
    long_text = "DATA ENTRADA SAIDA OCORRENCIA\n" * 5
    pdf = _mock_pdf([long_text] * 3)
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "native"


def test_scanned_pdf():
    pdf = _mock_pdf([""] * 3)
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "scanned"


def test_mixed_pdf():
    long_text = "DATA ENTRADA SAIDA OCORRENCIA\n" * 5
    # 3 text pages out of 5 = 60% → mixed
    pdf = _mock_pdf([long_text, long_text, long_text, "", ""])
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "mixed"


def test_all_pages_with_text():
    long_text = "a" * 100
    pdf = _mock_pdf([long_text] * 5)
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "native"


def test_pdf_close_called():
    pdf = _mock_pdf([""] * 2)
    with patch("pdfplumber.open", return_value=pdf):
        detect_pdf_type(b"fake")
    pdf.close.assert_called_once()


def test_pdf_close_called_on_exception():
    pdf = MagicMock()
    pdf.pages = [MagicMock()]
    pdf.pages[0].extract_text.side_effect = RuntimeError("boom")
    with patch("pdfplumber.open", return_value=pdf):
        try:
            detect_pdf_type(b"fake")
        except RuntimeError:
            pass
    pdf.close.assert_called_once()


def test_short_text_counts_as_no_text():
    # Text under 50 chars should count as no text
    pdf = _mock_pdf(["short"] * 3)
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "scanned"


def test_single_page_native():
    pdf = _mock_pdf(["a" * 100])
    with patch("pdfplumber.open", return_value=pdf):
        assert detect_pdf_type(b"fake") == "native"
