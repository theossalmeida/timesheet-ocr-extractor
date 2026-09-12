from __future__ import annotations
import asyncio
import io
import logging

import pypdf

logger = logging.getLogger(__name__)

from models.timesheet import TimesheetRow
from services import ai_usage, progress
from utils.normalizers import normalize_date, normalize_time

# A guia is one self-contained service form per page, so each request carries a
# single page: nothing is gained by showing the model two unrelated forms at
# once, and doing so lets it carry a field from one page onto the other.
#
# That also makes the page the unit of work, so progress counts pages - the
# thing the person watching actually recognises - and the bar moves one page at
# a time instead of jumping a whole block.
GEMINI_PAGES_PER_REQUEST = 1
CHUNK_SIZE = GEMINI_PAGES_PER_REQUEST

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


def _gemini_semaphore() -> asyncio.Semaphore:
    """One Gemini concurrency budget, shared by every chunk of a document.

    Chunks run concurrently, and each one fans out internally, so without a
    shared budget the in-flight request count would be the product of the two.
    """
    from services.gemini_service import _max_concurrent_chunks

    return asyncio.Semaphore(_max_concurrent_chunks())


async def _process_chunk_gemini(
    chunk_bytes: bytes, semaphore: asyncio.Semaphore | None = None
) -> list[dict]:
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
        rows = await extract_with_gemini_adaptive(
            chunk_bytes, chunk_size=GEMINI_PAGES_PER_REQUEST, semaphore=semaphore
        )
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


async def _gemini_page_retry(
    pdf_bytes: bytes, page_number: int, semaphore: asyncio.Semaphore
) -> list[dict]:
    """Re-read one page the local model could not, splitting it off-thread."""
    page_pdf = await asyncio.to_thread(_single_page_pdf, pdf_bytes, page_number)
    return await _process_chunk_gemini(page_pdf, semaphore)


async def _gemini_whole_document(pdf_bytes: bytes, chunk_size: int) -> list[dict]:
    semaphore = _gemini_semaphore()
    chunks = await asyncio.to_thread(_split_pdf_chunks, pdf_bytes, chunk_size)
    results = await asyncio.gather(
        *(_process_chunk_gemini(chunk, semaphore) for chunk in chunks)
    )
    return [record for chunk_records in results for record in chunk_records]


async def stream_guia_extraction(pdf_bytes: bytes, original_stem: str, chunk_size: int = CHUNK_SIZE):
    """Async generator yielding SSE strings for the guia ministerial extraction."""
    import json as _json
    import base64 as _b64
    from services.excel_builder import build_guia_excel
    from services.csv_builder import build_guia_csv

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
                yield progress.keep_alive()

    async def drain_parallel(coros, step):
        """Run `coros` concurrently, reporting each one as it finishes.

        Yields SSE strings, then the concatenated records. Progress counts
        completions rather than positions, because results arrive out of order.
        """
        tasks = [asyncio.create_task(c) for c in coros]
        pending = set(tasks)
        total = len(tasks)
        finished = 0
        try:
            yield progress.processing(finished, total, step=step)
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=15, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    yield progress.keep_alive()
                    continue
                finished += len(done)
                yield progress.processing(finished, total, step=step)
            yield [record for task in tasks for record in task.result()]
        finally:
            for task in tasks:
                task.cancel()

    try:
        with ai_usage.recording() as ai_calls:
            all_records: list[dict] = []
            used: list[str] = []

            yield progress.detecting(step="local-vision")
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
                retry_pages = []
                for outcome in run.outcomes:
                    if outcome.record:
                        all_records.append(outcome.record)
                    else:
                        retry_pages.append(outcome.page_number)

                if retry_pages:
                    semaphore = _gemini_semaphore()
                    async for item in drain_parallel(
                        [_gemini_page_retry(pdf_bytes, page, semaphore) for page in retry_pages],
                        "gemini",
                    ):
                        if isinstance(item, str):
                            yield item
                        else:
                            if item and "gemini-guia" not in used:
                                used.append("gemini-guia")
                            all_records.extend(item)
            else:
                reason = run.reason if run is not None else "IA local indisponivel"
                logger.info("guia: local reading rejected (%s); using Gemini", reason)
                semaphore = _gemini_semaphore()
                chunks = await asyncio.to_thread(_split_pdf_chunks, pdf_bytes, chunk_size)
                async for item in drain_parallel(
                    [_process_chunk_gemini(chunk, semaphore) for chunk in chunks],
                    "gemini",
                ):
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

            yield progress.building()

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
        retry_pages = []
        for outcome in run.outcomes:
            if outcome.record:
                records.append(outcome.record)
            else:
                retry_pages.append(outcome.page_number)

        if retry_pages:
            logger.info(
                "guia: %d page(s) unreadable locally; retrying them with Gemini - pages %s",
                len(retry_pages), retry_pages,
            )
            semaphore = _gemini_semaphore()
            retried = await asyncio.gather(
                *(_gemini_page_retry(pdf_bytes, page, semaphore) for page in retry_pages)
            )
            records.extend(record for page_records in retried for record in page_records)
    else:
        reason = run.reason if run is not None else "local vision OCR unavailable"
        logger.info("guia: local reading rejected (%s); using Gemini", reason)
        records = await _gemini_whole_document(pdf_bytes, chunk_size)

    rows = _aggregate(records)
    logger.info("guia: done - total_rows=%d", len(rows))
    return rows
