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

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
CHUNK_SIZE = 5  # pages per Gemini request

CONTRACHEQUE_PROMPT = """Você está extraindo dados de contracheques (holerites) de funcionários da Petrobras.

Para CADA PÁGINA do documento, extraia:
1. "competencia": o campo "Mês/Ano" ou "Competência" (formato: MM/YYYY, ex: "01/2022")
2. "itens": lista de itens de proventos da coluna "Descrição", **parando IMEDIATAMENTE antes de "Total de Proventos"**
   - Cada item: {"descricao": "nome do item", "valor": valor_numerico}
   - Converta valores monetários: "R$ 10.568,88" → 10568.88 (remova R$, pontos de milhar, troque vírgula por ponto)
   - Inclua APENAS PROVENTOS — NÃO inclua deduções, descontos nem totais
   - Se a página não for um contracheque válido, retorne lista vazia para ela

Retorne APENAS JSON válido com a estrutura:
{"paginas": [{"competencia": "01/2022", "itens": [{"descricao": "Salário Básico", "valor": 10568.88}, {"descricao": "Anuênio", "valor": 1234.50}]}, ...]}"""

_MAX_RETRIES = 3
_DEFAULT_WAIT = 15


class ContrachequeExtractionError(Exception):
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
    """Parse Gemini response and return list of per-page data."""
    try:
        text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        text = _clean_json(text)
        data = json.loads(text)
        if isinstance(data, dict):
            paginas = data.get("paginas") or []
            return paginas if isinstance(paginas, list) else []
        if isinstance(data, list):
            return data
        return []
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("contracheque: failed to parse Gemini response: %s", e)
        return []


async def _process_chunk(chunk_bytes: bytes) -> list[dict]:
    """Send PDF chunk to Gemini for contracheque data extraction."""
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

    return []  # unreachable; satisfies type checker


def _aggregate_salary_data(all_pages: list[dict]) -> dict[str, dict[str, dict[str, float]]]:
    """
    Aggregate per-page data into structure:
      {year: {month_num_str: {descricao: valor}}}

    If the same (year, month, descricao) appears on multiple pages, the last value wins.
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
        month_key = str(int(month_str))  # strip leading zero for dict key

        if year not in result:
            result[year] = {}
        if month_key not in result[year]:
            result[year][month_key] = {}

        for item in itens:
            descricao = str(item.get("descricao") or "").strip()
            valor = item.get("valor")
            if not descricao:
                continue
            try:
                valor_float = float(valor) if valor is not None else 0.0
            except (TypeError, ValueError):
                valor_float = 0.0
            result[year][month_key][descricao] = valor_float

    return result


async def stream_contracheque_extraction(
    pdf_bytes: bytes, original_stem: str, chunk_size: int = CHUNK_SIZE
):
    """Async generator yielding SSE strings for the contracheque extraction."""
    import json as _json
    import base64 as _b64
    from services.contracheque_excel_builder import build_contracheque_excel

    try:
        chunks = _split_pdf_chunks(pdf_bytes, chunk_size)
        total = len(chunks)
        all_pages: list[dict] = []

        for i, chunk in enumerate(chunks):
            yield f"data: {_json.dumps({'type': 'progress', 'chunk': i + 1, 'total': total})}\n\n"

            task = asyncio.create_task(_process_chunk(chunk))
            while not task.done():
                yield ": keep-alive\n\n"
                await asyncio.sleep(15)

            exc = task.exception()
            if exc is not None:
                logger.error("contracheque stream: chunk %d failed — %s", i + 1, exc)
                yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
                return

            all_pages.extend(task.result())

        salary_data = _aggregate_salary_data(all_pages)

        if not salary_data:
            yield f"data: {_json.dumps({'type': 'error', 'message': 'Nenhum dado de contracheque encontrado no PDF.'})}\n\n"
            return

        months_count = sum(len(months) for months in salary_data.values())

        excel_bytes = build_contracheque_excel(salary_data)

        yield "data: " + _json.dumps({
            "type": "done",
            "excel_b64": _b64.b64encode(excel_bytes).decode(),
            "excel_filename": f"contracheque_{original_stem}.xlsx",
            "months_extracted": months_count,
            "provider": "gemini-contracheque",
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("contracheque stream: unexpected error — %s", e)
        yield f"data: {_json.dumps({'type': 'error', 'message': 'Erro interno ao processar contracheque.'})}\n\n"
