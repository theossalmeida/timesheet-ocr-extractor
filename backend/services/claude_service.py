from __future__ import annotations
from io import BytesIO
import asyncio
import base64
import json
import logging
import re
from typing import Any, Callable

import anthropic
import pypdf

logger = logging.getLogger(__name__)

from config import settings
from models.timesheet import TimesheetRow
from services import ai_usage
from utils.normalizers import normalize_date, normalize_time, normalize_ocorrencia

CLAUDE_PAGE_CHUNK_SIZE = 2
CLAUDE_TIMEOUT_SECONDS = 180.0
# The SDK retries 408/409/429/5xx and connection errors with backoff, honouring
# Retry-After, so quota and overload responses never drop a chunk on first hit.
CLAUDE_MAX_RETRIES = 2
CLAUDE_MAX_OUTPUT_TOKENS = 16000


def _max_concurrent_chunks() -> int:
    return max(1, settings.ANTHROPIC_MAX_CONCURRENT_CHUNKS)


def is_claude_configured() -> bool:
    return bool((settings.ANTHROPIC_API_KEY or "").strip())


def _claude_model() -> str:
    return (settings.ANTHROPIC_MODEL or "claude-haiku-5-5").strip()


def _new_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=CLAUDE_TIMEOUT_SECONDS,
        max_retries=CLAUDE_MAX_RETRIES,
    )


EXTRACTION_PROMPT = """You are a timesheet data extractor for Brazilian labor documents.
Read only the supplied PDF pages/images and return strict JSON.

Return this exact JSON object shape:
{"rows":[{"data":"DD/MM/YYYY","marcacoes":["HH:MM","HH:MM"], "intervalo": "HH:MM", "ocorrencia":null,"confidence":"high|medium|low"}]}

Rules:
- Extract only values visibly anchored to the work-time table or service form fields on the page.
- For a normal cartao de ponto table, output one row per visible work date. Keep the punch times visible on that same row/date, in reading order.
- For a single-service form such as PAPELETA DE SERVICOS, GUIA MINISTERIAL, ordem de servico, viagem/linha service sheet, or similar, output at most ONE row for the page. Use the visible DATA field as data and the visible INICIO/TRABALHO and TERMINO/TRABALHO fields as marcacoes.
- Work-start labels may appear as INICIO/TRABALHO, INICIO, ENTRADA, HORA INICIO, PEGADA, or SAIDA GARAGEM, ALWAYS CONSIDER THE EARLIER ONE WITH THOSE LABELS.
- Work-end labels may appear as TERMINO/TRABALHO, TERMINO, SAIDA, HORA TERMINO, LARGADA, CHEGADA GARAGEM or CONTAS, ALWAYS CONSIDER THE LATEST ONE WITH THOSE LABELS.
- If the page/table has a column or field labeled INTERVALO, INT., or INTER., you must collect all non-zero interval values found across all trip rows/entries, sum them up as total minutes, and format the sum as HH:MM (e.g., if intervals are 6+6+6+6+5+6+1+8 = 44 minutes, return "00:44"). If no intervals are present, return null.
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


class ClaudeExtractionError(Exception):
    pass


async def _complete(
    client: anthropic.AsyncAnthropic, content: list[dict], kind: str
) -> list[TimesheetRow]:
    """Send one request, meter it, then parse it.

    Metering comes first: Anthropic bills a completed call whether or not its
    answer turns out to be parseable, so a discarded response still has to
    reach the cost ledger.
    """
    model = _claude_model()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=CLAUDE_MAX_OUTPUT_TOKENS,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APITimeoutError as error:
        ai_usage.record("claude", model, kind, None)
        raise ClaudeExtractionError(f"Claude request timed out: {error}") from error
    except anthropic.APIConnectionError as error:
        raise ClaudeExtractionError(f"Claude request failed: {error}") from error
    except anthropic.APIStatusError as error:
        logger.error("Claude API error - status=%d body=%s", error.status_code, str(error)[:300])
        raise ClaudeExtractionError(f"Claude API error {error.status_code}: {str(error)[:300]}") from error

    ai_usage.record("claude", model, kind, ai_usage.usage_from_claude(response.usage))
    if response.stop_reason == "refusal":
        raise ClaudeExtractionError("Claude declined the request")
    payload = _loads_claude_json(_extract_response_text(response))
    return _rows_from_payload(payload)


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_response_text(response: Any) -> str:
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise ClaudeExtractionError("Claude returned an empty response")
    return text


def _loads_claude_json(text: str) -> Any:
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
        raise ClaudeExtractionError(f"Failed to parse Claude response: {e}") from e


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


async def extract_with_claude(
    pdf_bytes: bytes, *, client: anthropic.AsyncAnthropic | None = None
) -> list[TimesheetRow]:
    if not is_claude_configured():
        return []

    logger.info(
        "Calling Claude extract API - model=%s pdf_size=%d bytes",
        _claude_model(),
        len(pdf_bytes),
    )
    content = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(pdf_bytes).decode("utf-8"),
            },
        },
        {"type": "text", "text": EXTRACTION_PROMPT},
    ]
    if client is not None:
        return await _complete(client, content, "extract")
    async with _new_client() as own_client:
        return await _complete(own_client, content, "extract")


def _split_pdf_into_chunks(
    pdf_bytes: bytes, chunk_size: int, *, reader: pypdf.PdfReader | None = None
) -> list[bytes]:
    if reader is None:
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


async def extract_with_claude_adaptive(
    pdf_bytes: bytes,
    chunk_size: int = CLAUDE_PAGE_CHUNK_SIZE,
    semaphore: asyncio.Semaphore | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[TimesheetRow]:
    """Extract a PDF, splitting it into page chunks sent concurrently.

    Pass `semaphore` to share one concurrency budget across several calls;
    without it each call gets its own, so N concurrent callers would reach
    N times the per-call limit in flight.

    `on_progress(done, total)` fires as each chunk finishes, so a caller
    streaming to a browser can report real progress instead of a guess.
    """
    if not is_claude_configured():
        return []
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    semaphore = semaphore or asyncio.Semaphore(_max_concurrent_chunks())

    def report(done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(done, total)

    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except Exception:
        async with semaphore:
            rows = await extract_with_claude(pdf_bytes)
        report(1, 1)
        return rows

    if page_count <= chunk_size:
        async with semaphore:
            rows = await extract_with_claude(pdf_bytes)
        report(1, 1)
        return rows

    async def extract_chunk(
        chunk: bytes, client: anthropic.AsyncAnthropic
    ) -> tuple[list[TimesheetRow], ClaudeExtractionError | None]:
        async with semaphore:
            try:
                return await extract_with_claude(chunk, client=client), None
            except ClaudeExtractionError as error:
                last_error = error
                logger.warning("Claude chunk failed; retrying as single pages: %s", error)

            page_chunks = await asyncio.to_thread(_split_pdf_into_chunks, chunk, 1)
            chunk_rows: list[TimesheetRow] = []
            for page_chunk in page_chunks:
                try:
                    chunk_rows.extend(await extract_with_claude(page_chunk, client=client))
                except ClaudeExtractionError as error:
                    last_error = error
                    logger.warning("Claude single-page fallback failed: %s", error)
            return chunk_rows, last_error

    chunks = await asyncio.to_thread(_split_pdf_into_chunks, pdf_bytes, chunk_size, reader=reader)
    finished = 0
    report(finished, len(chunks))

    async def extract_and_report(chunk: bytes, client: anthropic.AsyncAnthropic):
        nonlocal finished
        try:
            return await extract_chunk(chunk, client)
        finally:
            finished += 1
            report(finished, len(chunks))

    async with _new_client() as client:
        results = await asyncio.gather(
            *(extract_and_report(chunk, client) for chunk in chunks),
            return_exceptions=True,
        )
    rows: list[TimesheetRow] = []
    last_error: ClaudeExtractionError | None = None
    for result in results:
        if isinstance(result, BaseException):
            raise result
        chunk_rows, chunk_error = result
        rows.extend(chunk_rows)
        if chunk_error is not None:
            last_error = chunk_error

    if rows:
        return rows
    if last_error is not None:
        raise last_error
    return []


async def normalize_text_with_claude(ocr_text: str) -> list[TimesheetRow]:
    if not is_claude_configured():
        return []

    logger.info("Calling Claude normalize API - text_len=%d chars", len(ocr_text))
    content = [{"type": "text", "text": NORMALIZE_PROMPT + ocr_text}]
    async with _new_client() as client:
        return await _complete(client, content, "normalize")
