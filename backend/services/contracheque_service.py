from __future__ import annotations
import asyncio
import base64
import io
import logging
import re

import httpx
import pdfplumber
import pypdf

logger = logging.getLogger(__name__)

from config import settings

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
CHUNK_SIZE = 5  # pages per Gemini request (used only for fallback)

CONTRACHEQUE_PROMPT = """Você está extraindo dados de contracheques (holerites) de funcionários da Petrobras.

Para CADA PÁGINA do documento, extraia:
1. "competencia": o campo "Mês/Ano" (formato: MM/YYYY, ex: "01/2022")
2. "itens": lista de proventos da coluna "Descrição", parando ANTES de "Total de Proventos"
   - Cada item: {"descricao": "nome", "valor": valor_numerico}
   - Converta valores: "R$ 10.568,88" → 10568.88
   - Inclua APENAS PROVENTOS, não deduções nem totais

Retorne APENAS JSON:
{"paginas": [{"competencia": "01/2022", "itens": [{"descricao": "Salário Básico", "valor": 10568.88}]}]}"""

_MAX_RETRIES = 3
_DEFAULT_WAIT = 15

# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches the item block header in the text: "Código  Descrição  Quantidade  Valor"
_HEADER_RE = re.compile(
    r"C.digo\s+Descri..o\s+Quantidade\s+Valor",
    re.IGNORECASE,
)

# Matches the stop line
_STOP_RE = re.compile(r"Total\s+de\s+Proventos", re.IGNORECASE)

# Matches a salary item line: "XXXX Description [qty] R$ value"
# Code is 4 chars (alphanumeric); value ends with ,DD
_ITEM_RE = re.compile(
    r"^[A-Z0-9]{4}\s+.+?R\$\s*([\d.]+,\d{2})\s*$",
    re.IGNORECASE,
)

# Matches the competência cell content: "Mês/Ano\nMM/YYYY"
_COMPETENCIA_RE = re.compile(r"M.s/Ano\s*[\n\r]+\s*(\d{2}/\d{4})", re.IGNORECASE)
# Fallback: last MM/YYYY on the "Nome ... Matrícula ... MM/YYYY" row
_COMP_FALLBACK_RE = re.compile(r"\d{6,7}\s+(\d{2}/\d{4})")


# ── Currency helpers ──────────────────────────────────────────────────────────

def _parse_currency(raw: str) -> float | None:
    """Convert 'R$ 10.568,88' or '10.568,88' to 10568.88."""
    s = re.sub(r"R\$\s*", "", raw).strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_item_line(line: str) -> tuple[str, float] | None:
    """
    Parse a salary item line such as:
      '0001 Salário Básico 30 R$ 10.568,88'
      '0192 Complemento da RMNR R$ 6.668,03'

    Returns (description, value) or None if the line doesn't match.
    """
    if not _ITEM_RE.match(line):
        return None

    # Split at 'R$' to isolate value
    rs_pos = line.rfind("R$")
    if rs_pos == -1:
        return None

    value_part = line[rs_pos + 2:].strip()
    valor = _parse_currency(value_part)
    if valor is None:
        return None

    pre = line[:rs_pos].strip()
    # Remove the 4-char code at the start
    pre = re.sub(r"^[A-Z0-9]{4}\s+", "", pre, flags=re.IGNORECASE).strip()
    # Remove optional trailing numeric quantity (e.g. "30", "0,07", "11")
    desc = re.sub(r"\s+\d+(?:[,.]\d+)?\s*$", "", pre).strip()

    if not desc:
        return None

    return desc, valor


# ── pdfplumber extraction ─────────────────────────────────────────────────────

def _extract_page_pdfplumber(page) -> dict | None:
    """
    Attempt to extract competência + salary items from a single pdfplumber Page.
    Returns {"competencia": "MM/YYYY", "itens": [{...}]} or None on failure.
    """
    # 1. Find competência
    competencia: str | None = None

    tables = page.extract_tables()
    for table in tables:
        for row in (table or [])[:3]:
            for cell in (row or []):
                if not cell:
                    continue
                m = _COMPETENCIA_RE.search(str(cell))
                if m:
                    competencia = m.group(1)
                    break
            if competencia:
                break
        if competencia:
            break

    if not competencia:
        text = page.extract_text() or ""
        m = _COMP_FALLBACK_RE.search(text)
        if m:
            competencia = m.group(1)

    if not competencia:
        return None

    # 2. Extract salary items from text
    text = page.extract_text() or ""
    if not text:
        return None

    header_match = _HEADER_RE.search(text)
    stop_match = _STOP_RE.search(text)

    if not header_match or not stop_match:
        return None

    # Block between header line end and "Total de Proventos"
    block_start = text.find("\n", header_match.start())
    if block_start == -1 or block_start >= stop_match.start():
        return None

    block = text[block_start:stop_match.start()]
    items: list[dict] = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        result = _parse_item_line(line)
        if result:
            desc, valor = result
            items.append({"descricao": desc, "valor": valor})

    if not items:
        return None

    return {"competencia": competencia, "itens": items}


def _extract_all_pdfplumber(
    pdf_bytes: bytes,
) -> tuple[list[dict], list[int]]:
    """
    Run pdfplumber on every page.
    Returns:
      - results: list of successfully extracted page dicts
      - failed_indices: 0-based page indices that need Gemini fallback
    """
    results: list[dict] = []
    failed: list[int] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                data = _extract_page_pdfplumber(page)
            except Exception as e:
                logger.warning("contracheque: pdfplumber error on page %d — %s", i, e)
                data = None

            if data:
                logger.info(
                    "contracheque: pdfplumber OK — page %d  comp=%s  items=%d",
                    i, data["competencia"], len(data["itens"]),
                )
                results.append(data)
            else:
                logger.info("contracheque: pdfplumber failed page %d → queued for Gemini", i)
                failed.append(i)

    return results, failed


# ── Gemini fallback ───────────────────────────────────────────────────────────

class ContrachequeExtractionError(Exception):
    pass


def _split_pages_by_index(pdf_bytes: bytes, indices: list[int]) -> list[bytes]:
    """Build a list of single-page PDFs for the given page indices."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    result: list[bytes] = []
    for idx in indices:
        writer = pypdf.PdfWriter()
        writer.add_page(reader.pages[idx])
        buf = io.BytesIO()
        writer.write(buf)
        result.append(buf.getvalue())
    return result


def _make_chunks(pages_bytes: list[bytes], chunk_size: int) -> list[bytes]:
    """Merge individual page bytes into multi-page chunk PDFs."""
    chunks: list[bytes] = []
    for start in range(0, len(pages_bytes), chunk_size):
        slice_ = pages_bytes[start : start + chunk_size]
        if len(slice_) == 1:
            chunks.append(slice_[0])
            continue
        writer = pypdf.PdfWriter()
        for page_bytes in slice_:
            reader = pypdf.PdfReader(io.BytesIO(page_bytes))
            for page in reader.pages:
                writer.add_page(page)
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
        data = __import__("json").loads(text)
        if isinstance(data, dict):
            paginas = data.get("paginas") or []
            return paginas if isinstance(paginas, list) else []
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.warning("contracheque: failed to parse Gemini response: %s", e)
        return []


async def _process_chunk_gemini(chunk_bytes: bytes) -> list[dict]:
    encoded = base64.b64encode(chunk_bytes).decode()
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": encoded}},
                {"text": CONTRACHEQUE_PROMPT},
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
            paginas = _parse_gemini_response(response.json())
            logger.info("contracheque: Gemini returned %d pages for chunk", len(paginas))
            return paginas

        if response.status_code in (429, 503) and attempt < _MAX_RETRIES:
            wait = int(response.headers.get("retry-after", _DEFAULT_WAIT))
            logger.warning(
                "contracheque: Gemini rate limited (%d) — waiting %ds (attempt %d/%d)",
                response.status_code, wait, attempt + 1, _MAX_RETRIES,
            )
            await asyncio.sleep(wait)
            continue

        logger.error(
            "contracheque: Gemini error — status=%d body=%s",
            response.status_code, response.text[:300],
        )
        raise ContrachequeExtractionError(
            f"Gemini error {response.status_code}: {response.text[:300]}"
        )

    return []


# ── Data aggregation ──────────────────────────────────────────────────────────

def _aggregate_salary_data(
    all_pages: list[dict],
) -> dict[str, dict[str, dict[str, float]]]:
    """
    Merge per-page records into:
      {year: {month_num_str: {descricao: valor}}}
    """
    result: dict[str, dict[str, dict[str, float]]] = {}

    for page in all_pages:
        competencia = page.get("competencia")
        itens = page.get("itens") or []

        if not competencia:
            continue

        match = re.match(r"^(\d{1,2})/(\d{4})$", str(competencia).strip())
        if not match:
            logger.warning("contracheque: invalid competencia format: %s", competencia)
            continue

        month_str, year = match.groups()
        month_key = str(int(month_str))  # strip leading zero

        result.setdefault(year, {}).setdefault(month_key, {})

        for item in itens:
            desc = str(item.get("descricao") or "").strip()
            valor = item.get("valor")
            if not desc:
                continue
            try:
                result[year][month_key][desc] = float(valor) if valor is not None else 0.0
            except (TypeError, ValueError):
                result[year][month_key][desc] = 0.0

    return result


# ── Streaming entry point ─────────────────────────────────────────────────────

async def stream_contracheque_extraction(
    pdf_bytes: bytes,
    original_stem: str,
    chunk_size: int = CHUNK_SIZE,
):
    """
    Async generator yielding SSE strings.

    Pipeline:
      1. Try pdfplumber on every page (fast, no API cost)
      2. For pages pdfplumber could not parse, fall back to Gemini
      3. Aggregate and build Excel
    """
    import json as _json
    import base64 as _b64
    from services.contracheque_excel_builder import build_contracheque_excel

    try:
        total_pages = len(pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)

        # ── Step 1: pdfplumber ──────────────────────────────────────
        yield f"data: {_json.dumps({'type': 'progress', 'chunk': 0, 'total': 1, 'step': 'pdfplumber', 'message': f'Analisando {total_pages} páginas com pdfplumber...'})}\n\n"

        plumber_results, failed_indices = await asyncio.get_event_loop().run_in_executor(
            None, _extract_all_pdfplumber, pdf_bytes
        )

        all_pages: list[dict] = list(plumber_results)

        logger.info(
            "contracheque: pdfplumber done — ok=%d  failed=%d",
            len(plumber_results), len(failed_indices),
        )

        # ── Step 2: Gemini fallback for failed pages ─────────────────
        if failed_indices:
            logger.info(
                "contracheque: sending %d page(s) to Gemini — indices %s",
                len(failed_indices), failed_indices,
            )
            failed_page_bytes = _split_pages_by_index(pdf_bytes, failed_indices)
            gemini_chunks = _make_chunks(failed_page_bytes, chunk_size)
            total_chunks = len(gemini_chunks)

            for i, chunk in enumerate(gemini_chunks):
                yield f"data: {_json.dumps({'type': 'progress', 'chunk': i + 1, 'total': total_chunks, 'step': 'gemini', 'message': f'Gemini: processando parte {i + 1} de {total_chunks}...'})}\n\n"

                task = asyncio.create_task(_process_chunk_gemini(chunk))
                while not task.done():
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(15)

                exc = task.exception()
                if exc is not None:
                    logger.error("contracheque: Gemini chunk %d failed — %s", i + 1, exc)
                    yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
                    return

                all_pages.extend(task.result())
        else:
            # Signal 100% even if no Gemini was needed
            yield f"data: {_json.dumps({'type': 'progress', 'chunk': 1, 'total': 1, 'step': 'pdfplumber', 'message': 'Extração concluída com pdfplumber.'})}\n\n"

        # ── Step 3: aggregate + build Excel ──────────────────────────
        salary_data = _aggregate_salary_data(all_pages)

        if not salary_data:
            yield f"data: {_json.dumps({'type': 'error', 'message': 'Nenhum dado de contracheque encontrado no PDF.'})}\n\n"
            return

        months_count = sum(len(months) for months in salary_data.values())
        provider = (
            "pdfplumber"
            if not failed_indices
            else ("pdfplumber+gemini" if plumber_results else "gemini")
        )

        excel_bytes = build_contracheque_excel(salary_data)

        yield "data: " + _json.dumps({
            "type": "done",
            "excel_b64": _b64.b64encode(excel_bytes).decode(),
            "excel_filename": f"contracheque_{original_stem}.xlsx",
            "months_extracted": months_count,
            "provider": provider,
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("contracheque stream: unexpected error — %s", e)
        yield f"data: {_json.dumps({'type': 'error', 'message': 'Erro interno ao processar contracheque.'})}\n\n"
