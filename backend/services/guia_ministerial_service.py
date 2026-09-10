from __future__ import annotations
import asyncio
import io
import json
import logging

import pypdf

logger = logging.getLogger(__name__)

from models.timesheet import TimesheetRow
from services import ai_usage
from utils.normalizers import normalize_date, normalize_time

CHUNK_SIZE = 20  # pages per Gemini request (keeps progress updates granular)

# Guia Ministerial / Papeleta de Servico Externo forms are filled in by hand.
# The only machine-readable text on a scanned guia is usually the electronic
# signature footer and the page number stamped by the court system, so text
# extraction does not read the form - it reads the footer, and reports the
# signing date and time as the work date and punches. This pipeline therefore
# never scrapes text for guias: a vision model reads the form, or nothing does.
#
# A local model reads a short probe first. Only if it grades that probe highly
# does the rest of the document stay local; otherwise the whole document goes
# to Gemini, and single pages the local model cannot read go to Gemini too.


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


def _single_page_pdf(pdf_bytes: bytes, page_number: int) -> bytes:
    """One page as its own PDF, so a page the local model fails can be retried."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    writer = pypdf.PdfWriter()
    writer.add_page(reader.pages[page_number - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _run_local(pdf_bytes: bytes):
    """Read the document locally. Returns None when local reading is unusable."""
    try:
        from services.local_vision_ocr_service import (
            LocalVisionOCRError,
            run_guia_local,
        )
    except ImportError as e:
        logger.debug("guia: local vision OCR dependencies not installed: %s", e)
        return None

    try:
        return await run_guia_local(pdf_bytes)
    except LocalVisionOCRError as e:
        logger.warning("guia: local vision OCR failed: %s", e)
        return None
    except Exception as e:
        logger.warning("guia: local vision OCR raised unexpected error: %s", e)
        return None


def _record_from_row(row: TimesheetRow) -> dict:
    """A guia page is one service form: earliest punch in, latest punch out."""
    return {
        "data": row.data,
        "entrada": row.marcacoes[0] if row.marcacoes else None,
        "saida": row.marcacoes[-1] if len(row.marcacoes) > 1 else None,
        "_confidence": row.ocr_confidence,
        "_ocr_warning": row.ocr_warning,
    }


async def _process_chunk_gemini(chunk_bytes: bytes) -> list[dict]:
    """Paid vision fallback for pages Tesseract cannot read.

    Many guias are filled out by hand and Tesseract is a printed-text engine,
    so this is usually the only stage that can read them at all. Its extraction
    prompt already covers single-service forms (GUIA MINISTERIAL, PAPELETA DE
    SERVICOS), and every call it makes is metered by services/ai_usage.py.
    """
    from services.gemini_service import (
        GeminiExtractionError,
        extract_with_gemini_adaptive,
        is_gemini_configured,
    )

    if not is_gemini_configured():
        logger.debug("guia: Gemini is not configured, skipping")
        return []

    try:
        rows = await extract_with_gemini_adaptive(chunk_bytes)
    except GeminiExtractionError as e:
        logger.warning("guia: Gemini OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("guia: Gemini OCR raised unexpected error: %s", e)
        return []

    if rows:
        logger.info("guia: Gemini OCR read %d record(s)", len(rows))
    return [_record_from_row(row) for row in rows]


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
            grouped[date_str] = {
                "entrada": entrada,
                "saida": saida,
                "ocr_confidence": rec.get("_confidence"),
                "ocr_warning": rec.get("_ocr_warning"),
            }
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
            marcacoes=[t for t in (times["entrada"], times["saida"]) if t],
            ocr_confidence=times.get("ocr_confidence"),
            ocr_warning=times.get("ocr_warning"),
        ))
    return rows


async def _gemini_whole_document(pdf_bytes: bytes, chunk_size: int) -> list[dict]:
    records: list[dict] = []
    for chunk in _split_pdf_chunks(pdf_bytes, chunk_size):
        records.extend(await _process_chunk_gemini(chunk))
    return records


async def stream_guia_extraction(pdf_bytes: bytes, original_stem: str, chunk_size: int = CHUNK_SIZE):
    """Async generator yielding SSE strings for the guia ministerial extraction."""
    import json as _json
    import base64 as _b64
    from services.excel_builder import build_guia_excel
    from services.csv_builder import build_guia_csv

    def progress(step: str, message: str) -> str:
        return f"data: {_json.dumps({'type': 'progress', 'step': step, 'message': message})}\n\n"

    async def drain(coro):
        """Run `coro`, holding the SSE connection open while it works.

        Waits on the task rather than polling it, so a step that finishes
        quickly costs nothing and only a genuinely slow one emits keep-alives.
        """
        task = asyncio.create_task(coro)
        while True:
            try:
                yield await asyncio.wait_for(asyncio.shield(task), timeout=15)
                return
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"

    try:
        with ai_usage.recording() as ai_calls:
            all_records: list[dict] = []
            used: list[str] = []

            yield progress("local-vision", "IA local: lendo as primeiras paginas da guia...")
            run = None
            async for item in drain(_run_local(pdf_bytes)):
                if isinstance(item, str):
                    yield item
                else:
                    run = item

            if run is not None and run.passed:
                used.append("local-vision-guia")
                logger.info(
                    "guia: local probe passed at %.0f%%; %d page(s) read locally",
                    run.confidence * 100, len(run.outcomes),
                )
                # Pages the local model could not read are retried individually
                # rather than sending the whole document back to a paid model.
                for outcome in run.outcomes:
                    if outcome.record:
                        all_records.append(outcome.record)
                        continue
                    yield progress(
                        "gemini",
                        f"IA (Gemini): relendo a pagina {outcome.page_number}...",
                    )
                    page_pdf = _single_page_pdf(pdf_bytes, outcome.page_number)
                    async for item in drain(_process_chunk_gemini(page_pdf)):
                        if isinstance(item, str):
                            yield item
                        else:
                            if item and "gemini-guia" not in used:
                                used.append("gemini-guia")
                            all_records.extend(item)
            else:
                reason = run.reason if run is not None else "IA local indisponivel"
                logger.info("guia: local reading rejected (%s); using Gemini", reason)
                chunks = _split_pdf_chunks(pdf_bytes, chunk_size)
                for i, chunk in enumerate(chunks):
                    yield progress(
                        "gemini",
                        f"IA (Gemini): processando parte {i + 1} de {len(chunks)}...",
                    )
                    async for item in drain(_process_chunk_gemini(chunk)):
                        if isinstance(item, str):
                            yield item
                        else:
                            if item and "gemini-guia" not in used:
                                used.append("gemini-guia")
                            all_records.extend(item)

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
                "provider": "+".join(used) or "gemini-guia",
                "ai_usage": list(ai_calls),
            }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("guia stream: unexpected error - %s", e)
        yield f"data: {_json.dumps({'type': 'error', 'message': 'Erro interno ao processar guias ministeriais.'})}\n\n"


async def extract_with_guia_ministerial(
    pdf_bytes: bytes, chunk_size: int = CHUNK_SIZE
) -> list[TimesheetRow]:
    logger.info("guia: starting extraction - pdf_size=%d bytes", len(pdf_bytes))

    run = await _run_local(pdf_bytes)
    if run is not None and run.passed:
        logger.info(
            "guia: local probe passed at %.0f%%; %d page(s) read locally",
            run.confidence * 100, len(run.outcomes),
        )
        records: list[dict] = []
        for outcome in run.outcomes:
            if outcome.record:
                records.append(outcome.record)
                continue
            logger.info(
                "guia: page %d unreadable locally; retrying it with Gemini",
                outcome.page_number,
            )
            records.extend(
                await _process_chunk_gemini(_single_page_pdf(pdf_bytes, outcome.page_number))
            )
    else:
        reason = run.reason if run is not None else "local vision OCR unavailable"
        logger.info("guia: local reading rejected (%s); using Gemini", reason)
        records = await _gemini_whole_document(pdf_bytes, chunk_size)

    rows = _aggregate(records)
    logger.info("guia: done - total_rows=%d", len(rows))
    return rows
