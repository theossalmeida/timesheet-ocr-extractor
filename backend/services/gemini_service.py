from __future__ import annotations
from io import BytesIO
import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx
import pypdf

logger = logging.getLogger(__name__)

from config import settings
from models.timesheet import TimesheetRow
from services import ai_usage
from utils.normalizers import normalize_date, normalize_time, normalize_ocorrencia

GEMINI_PAGE_CHUNK_SIZE = 2
GEMINI_TIMEOUT_SECONDS = 180.0
GEMINI_RETRIES = 2


def is_gemini_configured() -> bool:
    return bool((settings.GEMINI_API_KEY or "").strip())


def _gemini_model() -> str:
    return (settings.GEMINI_MODEL or "gemini-3.1-pro-preview").strip()


def _gemini_url() -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{_gemini_model()}:generateContent"


EXTRACTION_PROMPT = """You are a timesheet data extractor for Brazilian labor documents.
Read only the supplied PDF pages/images and return strict JSON.

Return this exact JSON object shape:
{"rows":[{"data":"DD/MM/YYYY","marcacoes":["HH:MM","HH:MM"],"ocorrencia":null,"confidence":"high|medium|low"}]}

Rules:
- Extract only values visibly anchored to the work-time table or service form fields on the page.
- For a normal cartao de ponto table, output one row per visible work date. Keep the punch times visible on that same row/date, in reading order.
- For a single-service form such as PAPELETA DE SERVICOS, GUIA MINISTERIAL, ordem de servico, viagem/linha service sheet, or similar, output at most ONE row for the page. Use the visible DATA field as data and the visible INICIO/TRABALHO and TERMINO/TRABALHO fields as marcacoes.
- Work-start labels may appear as INICIO/TRABALHO, INICIO, ENTRADA, HORA INICIO, PEGADA, or SAIDA GARAGEM.
- Work-end labels may appear as TERMINO/TRABALHO, TERMINO, SAIDA, HORA TERMINO, LARGADA, CHEGADA GARAGEM or CONTAS.
- Ignore dates/times from signatures, electronic validation text, printed protocol text, QR codes, page numbers, addresses, phone numbers, totals, intervals, and footer/header metadata.
- Do not infer sequential dates. Do not duplicate a single-service page into multiple days.
- The number of work-start and work-end must match. If they do not match, return the earliest work-start with the latest work-end for each pair. For example, if a file has 2 work-start and 3 work-end we gonna consider the first pair and on the second we get the latest work-end.
- If the relevant date or work times are not readable, return {"rows":[]}.
Return ONLY JSON, no markdown."""

NORMALIZE_PROMPT = """You are a timesheet data parser. The following text is OCR output from a Brazilian labor timesheet document.
Extract ALL timesheet rows and return a JSON array where each element has:
- "data": date in DD/MM/YYYY format
- "marcacoes": array of every clock-in/clock-out time for the day, in chronological order, HH:MM format (empty array if none). Include every pair found (there can be 1, 2, 3 or more) - never cap or drop any. Do not include totals/duration columns (e.g. "QTDE", "Adicional").
- "ocorrencia_raw": occurrence code as written (or null)
Include rows with occurrences even without times.
Return ONLY the JSON array.

OCR TEXT:
"""


class GeminiExtractionError(Exception):
    pass


def _record_uncertain_request(error: httpx.RequestError, kind: str) -> None:
    if isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        ai_usage.record('gemini', _gemini_model(), kind, None)


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_response_text(response_json: dict) -> str:
    try:
        parts = response_json["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as e:
        raise GeminiExtractionError(f"Unexpected Gemini response structure: {e}") from e

    text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    text = "".join(text_parts).strip()
    if not text:
        raise GeminiExtractionError("Gemini returned an empty response")
    return text


def _loads_gemini_json(text: str) -> Any:
    text = _clean_json(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise GeminiExtractionError(f"Failed to parse Gemini response: {e}") from e


def _confidence_label(raw: dict[str, Any]) -> str | None:
    value = str(raw.get("confidence") or raw.get("confianca") or "").strip().lower()
    if value in {"high", "alta"}:
        return "high"
    if value in {"medium", "media", "mediana"}:
        return "medium"
    if value in {"low", "baixa"}:
        return "low"
    return None


def _ocr_warning_for_confidence(confidence: str | None) -> str | None:
    if confidence == "low":
        return "Baixa confianca OCR: conferir esta linha no PDF original."
    return None


def _rows_from_payload(payload: Any) -> list[TimesheetRow]:
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raw_rows = payload.get("records")
    else:
        raw_rows = None

    if not isinstance(raw_rows, list):
        return []

    rows: list[TimesheetRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue

        date_str = normalize_date(str(raw.get("data") or raw.get("date") or ""))
        if not date_str:
            continue

        raw_times = raw.get("marcacoes") or raw.get("times") or []
        if not isinstance(raw_times, list):
            raw_times = []

        marcacoes: list[str] = []
        for value in raw_times:
            normalized = normalize_time(str(value or ""))
            if normalized:
                marcacoes.append(normalized)

        for key in ("entrada", "entry", "inicio", "start", "saida", "exit", "termino", "end"):
            normalized = normalize_time(str(raw.get(key) or ""))
            if normalized and normalized not in marcacoes:
                marcacoes.append(normalized)

        occ_value = str(raw.get("ocorrencia_raw") or raw.get("ocorrencia") or raw.get("occurrence") or "").strip()
        occ_raw, occ_tipo = normalize_ocorrencia(occ_value) if occ_value else (None, None)
        if occ_tipo == "trabalho_normal":
            occ_raw, occ_tipo = None, None

        confidence = _confidence_label(raw)
        rows.append(TimesheetRow(
            data=date_str,
            marcacoes=marcacoes,
            ocorrencia_raw=occ_raw,
            ocorrencia_tipo=occ_tipo,
            ocr_confidence=confidence,
            ocr_warning=_ocr_warning_for_confidence(confidence),
        ))
    return rows


def _parse_gemini_response(response_json: dict, kind: str) -> list[TimesheetRow]:
    """Meter the call, then parse it.

    Metering comes first: Google bills a completed call whether or not its
    answer turns out to be parseable, so a discarded response still has to
    reach the cost ledger.
    """
    ai_usage.record("gemini", _gemini_model(), kind, ai_usage.usage_from_gemini(response_json))
    payload = _loads_gemini_json(_extract_response_text(response_json))
    return _rows_from_payload(payload)


async def extract_with_gemini(pdf_bytes: bytes) -> list[TimesheetRow]:
    if not is_gemini_configured():
        return []

    logger.info(
        "Calling Gemini extract API - model=%s pdf_size=%d bytes",
        settings.GEMINI_MODEL,
        len(pdf_bytes),
    )
    encoded = base64.b64encode(pdf_bytes).decode("utf-8")
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": encoded}},
                {"text": EXTRACTION_PROMPT},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
            "maxOutputTokens": 8192,
        },
    }
    response = None
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(GEMINI_TIMEOUT_SECONDS)) as client:
                response = await client.post(
                    _gemini_url(),
                    params={"key": settings.GEMINI_API_KEY},
                    json=body,
                )
            break
        except (httpx.TimeoutException, httpx.RequestError) as e:
            _record_uncertain_request(e, 'extract')
            if attempt >= GEMINI_RETRIES:
                raise GeminiExtractionError(
                    f"Gemini request failed after {attempt} attempt(s): {e}"
                ) from e
            logger.warning(
                "Gemini request failed on attempt %d/%d: %s",
                attempt,
                GEMINI_RETRIES,
                e,
            )
            await asyncio.sleep(2 * attempt)

    if response is None:
        raise GeminiExtractionError("Gemini request did not return a response")
    if response.status_code != 200:
        logger.error(
            "Gemini API error - status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GeminiExtractionError(
            f"Gemini API error {response.status_code}: {response.text[:300]}"
        )
    return _parse_gemini_response(response.json(), "extract")


def _split_pdf_into_chunks(pdf_bytes: bytes, chunk_size: int) -> list[bytes]:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    chunks: list[bytes] = []

    for start in range(0, len(reader.pages), chunk_size):
        writer = pypdf.PdfWriter()
        for page_index in range(start, min(start + chunk_size, len(reader.pages))):
            writer.add_page(reader.pages[page_index])
        out = BytesIO()
        writer.write(out)
        chunks.append(out.getvalue())

    return chunks


async def extract_with_gemini_adaptive(
    pdf_bytes: bytes,
    chunk_size: int = GEMINI_PAGE_CHUNK_SIZE,
) -> list[TimesheetRow]:
    try:
        page_count = len(pypdf.PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception:
        return await extract_with_gemini(pdf_bytes)

    if page_count <= chunk_size:
        return await extract_with_gemini(pdf_bytes)

    rows: list[TimesheetRow] = []
    last_error: GeminiExtractionError | None = None

    for chunk in _split_pdf_into_chunks(pdf_bytes, chunk_size):
        try:
            rows.extend(await extract_with_gemini(chunk))
            continue
        except GeminiExtractionError as e:
            last_error = e
            logger.warning("Gemini chunk failed; retrying as single pages: %s", e)

        for page_chunk in _split_pdf_into_chunks(chunk, 1):
            try:
                rows.extend(await extract_with_gemini(page_chunk))
            except GeminiExtractionError as e:
                last_error = e
                logger.warning("Gemini single-page fallback failed: %s", e)

    if rows:
        return rows
    if last_error is not None:
        raise last_error
    return []


async def normalize_text_with_gemini(ocr_text: str) -> list[TimesheetRow]:
    if not is_gemini_configured():
        return []

    logger.info("Calling Gemini normalize API - text_len=%d chars", len(ocr_text))
    body = {
        "contents": [{
            "parts": [{"text": NORMALIZE_PROMPT + ocr_text}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
            "maxOutputTokens": 8192,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(
                _gemini_url(),
                params={"key": settings.GEMINI_API_KEY},
                json=body,
            )
    except httpx.RequestError as error:
        _record_uncertain_request(error, 'normalize')
        raise
    if response.status_code != 200:
        logger.error(
            "Gemini normalization error - status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GeminiExtractionError(
            f"Gemini normalization error {response.status_code}: {response.text[:300]}"
        )
    return _parse_gemini_response(response.json(), "normalize")
