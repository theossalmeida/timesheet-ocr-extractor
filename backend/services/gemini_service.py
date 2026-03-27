from __future__ import annotations
import base64
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

from config import settings
from models.timesheet import TimesheetRow
from utils.normalizers import normalize_date, normalize_time, normalize_ocorrencia

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)

EXTRACTION_PROMPT = """You are a timesheet data extractor for Brazilian labor documents.
Extract ALL timesheet rows from the provided document.
Return a JSON array where each element has these fields (all optional, use null if absent):
- "data": date in DD/MM/YYYY format
- "entrada_1": first entry time in HH:MM format — ONLY from columns explicitly labelled "Entrada" or equivalent
- "saida_1": first exit time in HH:MM format — ONLY from columns explicitly labelled "Saída" or equivalent
- "entrada_2": second entry time in HH:MM format (after lunch break) — ONLY if a second "Entrada" column exists
- "saida_2": second exit time in HH:MM format — ONLY if a second "Saída" column exists
- "ocorrencia_raw": occurrence/absence code exactly as written (e.g. "FERIAS", "FALTA", "DSR")
IMPORTANT: columns labelled "Acréscimos", "Extras", "Adicional", "Intervalo", or similar are NOT entrada/saída — ignore them entirely.
Include rows with absences or occurrences even if no times are present.
Return ONLY the JSON array, no explanation, no markdown."""

NORMALIZE_PROMPT = """You are a timesheet data parser. The following text is OCR output from a Brazilian labor timesheet document.
Extract ALL timesheet rows and return a JSON array where each element has:
- "data": date in DD/MM/YYYY format
- "entrada_1": first entry time in HH:MM format (or null)
- "saida_1": first exit time in HH:MM format (or null)
- "entrada_2": second entry time in HH:MM (or null)
- "saida_2": second exit time in HH:MM (or null)
- "ocorrencia_raw": occurrence code as written (or null)
Include rows with occurrences even without times.
Return ONLY the JSON array.

OCR TEXT:
"""


class GeminiExtractionError(Exception):
    pass


def _clean_json(text: str) -> str:
    """Strip markdown code fences and fix common Gemini JSON issues."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_gemini_response(response_json: dict) -> list[TimesheetRow]:
    try:
        text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        text = _clean_json(text)
        data = json.loads(text)
    except (KeyError, IndexError) as e:
        raise GeminiExtractionError(f"Unexpected Gemini response structure: {e}") from e
    except json.JSONDecodeError as e:
        logger.warning("Gemini JSON parse failed — attempting truncation recovery: %s", e)
        # Try truncating to last valid complete object
        last_bracket = text.rfind("},")
        if last_bracket > 0:
            try:
                data = json.loads(text[: last_bracket + 1] + "]")
            except json.JSONDecodeError:
                raise GeminiExtractionError(f"Failed to parse Gemini response: {e}") from e
        else:
            raise GeminiExtractionError(f"Failed to parse Gemini response: {e}") from e

    rows: list[TimesheetRow] = []
    for item in data:
        occ_raw, occ_tipo = normalize_ocorrencia(item.get("ocorrencia_raw") or "")
        rows.append(TimesheetRow(
            data=normalize_date(item.get("data") or ""),
            entrada_1=normalize_time(item.get("entrada_1") or ""),
            saida_1=normalize_time(item.get("saida_1") or ""),
            entrada_2=normalize_time(item.get("entrada_2") or ""),
            saida_2=normalize_time(item.get("saida_2") or ""),
            ocorrencia_raw=occ_raw,
            ocorrencia_tipo=occ_tipo,
        ))
    return rows


async def extract_with_gemini(pdf_bytes: bytes) -> list[TimesheetRow]:
    logger.info("Calling Gemini extract API — pdf_size=%d bytes", len(pdf_bytes))
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
            "temperature": 0.1,
            "maxOutputTokens": 65536,
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        response = await client.post(
            GEMINI_URL,
            params={"key": settings.GEMINI_API_KEY},
            json=body,
        )
    if response.status_code != 200:
        logger.error(
            "Gemini API error — status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GeminiExtractionError(
            f"Gemini API error {response.status_code}: {response.text[:300]}"
        )
    return _parse_gemini_response(response.json())


async def normalize_text_with_gemini(ocr_text: str) -> list[TimesheetRow]:
    logger.info("Calling Gemini normalize API — text_len=%d chars", len(ocr_text))
    body = {
        "contents": [{
            "parts": [{"text": NORMALIZE_PROMPT + ocr_text}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 65536,
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        response = await client.post(
            GEMINI_URL,
            params={"key": settings.GEMINI_API_KEY},
            json=body,
        )
    if response.status_code != 200:
        logger.error(
            "Gemini normalization error — status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GeminiExtractionError(
            f"Gemini normalization error {response.status_code}: {response.text[:300]}"
        )
    return _parse_gemini_response(response.json())
