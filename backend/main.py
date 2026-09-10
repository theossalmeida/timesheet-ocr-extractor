import asyncio
import base64
import io
import json
import logging
from contextlib import asynccontextmanager

from access import AccessMiddleware
from accounts import router as accounts_router
from database import initialize, pool
from documents import router as documents_router, stored_stream, safe_filename, create_extraction, finish_extraction, fail_extraction, processing_lock
from security import team
from jobs import router as jobs_router, drain_jobs

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings
from models.timesheet import ExtractionResult
from services.csv_builder import build_csv
from services.excel_builder import build_excel
from services.contracheque_extra_hours_service import (
    stream_contracheque_extra_hours_extraction,
)
from services.contracheque_service import stream_contracheque_extraction
from services.frequency_cycle_service import (
    stream_frequency_cycle_extraction,
)
from services.gemini_service import (
    GeminiExtractionError,
    extract_with_gemini_adaptive,
    is_gemini_configured,
)
from services.guia_ministerial_service import stream_guia_extraction
from services.local_vision_ocr_service import (
    LocalVisionConnectionError,
    LocalVisionOCRError,
    extract_timesheet_rows_local_vision,
    is_local_vision_ocr_configured,
)
from services import ai_usage
from services.pdf_detector import detect_pdf_type
from services.pdfplumber_service import extract_with_pdfplumber, get_scanned_page_bytes
from services.tesseract_ocr_service import (
    TesseractOCRError,
    extract_timesheet_rows_tesseract,
    is_tesseract_available,
)
from utils.validators import validate_result, validate_row


logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=["10/minute"])

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(initialize)
    yield
    await drain_jobs()
    await asyncio.to_thread(pool.close)


app = FastAPI(
    lifespan=lifespan,
    title="AUTUS",
    description="Extrai registros de ponto de PDFs trabalhistas e gera Excel.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "DELETE", "PUT"],
    allow_headers=["*"],
    expose_headers=["X-Provider-Used", "X-Rows-Extracted", "X-PDF-Type"],
)

app.add_middleware(AccessMiddleware)
app.include_router(accounts_router)
app.include_router(documents_router)
app.include_router(jobs_router)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Erro interno do servidor. Tente novamente."},
    )


def _validate_pdf(file_bytes: bytes, size_bytes: int, max_mb: int | None = None) -> None:
    limit = (max_mb or settings.MAX_FILE_SIZE_MB) * 1024 * 1024
    if size_bytes > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo muito grande. Maximo permitido: {max_mb or settings.MAX_FILE_SIZE_MB}MB.",
        )
    if not file_bytes[:4] == b"%PDF":
        raise HTTPException(
            status_code=400,
            detail="Arquivo invalido. Apenas PDFs sao aceitos.",
        )


def _sort_key(date_str: str | None) -> tuple[int, int, int]:
    """Convert 'DD/MM/YYYY' to a (YYYY, MM, DD) tuple for sorting. Invalid dates sort last."""
    if not date_str:
        return (9999, 99, 99)
    try:
        d, m, y = date_str.split("/")
        return (int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return (9999, 99, 99)


def _run_tesseract_timesheet(pdf_bytes: bytes) -> list:
    """Run local Tesseract OCR extraction for the generic timesheet format,
    never raising - an empty list means "could not be read locally"
    (Tesseract not installed, unrenderable bytes, or no rows matched).
    """
    if not is_tesseract_available():
        logger.debug("Tesseract binary not found, skipping local OCR")
        return []
    try:
        rows = extract_timesheet_rows_tesseract(pdf_bytes)
        if rows:
            logger.info("Tesseract OCR extracted %d row(s) locally", len(rows))
        return rows
    except TesseractOCRError as e:
        logger.warning("Tesseract OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("Tesseract OCR raised unexpected error: %s", e)
        return []



async def _run_gemini_timesheet(scanned_pdf_bytes: bytes | None) -> list:
    """Run Gemini only for scanned/image-only PDF pages."""
    if not scanned_pdf_bytes:
        logger.debug("Gemini OCR skipped: no scanned/image-only pages")
        return []
    if not is_gemini_configured():
        logger.debug("Gemini OCR is not configured, skipping")
        return []
    try:
        rows = await extract_with_gemini_adaptive(scanned_pdf_bytes)
        if rows:
            logger.info("Gemini OCR extracted %d row(s)", len(rows))
        return rows
    except GeminiExtractionError as e:
        logger.warning("Gemini OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("Gemini OCR raised unexpected error: %s", e)
        return []

async def _run_local_vision_timesheet(pdf_bytes: bytes | None) -> list:
    """Run the optional local vision-model fallback.

    This is best-effort: unavailable Tailscale host, model mismatch, invalid
    JSON, or unreadable pages all collapse to [] so the caller can return the
    normal 422 when nothing is extracted.
    """
    if not pdf_bytes:
        logger.debug("Local vision OCR skipped: no scanned/image-only pages")
        return []
    if not is_local_vision_ocr_configured():
        logger.debug("Local vision OCR is not configured, skipping")
        return []
    try:
        rows = await extract_timesheet_rows_local_vision(pdf_bytes)
        if rows:
            logger.info("Local vision OCR extracted %d row(s)", len(rows))
        return rows
    except LocalVisionConnectionError as e:
        logger.warning("Local vision OCR connection failed: %s", e)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LocalVisionOCRError as e:
        logger.warning("Local vision OCR failed: %s", e)
        return []
    except Exception as e:
        logger.warning("Local vision OCR raised unexpected error: %s", e)
        return []

async def _run_pipeline(pdf_bytes: bytes) -> tuple[ExtractionResult, str, list[dict]]:
    """Run the extraction pipeline, metering the paid AI calls it makes.

    Returns (result, provider, ai_calls), where ai_calls is one entry per
    Gemini HTTP call - chunk calls, single-page retries and calls whose answer
    was discarded included, since Google bills all of them. The ledger is what
    the document cost is computed from (documents.finish_extraction).
    """
    with ai_usage.recording() as ai_calls:
        result, provider = await _extract_rows(pdf_bytes)
    return result, provider, ai_calls


async def _extract_rows(pdf_bytes: bytes) -> tuple[ExtractionResult, str]:
    """Run extraction pipeline: pdfplumber -> Tesseract -> Gemini/local vision OCR. Returns (result, provider).

    Tesseract runs locally over rendered page images for anything pdfplumber
    could not read. Gemini is used only for scanned/image-only PDF pages
    after local extraction fails.
    """
    pdf_type = await asyncio.to_thread(detect_pdf_type, pdf_bytes)
    logger.info("PDF type detected: %s, size: %d bytes", pdf_type, len(pdf_bytes))

    provider = "pdfplumber"

    # Always attempt local extraction first - detect_pdf_type is only a hint and
    # produces false negatives (e.g. reports whose summary pages fail the meaningful-text
    # heuristic get flagged "mixed"/"scanned" even though pdfplumber reads them fully).
    # OCR is the fallback, only reached when pdfplumber yields nothing for a page.
    rows = await asyncio.to_thread(extract_with_pdfplumber, pdf_bytes)

    if rows:
        # Check for scanned pages mixed into the same PDF (e.g. digital-signature wrappers
        # around image-only timesheets after the native-text section).
        scanned_bytes = await asyncio.to_thread(get_scanned_page_bytes, pdf_bytes)
        if scanned_bytes:
            pdf_type = "mixed"
            logger.info("Hybrid PDF: found scanned pages - running local Tesseract OCR")
            extra_rows = await asyncio.to_thread(_run_tesseract_timesheet, scanned_bytes)
            if extra_rows:
                provider = "pdfplumber+tesseract"
                rows = sorted(rows + extra_rows, key=lambda r: _sort_key(r.data))
                logger.info("Hybrid merge - total rows=%d", len(rows))
            else:
                extra_rows = await _run_gemini_timesheet(scanned_bytes)
                if extra_rows:
                    provider = "pdfplumber+gemini"
                    rows = sorted(rows + extra_rows, key=lambda r: _sort_key(r.data))
                    logger.info("Hybrid Gemini merge - total rows=%d", len(rows))
                else:
                    extra_rows = await _run_local_vision_timesheet(scanned_bytes)
                    if extra_rows:
                        provider = "pdfplumber+local-vision"
                        rows = sorted(rows + extra_rows, key=lambda r: _sort_key(r.data))
                        logger.info("Hybrid local-vision merge - total rows=%d", len(rows))

    if not rows:
        scanned_bytes = await asyncio.to_thread(get_scanned_page_bytes, pdf_bytes)
        tesseract_bytes = scanned_bytes or pdf_bytes
        rows = await asyncio.to_thread(_run_tesseract_timesheet, tesseract_bytes)
        if rows:
            provider = "tesseract"
        else:
            rows = await _run_gemini_timesheet(scanned_bytes)
            if rows:
                provider = "gemini"
            else:
                rows = await _run_local_vision_timesheet(scanned_bytes)
                if rows:
                    provider = "local-vision"

    if not rows:
        raise HTTPException(
            status_code=422,
            detail="Nenhum registro de ponto encontrado no PDF.",
        )

    row_warnings: list[str] = []
    for row in rows:
        row_warnings.extend(validate_row(row))
    result_warnings = validate_result(rows)

    logger.info(
        "Extraction complete - provider=%s rows=%d pdf_type=%s warnings=%d",
        provider,
        len(rows),
        pdf_type,
        len(row_warnings + result_warnings),
    )

    result = ExtractionResult(
        rows=rows,
        provider=provider,  # type: ignore[arg-type]
        pdf_type=pdf_type,
        warnings=row_warnings + result_warnings,
        total_rows=len(rows),
    )
    return result, provider



def _timesheet_bundle_content(result: ExtractionResult, provider: str, original_stem: str, ai_calls: list[dict] | None = None) -> dict:
    excel_bytes = build_excel(result)
    csv_content = build_csv(result)
    return {
        "excel_b64": base64.b64encode(excel_bytes).decode(),
        "excel_filename": f"timesheet_{original_stem}.xlsx",
        "csv_b64": base64.b64encode(csv_content.encode("utf-8-sig")).decode(),
        "csv_filename": f"pjecalc_{original_stem}.csv",
        "csv_mime": "text/csv",
        "rows_extracted": result.total_rows,
        "provider": provider,
        "pdf_type": result.pdf_type,
        # Consumed by documents.finish_extraction and stripped before the
        # payload reaches the client.
        "ai_usage": list(ai_calls or []),
    }


async def stream_timesheet_extraction(pdf_bytes: bytes, original_stem: str):
    yield "data: " + json.dumps({
        "type": "progress",
        "chunk": 1,
        "total": 5,
        "step": "received",
        "message": "Arquivo recebido. Analisando PDF...",
    }, ensure_ascii=False) + "\n\n"

    task = asyncio.create_task(_run_pipeline(pdf_bytes))
    yielded_extracting = False
    while not task.done():
        if not yielded_extracting:
            yielded_extracting = True
            yield "data: " + json.dumps({
                "type": "progress",
                "chunk": 2,
                "total": 5,
                "step": "extracting",
                "message": "Extraindo registros...",
            }, ensure_ascii=False) + "\n\n"

        await asyncio.wait({task}, timeout=10)
        if not task.done():
            yield ": keep-alive\n\n"

    try:
        result, provider, ai_calls = task.result()
    except HTTPException as e:
        yield "data: " + json.dumps({
            "type": "error",
            "message": str(e.detail),
            "status": e.status_code,
        }, ensure_ascii=False) + "\n\n"
        return
    except Exception as e:
        logger.exception("timesheet stream: unexpected error - %s", e)
        yield "data: " + json.dumps({
            "type": "error",
            "message": "Erro interno ao processar o PDF.",
            "status": 500,
        }, ensure_ascii=False) + "\n\n"
        return

    yield "data: " + json.dumps({
        "type": "progress",
        "chunk": 4,
        "total": 5,
        "step": "building",
        "message": "Gerando arquivos...",
    }, ensure_ascii=False) + "\n\n"

    content = await asyncio.to_thread(_timesheet_bundle_content, result, provider, original_stem, ai_calls)
    content["type"] = "done"
    yield "data: " + json.dumps(content, ensure_ascii=False) + "\n\n"

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.post("/extract")
@limiter.limit("10/minute")
async def extract(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /extract - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    extraction_id = await asyncio.to_thread(create_extraction, request, file.filename, "cartao", pdf_bytes)
    try:
        async with processing_lock:
            result, provider, ai_calls = await _run_pipeline(pdf_bytes)
    except BaseException:
        await asyncio.shield(asyncio.to_thread(fail_extraction, extraction_id))
        raise
    original_stem = (file.filename or "ponto").removesuffix(".pdf").removesuffix(".PDF")
    try:
        content = await asyncio.to_thread(_timesheet_bundle_content, result, provider, original_stem, ai_calls)
        await asyncio.to_thread(finish_extraction, extraction_id, content)
    except Exception:
        await asyncio.to_thread(fail_extraction, extraction_id)
        raise
    content.pop("ai_usage", None)

    return JSONResponse(
        content=content,
        headers={
            "X-Provider-Used": provider,
            "X-Rows-Extracted": str(result.total_rows),
            "X-PDF-Type": result.pdf_type,
        },
    )


@app.post("/extract/stream")
@limiter.limit("10/minute")
async def extract_stream(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /extract/stream - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    original_stem = (file.filename or "ponto").removesuffix(".pdf").removesuffix(".PDF")
    return StreamingResponse(
        stored_stream(request, pdf_bytes, file.filename, "cartao", lambda: stream_timesheet_extraction(pdf_bytes, original_stem)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/extract/guia")
@limiter.limit("10/minute")
async def extract_guia(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /extract/guia - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes), max_mb=200)

    original_stem = (file.filename or "guia").removesuffix(".pdf").removesuffix(".PDF")

    return StreamingResponse(
        stored_stream(request, pdf_bytes, file.filename, "guia", lambda: stream_guia_extraction(pdf_bytes, original_stem)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/extract/frequencia")
@limiter.limit("10/minute")
async def extract_frequencia(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /extract/frequencia - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes), max_mb=200)

    original_stem = (file.filename or "frequencia").removesuffix(".pdf").removesuffix(".PDF")

    return StreamingResponse(
        stored_stream(request, pdf_bytes, file.filename, "frequencia", lambda: stream_frequency_cycle_extraction(pdf_bytes, original_stem)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/contracheque")
@limiter.limit("10/minute")
async def extract_contracheque(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /contracheque - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes), max_mb=200)

    original_stem = (file.filename or "contracheque").removesuffix(".pdf").removesuffix(".PDF")

    return StreamingResponse(
        stored_stream(request, pdf_bytes, file.filename, "contracheque", lambda: stream_contracheque_extraction(pdf_bytes, original_stem)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/contracheque/horas-extras")
@limiter.limit("10/minute")
async def extract_contracheque_horas_extras(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /contracheque/horas-extras - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes), max_mb=200)

    original_stem = (file.filename or "contracheque").removesuffix(".pdf").removesuffix(".PDF")

    return StreamingResponse(
        stored_stream(request, pdf_bytes, file.filename, "horas_extras", lambda: stream_contracheque_extra_hours_extraction(pdf_bytes, original_stem)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/preview")
@limiter.limit("10/minute")
async def preview(request: Request, file: UploadFile = File(...)):
    await asyncio.to_thread(team, request)
    pdf_bytes = await file.read(200 * 1024 * 1024 + 1)
    file.filename = safe_filename(file.filename or "documento.pdf")
    logger.info(
        "POST /preview - filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    extraction_id = await asyncio.to_thread(create_extraction, request, file.filename, "preview", pdf_bytes)
    try:
        async with processing_lock:
            result, provider, ai_calls = await _run_pipeline(pdf_bytes)
        content = await asyncio.to_thread(_timesheet_bundle_content, result, provider, "preview", ai_calls)
        await asyncio.to_thread(finish_extraction, extraction_id, content)
        return result
    except BaseException:
        await asyncio.shield(asyncio.to_thread(fail_extraction, extraction_id))
        raise
