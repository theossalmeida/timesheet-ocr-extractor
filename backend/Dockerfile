FROM python:3.12-slim

# tesseract-ocr + tesseract-ocr-por: local OCR engine used by
# services/tesseract_ocr_service.py as the fallback for scanned pages and
# pages with an obfuscated/"encrypted" font encoding (see frequency_cycle_service.py,
# main.py, contracheque_service.py, contracheque_extra_hours_service.py,
# guia_ministerial_service.py). Without this package installed in the image,
# is_tesseract_available() returns False and every OCR fallback silently
# yields zero rows — this is a hard runtime requirement, not optional.
# libpoppler-cpp-dev / poppler-utils are kept for pdfplumber's own dependency
# chain; PyMuPDF (used for rendering pages before OCR) needs no system packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
