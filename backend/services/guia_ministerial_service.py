from __future__ import annotations
import asyncio
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

MISTRAL_FILES_URL = "https://api.mistral.ai/v1/files"
MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"

CHUNK_SIZE = 20  # pages per Mistral OCR upload (stays under 50 MB limit)

GUIA_SYSTEM_PROMPT = """You are extracting work time records from OCR text of Brazilian labor timesheet documents.

The text comes from scanned handwritten work tickets (Guia Ministerial, Papeleta de Serviço Externo) or any handwritten daily work record. Format and labels may vary widely.

For EACH daily work record found, extract:
- "worker_name": the worker's full name (string, or null if not identifiable)
- "worker_id": the worker's ID, registration number, or code (string, or null if absent)
- "data": the date in DD/MM/YYYY format
- "primeira_entrada": the FIRST clock-in/arrival/start time of that day in HH:MM format
- "ultima_saida": the LAST clock-out/departure/end time of that day in HH:MM format

Rules:
- If a record has multiple time rows (multiple trips or shifts in one day), extract ONLY the first clock-in and LAST clock-out — ignore all intermediate times
- Times may be written as HHMM (e.g. "1350" means "13:50"), HH:MM, H:MM — always output as HH:MM
- Dates may be DD/MM/YY or DD/MM/YYYY — always output DD/MM/YYYY (treat 2-digit years as 20XX)
- If a time or date is illegible or missing, use null
- If multiple different workers appear, return one entry per worker per date
- Return an empty records array if no valid records are found

Return ONLY a valid JSON object with a "records" key. Example:
{"records": [{"worker_name": "JOÃO SILVA", "worker_id": "12345", "data": "25/01/2024", "primeira_entrada": "06:30", "ultima_saida": "14:50"}]}"""

GUIA_USER_TEMPLATE = (
    "Extract all work time records from the following OCR text and return JSON.\n\n"
    "--- OCR TEXT ---\n{text}\n--- END ---"
)


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


def _parse_chat_response(response_json: dict) -> list[dict]:
    try:
        text = response_json["choices"][0]["message"]["content"]
        text = _clean_json(text)
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            records = data.get("records") or data.get("data") or []
            return records if isinstance(records, list) else []
        return []
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("guia: failed to parse chat response: %s", e)
        return []


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
    """Group raw records by (worker, date). Keep earliest entrada, latest saída."""
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


async def _ocr_chunk(chunk_bytes: bytes) -> list[str]:
    """Upload one PDF chunk to Mistral OCR; return list of markdown strings per page."""
    headers = {"Authorization": f"Bearer {settings.MISTRAL_API_KEY}"}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=10.0)
    ) as client:
        upload = await client.post(
            MISTRAL_FILES_URL,
            headers=headers,
            files={"file": ("chunk.pdf", chunk_bytes, "application/pdf")},
            data={"purpose": "ocr"},
        )
        if upload.status_code != 200:
            raise GuiaExtractionError(
                f"Mistral file upload failed {upload.status_code}: {upload.text[:300]}"
            )
        file_id = upload.json()["id"]

        ocr = await client.post(
            MISTRAL_OCR_URL,
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": "mistral-ocr-latest",
                "document": {"type": "file", "file_id": file_id},
                "include_image_base64": False,
            },
        )

        # Best-effort cleanup
        try:
            await client.delete(f"{MISTRAL_FILES_URL}/{file_id}", headers=headers)
        except Exception:
            pass

        if ocr.status_code != 200:
            raise GuiaExtractionError(
                f"Mistral OCR failed {ocr.status_code}: {ocr.text[:300]}"
            )

    pages = ocr.json().get("pages", [])
    return [p.get("markdown", "") for p in pages]


_CHAT_MAX_RETRIES = 5
_CHAT_DEFAULT_WAIT = 60  # seconds to wait when Retry-After header is absent


async def _extract_from_markdown(pages: list[str]) -> list[dict]:
    """Call Mistral small chat to extract structured records from OCR markdown.

    Retries up to _CHAT_MAX_RETRIES times on 429 rate-limit responses,
    honouring the Retry-After header when present.
    """
    text = "\n\n---PAGE BREAK---\n\n".join(p for p in pages if p.strip())
    if not text:
        return []

    body = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": GUIA_SYSTEM_PROMPT},
            {"role": "user", "content": GUIA_USER_TEMPLATE.format(text=text)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 8192,
    }

    headers = {
        "Authorization": f"Bearer {settings.MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(_CHAT_MAX_RETRIES + 1):
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(MISTRAL_CHAT_URL, headers=headers, json=body)

        if response.status_code == 200:
            return _parse_chat_response(response.json())

        if response.status_code == 429 and attempt < _CHAT_MAX_RETRIES:
            wait = int(response.headers.get("retry-after", _CHAT_DEFAULT_WAIT))
            logger.warning(
                "guia: chat rate limited — waiting %ds (attempt %d/%d)",
                wait, attempt + 1, _CHAT_MAX_RETRIES,
            )
            await asyncio.sleep(wait)
            continue

        logger.error(
            "guia: Mistral chat error — status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise GuiaExtractionError(
            f"Mistral chat error {response.status_code}: {response.text[:300]}"
        )

    return []  # unreachable; satisfies type checker


async def _process_chunk(chunk_bytes: bytes) -> list[dict]:
    """OCR one PDF chunk then extract records — the testable unit."""
    pages = await _ocr_chunk(chunk_bytes)
    logger.info("guia: OCR returned %d pages for chunk", len(pages))
    return await _extract_from_markdown(pages)


async def extract_with_guia_ministerial(
    pdf_bytes: bytes, chunk_size: int = CHUNK_SIZE
) -> list[TimesheetRow]:
    logger.info("guia: starting extraction — pdf_size=%d bytes", len(pdf_bytes))
    chunks = _split_pdf_chunks(pdf_bytes, chunk_size=chunk_size)
    logger.info("guia: split into %d chunks of up to %d pages", len(chunks), chunk_size)

    all_records: list[dict] = []
    for i, chunk in enumerate(chunks):
        logger.info("guia: processing chunk %d/%d — %d bytes", i + 1, len(chunks), len(chunk))
        records = await _process_chunk(chunk)
        logger.info("guia: chunk %d → %d records", i + 1, len(records))
        all_records.extend(records)

    rows = _aggregate(all_records)
    logger.info(
        "guia: done — total_rows=%d unique workers=%d",
        len(rows),
        len({r.worker_name for r in rows}),
    )
    return rows
