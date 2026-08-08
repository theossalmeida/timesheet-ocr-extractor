from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any

import httpx

from config import settings
from models.timesheet import TimesheetRow
from utils.normalizers import normalize_date, normalize_ocorrencia, normalize_time

logger = logging.getLogger(__name__)


class LocalVisionOCRError(Exception):
    pass


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def is_local_vision_ocr_configured() -> bool:
    return bool(
        (settings.LOCAL_VISION_OCR_BASE_URL or "").strip()
        and (settings.LOCAL_VISION_OCR_MODEL or "").strip()
    )


def _base_url() -> str:
    return settings.LOCAL_VISION_OCR_BASE_URL.rstrip("/")


def _response_to_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            raise LocalVisionOCRError("local vision model did not return JSON")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise LocalVisionOCRError("local vision model returned non-object JSON")
    return parsed


def _image_to_base64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _render_pdf_page_images(pdf_bytes: bytes) -> list[str]:
    from services.tesseract_ocr_service import _render_pdf_pages

    images = _render_pdf_pages(pdf_bytes, dpi=settings.LOCAL_VISION_OCR_DPI)
    return [_image_to_base64(image) for image in images]


async def _call_ollama(prompt: str, image_b64: str) -> dict[str, Any]:
    if not is_local_vision_ocr_configured():
        raise LocalVisionOCRError("local vision OCR is not configured")

    url = f"{_base_url()}/api/generate"
    payload = {
        "model": settings.LOCAL_VISION_OCR_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    timeout = httpx.Timeout(settings.LOCAL_VISION_OCR_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        raise LocalVisionOCRError(
            f"local vision OCR failed {response.status_code}: {response.text[:300]}"
        )

    body = response.json()
    text = body.get("response")
    if not isinstance(text, str) or not text.strip():
        raise LocalVisionOCRError("local vision OCR returned an empty response")
    return _response_to_json(text)


def _rows_from_payload(payload: dict[str, Any]) -> list[TimesheetRow]:
    raw_rows = payload.get("rows")
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

        for key in ("entrada", "saida"):
            normalized = normalize_time(str(raw.get(key) or ""))
            if normalized and normalized not in marcacoes:
                marcacoes.append(normalized)

        occ_value = str(raw.get("ocorrencia") or raw.get("occurrence") or "").strip()
        occ_raw, occ_tipo = normalize_ocorrencia(occ_value) if occ_value else (None, None)
        if occ_tipo == "trabalho_normal":
            occ_raw, occ_tipo = None, None

        rows.append(
            TimesheetRow(
                data=date_str,
                marcacoes=marcacoes,
                ocorrencia_raw=occ_raw,
                ocorrencia_tipo=occ_tipo,
            )
        )
    return rows


def _records_from_payload(payload: dict[str, Any]) -> list[dict]:
    raw_records = payload.get("records") or payload.get("rows")
    if not isinstance(raw_records, list):
        return []

    records: list[dict] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        date_str = normalize_date(str(raw.get("data") or raw.get("date") or ""))
        if not date_str:
            continue

        entrada = normalize_time(str(raw.get("entrada") or raw.get("entry") or ""))
        saida = normalize_time(str(raw.get("saida") or raw.get("exit") or ""))

        raw_times = raw.get("marcacoes") or raw.get("times") or []
        if isinstance(raw_times, list):
            normalized_times = [
                t for value in raw_times if (t := normalize_time(str(value or "")))
            ]
            if normalized_times:
                entrada = entrada or min(normalized_times)
                saida = saida or max(normalized_times)

        if entrada or saida:
            records.append({"data": date_str, "entrada": entrada, "saida": saida})
    return records


_TIMESHEET_PROMPT = """You are reading a Brazilian timesheet page image.
Extract only work-date rows and punch times that are visible on the page.
Return strict JSON only, with this shape:
{"rows":[{"data":"DD/MM/YYYY","marcacoes":["HH:MM","HH:MM"],"ocorrencia":null}]}
Rules:
- Use DD/MM/YYYY dates and HH:MM 24-hour times.
- Keep every punch time visible for a date, in reading order.
- If a date has an absence/holiday/vacation note instead of times, put it in ocorrencia.
- Do not invent missing dates or times. If nothing is readable, return {"rows":[]}.
"""


_GUIA_PROMPT = """You are reading a Guia Ministerial / Papeleta de Servico Externo page image.
Extract only records that have a visible service date and at least one visible time.
Return strict JSON only, with this shape:
{"records":[{"data":"DD/MM/YYYY","entrada":"HH:MM","saida":"HH:MM"}]}
Rules:
- Use DD/MM/YYYY dates and HH:MM 24-hour times.
- Prefer labels like entrada, inicio, saida, termino, chegada, partida when present.
- If labels are unclear, use the earliest visible time as entrada and the latest as saida.
- Do not invent missing dates or times. If nothing is readable, return {"records":[]}.
"""


async def extract_timesheet_rows_local_vision(pdf_bytes: bytes) -> list[TimesheetRow]:
    if not is_local_vision_ocr_configured():
        return []

    images = _render_pdf_page_images(pdf_bytes)
    rows: list[TimesheetRow] = []
    for page_index, image_b64 in enumerate(images, start=1):
        payload = await _call_ollama(_TIMESHEET_PROMPT, image_b64)
        page_rows = _rows_from_payload(payload)
        if page_rows:
            logger.info(
                "local vision OCR: page %d found %d timesheet row(s)",
                page_index,
                len(page_rows),
            )
        rows.extend(page_rows)
    return rows


async def extract_guia_records_local_vision(pdf_bytes: bytes) -> list[dict]:
    if not is_local_vision_ocr_configured():
        return []

    images = _render_pdf_page_images(pdf_bytes)
    records: list[dict] = []
    for page_index, image_b64 in enumerate(images, start=1):
        payload = await _call_ollama(_GUIA_PROMPT, image_b64)
        page_records = _records_from_payload(payload)
        if page_records:
            logger.info(
                "local vision OCR: page %d found %d guia record(s)",
                page_index,
                len(page_records),
            )
        records.extend(page_records)
    return records