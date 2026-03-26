from __future__ import annotations
import base64
import io
import json
import logging
import re

import httpx
import pypdf

logger = logging.getLogger(__name__)

from config import settings
from models.timesheet import TimesheetRow
from utils.normalizers import normalize_date, normalize_time

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

CHUNK_SIZE = 20  # pages per Gemini request

GUIA_PROMPT = """You are extracting work time records from scanned Brazilian labor timesheet documents.

The document may be in any format: individual work tickets (Guia Ministerial, Papeleta de Serviço Externo), daily sheets, monthly timesheets, or any handwritten work record. The format, layout, and language of labels may vary.

For EACH daily work record found in the document, extract:
- "worker_name": the worker's full name (string, or null if not identifiable)
- "worker_id": the worker's ID, registration number, or code (string, or null if absent)
- "data": the date in DD/MM/YYYY format
- "primeira_entrada": the FIRST clock-in/arrival/start time of that day in HH:MM format
- "ultima_saida": the LAST clock-out/departure/end time of that day in HH:MM format

Rules:
- If a record has multiple time rows (e.g. multiple trips or shifts in one day), extract ONLY the first clock-in and LAST clock-out — ignore all intermediate times, breaks, and totals
- Times may be written as HHMM (e.g. "1350" means "13:50"), HH:MM, H:MM, or similar — always output as HH:MM
- Dates may be DD/MM/YY or DD/MM/YYYY — always output DD/MM/YYYY (treat 2-digit years as 20XX)
- If a time or date is illegible, use null
- If multiple different workers appear, return one entry per worker per date
- Return an empty array [] if no valid records are found

Return ONLY a JSON array, no explanation, no markdown. Example:
[{"worker_name": "JOÃO SILVA", "worker_id": "12345", "data": "25/01/2024", "primeira_entrada": "06:30", "ultima_saida": "14:50"}]"""


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


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_chunk_response(response_json: dict) -> list[dict]:
    try:
        text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        text = _clean_json(text)
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("guia: failed to parse Gemini chunk response: %s", e)
        return []


async def _call_gemini_chunk(chunk_bytes: bytes) -> list[dict]:
    encoded = base64.b64encode(chunk_bytes).decode("utf-8")
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": encoded}},
                {"text": GUIA_PROMPT},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 65536,
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        response = await client.post(
            GEMINI_URL,
            params={"key": settings.GEMINI_API_KEY},
            json=body,
        )
    if response.status_code != 200:
        logger.error(
            "guia: Gemini error — status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GuiaExtractionError(
            f"Gemini API error {response.status_code}: {response.text[:300]}"
        )
    return _parse_chunk_response(response.json())


def _normalize_worker(name: str | None, worker_id: str | None) -> str:
    name = (name or "").strip()
    worker_id = (worker_id or "").strip()
    if name:
        return name.upper()
    if worker_id:
        return f"MOTORISTA {worker_id}"
    return "DESCONHECIDO"


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    try:
        d, m, y = date_str.split("/")
        return (int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return (9999, 99, 99)


def _aggregate(records: list[dict]) -> list[TimesheetRow]:
    """Group raw Gemini records by (worker, date). Keep earliest entrada, latest saída."""
    # {worker_key: {date_str: {"entrada": str|None, "saida": str|None}}}
    grouped: dict[str, dict[str, dict[str, str | None]]] = {}

    for rec in records:
        worker = _normalize_worker(rec.get("worker_name"), rec.get("worker_id"))
        date_str = normalize_date(str(rec.get("data") or ""))
        if not date_str:
            continue

        entrada = normalize_time(str(rec.get("primeira_entrada") or ""))
        saida = normalize_time(str(rec.get("ultima_saida") or ""))

        if worker not in grouped:
            grouped[worker] = {}

        if date_str not in grouped[worker]:
            grouped[worker][date_str] = {"entrada": entrada, "saida": saida}
        else:
            existing = grouped[worker][date_str]
            if entrada and (not existing["entrada"] or entrada < existing["entrada"]):
                existing["entrada"] = entrada
            if saida and (not existing["saida"] or saida > existing["saida"]):
                existing["saida"] = saida

    rows: list[TimesheetRow] = []
    for worker, dates in grouped.items():
        for date_str, times in sorted(dates.items(), key=lambda kv: _date_sort_key(kv[0])):
            rows.append(TimesheetRow(
                data=date_str,
                entrada_1=times["entrada"],
                saida_1=times["saida"],
                worker_name=worker,
            ))
    return rows


async def extract_with_guia_ministerial(
    pdf_bytes: bytes, chunk_size: int = CHUNK_SIZE
) -> list[TimesheetRow]:
    logger.info("guia: starting extraction — pdf_size=%d bytes", len(pdf_bytes))
    chunks = _split_pdf_chunks(pdf_bytes, chunk_size=chunk_size)
    logger.info("guia: split into %d chunks of up to %d pages", len(chunks), chunk_size)

    all_records: list[dict] = []
    for i, chunk in enumerate(chunks):
        logger.info(
            "guia: processing chunk %d/%d — chunk_size=%d bytes",
            i + 1, len(chunks), len(chunk),
        )
        records = await _call_gemini_chunk(chunk)
        logger.info("guia: chunk %d → %d records", i + 1, len(records))
        all_records.extend(records)

    rows = _aggregate(all_records)
    logger.info("guia: done — total_rows=%d unique workers=%d", len(rows), len({r.worker_name for r in rows}))
    return rows
