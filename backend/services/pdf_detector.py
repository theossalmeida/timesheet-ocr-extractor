from __future__ import annotations
import io
from typing import Literal

import pdfplumber


def detect_pdf_type(pdf_bytes: bytes) -> Literal["native", "scanned", "mixed"]:
    pdf = None
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        total_pages = len(pdf.pages)
        pages_to_check = min(5, total_pages)
        text_pages = 0
        for i in range(pages_to_check):
            text = pdf.pages[i].extract_text() or ""
            if len(text) > 50:
                text_pages += 1
        ratio = text_pages / pages_to_check if pages_to_check > 0 else 0.0
        if ratio >= 0.8:
            return "native"
        elif ratio <= 0.2:
            return "scanned"
        else:
            return "mixed"
    finally:
        if pdf is not None:
            pdf.close()
