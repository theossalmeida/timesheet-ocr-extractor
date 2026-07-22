from __future__ import annotations
import asyncio
import io
import json
import logging
import re

import pypdf

logger = logging.getLogger(__name__)

from models.timesheet import TimesheetRow
from utils.normalizers import normalize_date, normalize_time

CHUNK_SIZE = 20  # pages per local-OCR request (keeps progress updates granular)

# Guia Ministerial / Papeleta de Serviço Externo documents use loose,
# inconsistent field labels for entrada/saída (e.g. "hora entrada", "hora
# início", "saída da garagem"). Without a vision-capable model to read those
# labels semantically, the local OCR path instead falls back to a simpler
# (and more limited) heuristic: for every date found on a line, take every
# HH:MM-shaped time on that same line and use the earliest as entrada and the
# latest as saída — mirroring the old "always earliest=entrada, latest=saida"
# aggregation rule, just without label awareness.
#
# IMPORTANT LIMITATION: many real Guia Ministerial forms are filled out by
# hand. Tesseract is a printed-text OCR engine and does not reliably read
# handwriting — this path works reasonably well for typed/printed guias, but
# will likely miss or misread handwritten ones. There is no local, free
# equivalent to a handwriting-capable vision model; this trade-off should be
# revisited if handwritten guias are common in practice.
_DATE_TOKEN_RE = re.compile(r"\b(\d{2})[/\-.](\d{2})[/\-.](\d{2,4})\b")
_TIME_TOKEN_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


class GuiaExtractionError(Exception):
    pass


def _split_pdf_chunks(pdf_bytes: bytes, chunk_size: int = CHUNK_SIZE) -> list[bytes]:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    chunks: list[bytes] = []
    for start in range(0, total, chunk_size):
        writer = pypdf.PdfWriter()
        for idx in range(start, min(start + chunk_size, total)):
            writer.add_page(reader.pages[idx])
        buf = io.BytesIO()
        writer.write(buf)
        chunks.append(buf.getvalue())
    return chunks


def _extract_records_from_text(text: str) -> list[dict]:
    """Find "DD/MM/YYYY ... HH:MM ... HH:MM" style lines in OCR'd text and
    turn each into a raw {"data", "entrada", "saida"} record (earliest time
    on the line = entrada, latest = saida).
    """
    records: list[dict] = []
    for line in text.splitlines():
        date_match = _DATE_TOKEN_RE.search(line)
        if not date_match:
            continue
        dd, mm, yy = date_match.groups()
        year = f"20{yy}" if len(yy) == 2 else yy
        data_str = f"{dd}/{mm}/{year}"

        times = [f"{h.zfill(2)}:{m}" for h, m in _TIME_TOKEN_RE.findall(line)]
        if not times:
            continue

        records.append({
            "data": data_str,
            "entrada": min(times),
            "saida": max(times),
        })
    return records


def _process_chunk_tesseract(chunk_bytes: bytes) -> list[dict]:
    """OCR a PDF chunk locally with Tesseract and extract raw records.
    Never raises — returns [] when Tesseract is unavailable, the bytes
    aren't a renderable PDF, or no page yields a match.
    """
    try:
        from services.tesseract_ocr_service import (
            TesseractOCRError,
            is_tesseract_available,
            ocr_pdf_page_texts,
        )
    except ImportError as e:
        logger.debug("guia: tesseract OCR dependencies not installed: %s", e)
        return []

    if not is_tesseract_available():
        logger.debug("guia: Tesseract binary not found, skipping local OCR")
        return []

    try:
        page_texts = ocr_pdf_page_texts(chunk_bytes)
    except TesseractOCRError as e:
        logger.warning("guia: Tesseract OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("guia: Tesseract OCR raised unexpected error: %s", e)
        return []

    records: list[dict] = []
    for page_index, text in page_texts:
        page_records = _extract_records_from_text(text)
        if page_records:
            logger.info(
                "guia: Tesseract OCR — page %d found %d record(s)",
                page_index, len(page_records),
            )
        records.extend(page_records)
    return records


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    try:
        d, m, y = date_str.split("/")
        return (int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return (9999, 99, 99)


def _aggregate(records: list[dict]) -> list[TimesheetRow]:
    """Group records by date. Keep earliest entrada, latest saída. Single worker assumed."""
    grouped: dict[str, dict[str, str | None]] = {}

    for rec in records:
        date_str = normalize_date(str(rec.get("data") or ""))
        if not date_str:
            continue

        entrada = normalize_time(str(rec.get("entrada") or ""))
        saida = normalize_time(str(rec.get("saida") or ""))

        if date_str not in grouped:
            grouped[date_str] = {"entrada": entrada, "saida": saida}
        else:
            existing = grouped[date_str]
            if entrada and (not existing["entrada"] or entrada < existing["entrada"]):
                existing["entrada"] = entrada
            if saida and (not existing["saida"] or saida > existing["saida"]):
                existing["saida"] = saida

    rows: list[TimesheetRow] = []
    for date_str, times in sorted(grouped.items(), key=lambda kv: _date_sort_key(kv[0])):
        rows.append(TimesheetRow(
            data=date_str,
            entrada_1=times["entrada"],
            saida_1=times["saida"],
        ))
    return rows


async def stream_guia_extraction(pdf_bytes: bytes, original_stem: str, chunk_size: int = CHUNK_SIZE):
    """Async generator yielding SSE strings for the guia ministerial extraction."""
    import json as _json
    import base64 as _b64
    from services.excel_builder import build_guia_excel
    from services.csv_builder import build_guia_csv

    try:
        chunks = _split_pdf_chunks(pdf_bytes, chunk_size)
        total = len(chunks)
        all_records: list[dict] = []

        for i, chunk in enumerate(chunks):
            yield f"data: {_json.dumps({'type': 'progress', 'chunk': i + 1, 'total': total, 'step': 'tesseract', 'message': f'OCR local (Tesseract): processando parte {i + 1} de {total}...'})}\n\n"

            task = asyncio.create_task(asyncio.to_thread(_process_chunk_tesseract, chunk))
            while not task.done():
                yield ": keep-alive\n\n"
                await asyncio.sleep(15)

            all_records.extend(task.result())

        rows = _aggregate(all_records)

        if not rows:
            yield f"data: {_json.dumps({'type': 'error', 'message': 'Nenhum registro encontrado nas guias ministeriais.'})}\n\n"
            return

        excel_bytes = build_guia_excel(rows)
        csv_bytes, csv_mime = build_guia_csv(rows)
        csv_ext = "zip" if csv_mime == "application/zip" else "csv"

        yield "data: " + _json.dumps({
            "type": "done",
            "excel_b64": _b64.b64encode(excel_bytes).decode(),
            "excel_filename": f"guia_{original_stem}.xlsx",
            "csv_b64": _b64.b64encode(csv_bytes).decode(),
            "csv_filename": f"pjecalc_{original_stem}.{csv_ext}",
            "csv_mime": csv_mime,
            "rows_extracted": len(rows),
            "provider": "tesseract-guia",
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("guia stream: unexpected error — %s", e)
        yield f"data: {_json.dumps({'type': 'error', 'message': 'Erro interno ao processar guias ministeriais.'})}\n\n"


async def extract_with_guia_ministerial(
    pdf_bytes: bytes, chunk_size: int = CHUNK_SIZE
) -> list[TimesheetRow]:
    logger.info("guia: starting extraction — pdf_size=%d bytes", len(pdf_bytes))
    chunks = _split_pdf_chunks(pdf_bytes, chunk_size=chunk_size)
    logger.info("guia: split into %d chunks of up to %d pages", len(chunks), chunk_size)

    all_records: list[dict] = []
    for i, chunk in enumerate(chunks):
        logger.info("guia: processing chunk %d/%d — %d bytes", i + 1, len(chunks), len(chunk))
        records = await asyncio.to_thread(_process_chunk_tesseract, chunk)
        logger.info("guia: chunk %d → %d records", i + 1, len(records))
        all_records.extend(records)

    rows = _aggregate(all_records)
    logger.info("guia: done — total_rows=%d", len(rows))
    return rows
