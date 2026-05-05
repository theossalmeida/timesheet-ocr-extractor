from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re

import pypdf

from services.contracheque_service import (
    CHUNK_SIZE,
    _extract_all_pdfplumber,
    _make_chunks,
    _process_chunk_gemini,
    _split_pages_by_index,
)
from services.contracheque_extra_hours_excel_builder import (
    build_contracheque_extra_hours_excel,
)

logger = logging.getLogger(__name__)

_EXTRA_HOUR_RE = re.compile(
    r"(\bHE\b|Hora\s+Extra|Horas\s+Extras|Extra|RSR-HE|Rep\.\s*Sem\.\s*Rem\s+HE)",
    re.IGNORECASE,
)


def is_extra_hour_description(description: str) -> bool:
    return bool(_EXTRA_HOUR_RE.search(description or ""))


def _normalize_description(description: str) -> str:
    return re.sub(r"\s+", " ", description).strip()


def _month_sort_key(competencia: str) -> tuple[int, int]:
    try:
        month, year = competencia.split("/")
        return int(year), int(month)
    except (ValueError, AttributeError):
        return 9999, 99


def aggregate_extra_hours(
    pages: list[dict],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    extra_hours_data: dict[str, dict[str, float]] = {}
    columns: list[str] = []
    seen_columns: set[str] = set()

    for page in sorted(
        pages,
        key=lambda p: _month_sort_key(str(p.get("competencia") or "")),
    ):
        competencia = str(page.get("competencia") or "").strip()
        if not re.match(r"^\d{2}/\d{4}$", competencia):
            logger.warning("contracheque extra-hours: invalid competencia: %s", competencia)
            continue

        for item in page.get("itens") or []:
            description = _normalize_description(str(item.get("descricao") or ""))
            if not description or not is_extra_hour_description(description):
                continue

            try:
                value = float(item.get("valor") or 0)
            except (TypeError, ValueError):
                value = 0.0

            if description not in seen_columns:
                seen_columns.add(description)
                columns.append(description)

            month_values = extra_hours_data.setdefault(competencia, {})
            month_values[description] = month_values.get(description, 0.0) + value

    return extra_hours_data, columns


async def stream_contracheque_extra_hours_extraction(
    pdf_bytes: bytes,
    original_stem: str,
    chunk_size: int = CHUNK_SIZE,
):
    try:
        total_pages = len(pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)

        yield "data: " + json.dumps({
            "type": "progress",
            "chunk": 0,
            "total": 1,
            "step": "pdfplumber",
            "message": f"Analisando {total_pages} paginas com pdfplumber...",
        }) + "\n\n"

        plumber_results, failed_indices = await asyncio.get_running_loop().run_in_executor(
            None,
            _extract_all_pdfplumber,
            pdf_bytes,
        )
        all_pages: list[dict] = list(plumber_results)

        if failed_indices:
            logger.info(
                "contracheque extra-hours: sending %d page(s) to Gemini",
                len(failed_indices),
            )
            failed_page_bytes = _split_pages_by_index(pdf_bytes, failed_indices)
            gemini_chunks = _make_chunks(failed_page_bytes, chunk_size)
            total_chunks = len(gemini_chunks)

            for i, chunk in enumerate(gemini_chunks):
                yield "data: " + json.dumps({
                    "type": "progress",
                    "chunk": i + 1,
                    "total": total_chunks,
                    "step": "gemini",
                    "message": f"Gemini: processando parte {i + 1} de {total_chunks}...",
                }) + "\n\n"

                task = asyncio.create_task(_process_chunk_gemini(chunk))
                while not task.done():
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(15)

                exc = task.exception()
                if exc is not None:
                    logger.error("contracheque extra-hours: Gemini chunk failed: %s", exc)
                    yield "data: " + json.dumps({"type": "error", "message": str(exc)}) + "\n\n"
                    return

                all_pages.extend(task.result())
        else:
            yield "data: " + json.dumps({
                "type": "progress",
                "chunk": 1,
                "total": 1,
                "step": "pdfplumber",
                "message": "Extracao concluida com pdfplumber.",
            }) + "\n\n"

        extra_hours_data, columns = aggregate_extra_hours(all_pages)
        if not extra_hours_data or not columns:
            yield "data: " + json.dumps({
                "type": "error",
                "message": "Nenhuma verba de horas extras encontrada no PDF.",
            }) + "\n\n"
            return

        provider = (
            "pdfplumber"
            if not failed_indices
            else ("pdfplumber+gemini" if plumber_results else "gemini")
        )
        excel_bytes = build_contracheque_extra_hours_excel(extra_hours_data, columns)

        yield "data: " + json.dumps({
            "type": "done",
            "excel_b64": base64.b64encode(excel_bytes).decode(),
            "excel_filename": f"horas_extras_{original_stem}.xlsx",
            "months_extracted": len(extra_hours_data),
            "columns_extracted": len(columns),
            "provider": provider,
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("contracheque extra-hours stream: unexpected error: %s", e)
        yield "data: " + json.dumps({
            "type": "error",
            "message": "Erro interno ao processar horas extras do contracheque.",
        }) + "\n\n"
