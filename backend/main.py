from __future__ import annotations
import io
import logging

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from config import settings
from models.timesheet import ExtractionResult
from services.excel_builder import build_excel
from services.gemini_service import GeminiExtractionError, extract_with_gemini
from services.mistral_service import MistralExtractionError, extract_with_mistral
from services.pdf_detector import detect_pdf_type
from services.pdfplumber_service import extract_with_pdfplumber
from utils.validators import validate_result, validate_row

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

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
)


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


def _validate_pdf(file_bytes: bytes, size_bytes: int) -> None:
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo muito grande. Máximo permitido: {settings.MAX_FILE_SIZE_MB}MB.",
        )
    if not file_bytes[:4] == b"%PDF":
        raise HTTPException(
            status_code=400,
            detail="Arquivo inválido. Apenas PDFs são aceitos.",
        )


async def _run_pipeline(pdf_bytes: bytes) -> tuple[ExtractionResult, str]:
    """Run extraction pipeline: pdfplumber → gemini → mistral. Returns (result, provider)."""
    pdf_type = detect_pdf_type(pdf_bytes)
    logger.info("PDF type detected: %s, size: %d bytes", pdf_type, len(pdf_bytes))

    rows = None
    provider = "pdfplumber"

    if pdf_type == "native":
        rows = extract_with_pdfplumber(pdf_bytes)

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

    logger.info("Extracted %d rows via %s", len(rows), provider)

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
async def extract(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    result, provider = await _run_pipeline(pdf_bytes)
    excel_bytes = build_excel(result)

    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="timesheet.xlsx"',
            "X-Provider-Used": provider,
            "X-Rows-Extracted": str(result.total_rows),
            "X-PDF-Type": result.pdf_type,
        },
    )


@app.post("/preview")
async def preview(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    _validate_pdf(pdf_bytes, len(pdf_bytes))

    result, _ = await _run_pipeline(pdf_bytes)
    return result
