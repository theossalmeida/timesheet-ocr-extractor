import base64
import io
import logging

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
from services.guia_ministerial_service import stream_guia_extraction
from services.gemini_service import GeminiExtractionError, extract_with_gemini
from services.mistral_service import MistralExtractionError, extract_with_mistral
from services.pdf_detector import detect_pdf_type
from services.pdfplumber_service import extract_with_pdfplumber, get_scanned_page_bytes
from utils.validators import validate_result, validate_row

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=["10/minute"])

app = FastAPI(
    title="Timesheet Extractor",
    description="Extrai registros de ponto de PDFs trabalhistas e gera Excel.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
    expose_headers=["X-Provider-Used", "X-Rows-Extracted", "X-PDF-Type"],
)

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
            detail=f"Arquivo muito grande. Máximo permitido: {max_mb or settings.MAX_FILE_SIZE_MB}MB.",
        )
    if not file_bytes[:4] == b"%PDF":
        raise HTTPException(
            status_code=400,
            detail="Arquivo inválido. Apenas PDFs são aceitos.",
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


async def _run_pipeline(pdf_bytes: bytes) -> tuple[ExtractionResult, str]:
    """Run extraction pipeline: pdfplumber → gemini → mistral. Returns (result, provider)."""
    pdf_type = detect_pdf_type(pdf_bytes)
    logger.info("PDF type detected: %s, size: %d bytes", pdf_type, len(pdf_bytes))

    rows = None
    provider = "pdfplumber"

    if pdf_type == "native":
        rows = extract_with_pdfplumber(pdf_bytes)
        if rows:
            # Check for scanned pages mixed into the same PDF (e.g. digital-signature wrappers
            # around image-only timesheets after the native-text section).
            scanned_bytes = get_scanned_page_bytes(pdf_bytes)
            if scanned_bytes:
                pdf_type = "mixed"
                logger.info("Hybrid PDF: found scanned pages — running OCR")
                extra_rows: list | None = None
                try:
                    extra_rows = await extract_with_gemini(scanned_bytes)
                except GeminiExtractionError as e:
                    logger.warning("Gemini failed on scanned pages: %s — trying Mistral", e)
                if extra_rows is None:
                    try:
                        extra_rows = await extract_with_mistral(scanned_bytes)
                        provider = "pdfplumber+mistral"
                    except MistralExtractionError as e:
                        logger.warning("Mistral also failed on scanned pages: %s", e)
                elif extra_rows:
                    provider = "pdfplumber+gemini"
                if extra_rows:
                    rows = sorted(rows + extra_rows, key=lambda r: _sort_key(r.data))
                    logger.info("Hybrid merge — total rows=%d", len(rows))

    if rows is None:
        provider = "gemini"
        try:
            rows = await extract_with_gemini(pdf_bytes)
        except GeminiExtractionError as e:
            logger.warning("Gemini failed: %s — falling back to Mistral", e)
            rows = None

    if rows is None:
        provider = "mistral"
        try:
            rows = await extract_with_mistral(pdf_bytes)
        except MistralExtractionError as e:
            logger.error("Mistral failed: %s", e)
            raise HTTPException(
                status_code=422,
                detail="Não foi possível extrair registros de ponto deste PDF.",
            )

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
        "Extraction complete — provider=%s rows=%d pdf_type=%s warnings=%d",
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


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/extract")
@limiter.limit("10/minute")
async def extract(request: Request, file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    logger.info(
        "POST /extract — filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    result, provider = await _run_pipeline(pdf_bytes)
    excel_bytes = build_excel(result)
    csv_content = build_csv(result)

    original_stem = (file.filename or "ponto").removesuffix(".pdf").removesuffix(".PDF")

    return JSONResponse(
        content={
            "excel_b64": base64.b64encode(excel_bytes).decode(),
            "excel_filename": f"timesheet_{original_stem}.xlsx",
            "csv_b64": base64.b64encode(csv_content.encode("utf-8-sig")).decode(),
            "csv_filename": f"pjecalc_{original_stem}.csv",
            "csv_mime": "text/csv",
            "rows_extracted": result.total_rows,
            "provider": provider,
            "pdf_type": result.pdf_type,
        },
        headers={
            "X-Provider-Used": provider,
            "X-Rows-Extracted": str(result.total_rows),
            "X-PDF-Type": result.pdf_type,
        },
    )


@app.post("/extract/guia")
@limiter.limit("10/minute")
async def extract_guia(request: Request, file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    logger.info(
        "POST /extract/guia — filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes), max_mb=200)

    original_stem = (file.filename or "guia").removesuffix(".pdf").removesuffix(".PDF")

    return StreamingResponse(
        stream_guia_extraction(pdf_bytes, original_stem),
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
    pdf_bytes = await file.read()
    logger.info(
        "POST /preview — filename=%s size=%d bytes",
        file.filename or "unknown",
        len(pdf_bytes),
    )
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    result, _ = await _run_pipeline(pdf_bytes)
    return result
