from __future__ import annotations
import asyncio
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

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"

CHUNK_SIZE = 20  # pages per Gemini request

GUIA_PROMPT = """Você está extraindo registros de ponto de documentos trabalhistas brasileiros (Guia Ministerial / Papeleta de Serviço Externo).

Para CADA registro diário encontrado, extraia:
- "data": a data no formato DD/MM/YYYY
- "entrada": o horário de entrada no formato HH:MM
  (procure por rótulos como: hora entrada, hora início, hora começo, entrada, início, saída da garagem)
- "saida": o horário de saída no formato HH:MM
  (procure por rótulos como: hora saída, hora término, hora fim, saída, término, chegada à garagem)

Regras:
- Use SEMPRE o horário MAIS CEDO encontrado como "entrada" e o MAIS TARDE como "saida" para cada data — mesmo que haja múltiplas linhas ou turnos no mesmo dia
- Horários podem estar escritos como HHMM (ex: "1350" → "13:50"), HH:MM ou H:MM — retorne sempre HH:MM
- Datas podem estar em DD/MM/AA ou DD/MM/AAAA — retorne sempre DD/MM/AAAA (anos com 2 dígitos = 20XX)
- Se um horário ou data estiver ilegível ou ausente, use null
- Retorne array vazio se nenhum registro válido for encontrado

Retorne APENAS JSON válido com a chave "records":
{"records": [{"data": "25/01/2024", "entrada": "06:30", "saida": "14:50"}]}"""

_MAX_RETRIES = 3
_DEFAULT_WAIT = 15


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


def _parse_gemini_response(response_json: dict) -> list[dict]:
    try:
        text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        text = _clean_json(text)
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            records = data.get("records") or data.get("data") or []
            return records if isinstance(records, list) else []
        return []
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("guia: failed to parse Gemini response: %s", e)
        return []


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


async def _process_chunk(chunk_bytes: bytes) -> list[dict]:
    """Send PDF chunk directly to Gemini — no OCR pre-step, single API call."""
    encoded = base64.b64encode(chunk_bytes).decode()
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": encoded}},
                {"text": GUIA_PROMPT},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 16384,
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }

    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=10.0)
        ) as client:
            response = await client.post(
                GEMINI_URL,
                headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                json=body,
            )

        if response.status_code == 200:
            records = _parse_gemini_response(response.json())
            logger.info("guia: Gemini returned %d records for chunk", len(records))
            return records

        if response.status_code in (429, 503) and attempt < _MAX_RETRIES:
            wait = int(response.headers.get("retry-after", _DEFAULT_WAIT))
            logger.warning(
                "guia: Gemini rate limited (%d) — waiting %ds (attempt %d/%d)",
                response.status_code, wait, attempt + 1, _MAX_RETRIES,
            )
            await asyncio.sleep(wait)
            continue

        logger.error(
            "guia: Gemini error — status=%d body=%s",
            response.status_code, response.text[:300],
        )
        raise GuiaExtractionError(
            f"Gemini error {response.status_code}: {response.text[:300]}"
        )

    return []  # unreachable; satisfies type checker


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
            yield f"data: {_json.dumps({'type': 'progress', 'chunk': i + 1, 'total': total})}\n\n"

            task = asyncio.create_task(_process_chunk(chunk))
            while not task.done():
                yield ": keep-alive\n\n"
                await asyncio.sleep(15)

            exc = task.exception()
            if exc is not None:
                logger.error("guia stream: chunk %d failed — %s", i + 1, exc)
                yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
                return

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
            "provider": "gemini-guia",
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
        records = await _process_chunk(chunk)
        logger.info("guia: chunk %d → %d records", i + 1, len(records))
        all_records.extend(records)

    rows = _aggregate(all_records)
    logger.info("guia: done — total_rows=%d", len(rows))
    return rows
