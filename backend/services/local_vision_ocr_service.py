from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings
from models.timesheet import TimesheetRow
from utils.http_client import ensure_async_client
from utils.normalizers import normalize_date, normalize_ocorrencia, normalize_time

logger = logging.getLogger(__name__)


class LocalVisionOCRError(Exception):
    pass


class LocalVisionConnectionError(LocalVisionOCRError):
    pass



_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class _CropTemplate:
    name: str
    box: tuple[float, float, float, float]


_SERVICE_CROP_TEMPLATES = [
    _CropTemplate("top_wide", (0.0, 0.0, 1.0, 0.55)),
    _CropTemplate("header_wide", (0.0, 0.0, 1.0, 0.38)),
    _CropTemplate("top_left", (0.0, 0.0, 0.65, 0.58)),
    _CropTemplate("top_center", (0.15, 0.0, 0.85, 0.58)),
    _CropTemplate("top_right", (0.35, 0.0, 1.0, 0.58)),
    _CropTemplate("body_wide", (0.0, 0.12, 1.0, 0.70)),
]
_SAMPLE_PAGES = 3


def is_local_vision_ocr_configured() -> bool:
    return bool(
        (settings.LOCAL_VISION_OCR_BASE_URL or "").strip()
        and (settings.LOCAL_VISION_OCR_MODEL or "").strip()
    )


def _base_url() -> str:
    return settings.LOCAL_VISION_OCR_BASE_URL.rstrip("/")

def _resolved_provider() -> str:
    provider = (settings.LOCAL_VISION_OCR_PROVIDER or "auto").strip().lower()
    if provider in {"lmstudio", "openai", "openai-compatible"}:
        return "lmstudio"
    if provider == "ollama":
        return "ollama"

    base_url = _base_url().lower()
    if base_url.endswith("/v1") or ":1234" in base_url:
        return "lmstudio"
    return "ollama"


def _lmstudio_base_url() -> str:
    base_url = _base_url()
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"


def _connection_error_message(provider: str, url: str) -> str:
    return (
        f"Nao foi possivel conectar ao OCR local ({provider}) em {url}. "
        "Confirme que o LM Studio Server esta iniciado no Mac, com acesso pela rede "
        "habilitado, porta 1234 liberada, e que LOCAL_VISION_OCR_BASE_URL aponta "
        "para o IP correto."
    )


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
            preview = re.sub(r"\s+", " ", text)[:300]
            raise LocalVisionOCRError(
                f"local vision model did not return JSON: {preview}"
            )
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise LocalVisionOCRError("local vision model returned non-object JSON")
    return parsed


def _image_to_base64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _model_text_to_payload(text: str) -> dict[str, Any]:
    try:
        return _response_to_json(text)
    except LocalVisionOCRError as e:
        if not str(e).startswith("local vision model did not return JSON:"):
            raise
        preview = re.sub(r"\s+", " ", text.strip())[:300]
        logger.info(
            "local vision OCR: model returned non-JSON text; trying text parsers: %s",
            preview,
        )
        return {"_raw_text": text}


def _timesheet_rows_from_text(text: str) -> list[TimesheetRow]:
    from services.pdfplumber_service import (
        _MULTIROW_DATE_RE,
        _parse_multirow_cell,
        _parse_peg_larg_rows,
        _parse_text_rows,
        _parse_weekday_first_rows,
    )

    rows = (
        _parse_peg_larg_rows(text)
        or _parse_text_rows(text)
        or _parse_weekday_first_rows(text)
    )
    if rows:
        return rows

    multirow_rows: list[TimesheetRow] = []
    for line in text.split("\n"):
        if _MULTIROW_DATE_RE.search(line):
            multirow_rows.extend(_parse_multirow_cell(line))
    return multirow_rows


def _payload_shape_summary(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("rows", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.append(f"{key}={len(value)}")
    if "_raw_text" in payload:
        raw_text = payload.get("_raw_text")
        text_len = len(raw_text) if isinstance(raw_text, str) else 0
        parts.append(f"raw_text_len={text_len}")
    extra_keys = sorted(k for k in payload.keys() if k not in {"rows", "records", "_raw_text"})
    if extra_keys:
        parts.append(f"extra_keys={extra_keys}")
    return ", ".join(parts) if parts else "empty"

def _preprocess_image_for_vision(image):
    try:
        import cv2
        import numpy as np
        from PIL import Image

        arr = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

        coords = np.column_stack(np.where(gray < 245))
        if coords.size:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            if 0.2 <= abs(angle) <= 8:
                height, width = gray.shape[:2]
                matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
                gray = cv2.warpAffine(
                    gray,
                    matrix,
                    (width, height),
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_REPLICATE,
                )

        return Image.fromarray(gray).convert("RGB")
    except Exception as e:
        logger.debug("local vision OCR: OpenCV preprocessing unavailable: %s", e)
        from PIL import ImageEnhance, ImageFilter

        return (
            ImageEnhance.Contrast(image.convert("RGB"))
            .enhance(1.35)
            .filter(ImageFilter.SHARPEN)
        )

def _render_crop_page_images(pdf_bytes: bytes, page_indices=None):
    from services.tesseract_ocr_service import _render_pdf_pages

    return [
        _preprocess_image_for_vision(image)
        for image in _render_pdf_pages(
            pdf_bytes,
            dpi=max(settings.LOCAL_VISION_OCR_DPI, 300),
            page_indices=page_indices,
        )
    ]


def _page_count(pdf_bytes: bytes) -> int:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return len(doc)
    finally:
        doc.close()


def _crop_image(image, template: _CropTemplate):
    width, height = image.size
    x1, y1, x2, y2 = template.box
    return image.crop((
        int(width * x1),
        int(height * y1),
        int(width * x2),
        int(height * y2),
    ))


async def _call_ollama(
    prompt: str, image_b64: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    if not is_local_vision_ocr_configured():
        raise LocalVisionOCRError("local vision OCR is not configured")

    url = f"{_base_url()}/api/generate"
    payload = {
        "model": settings.LOCAL_VISION_OCR_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": settings.LOCAL_VISION_OCR_NUM_CTX},
    }

    timeout = httpx.Timeout(settings.LOCAL_VISION_OCR_TIMEOUT_SECONDS)
    try:
        async with ensure_async_client(client, timeout) as active_client:
            response = await active_client.post(url, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise LocalVisionConnectionError(
            _connection_error_message("Ollama", url)
        ) from e

    if response.status_code != 200:
        raise LocalVisionOCRError(
            f"local vision OCR failed {response.status_code}: {response.text[:300]}"
        )

    body = response.json()
    text = body.get("response")
    if not isinstance(text, str) or not text.strip():
        raise LocalVisionOCRError("local vision OCR returned an empty response")
    return _model_text_to_payload(text)

async def _call_lmstudio(
    prompt: str, image_b64: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    if not is_local_vision_ocr_configured():
        raise LocalVisionOCRError("local vision OCR is not configured")

    url = f"{_lmstudio_base_url()}/chat/completions"
    text_prompt = (
        "Return strict JSON only. Do not add markdown or commentary.\n\n"
        f"{prompt}"
    )
    payload = {
        "model": settings.LOCAL_VISION_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "local_vision_ocr_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        "records": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
    }

    timeout = httpx.Timeout(settings.LOCAL_VISION_OCR_TIMEOUT_SECONDS)
    try:
        async with ensure_async_client(client, timeout) as active_client:
            response = await active_client.post(url, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise LocalVisionConnectionError(
            _connection_error_message("LM Studio", url)
        ) from e

    if response.status_code != 200:
        raise LocalVisionOCRError(
            f"LM Studio vision OCR failed {response.status_code}: {response.text[:300]}"
        )

    body = response.json()
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LocalVisionOCRError("LM Studio vision OCR returned no choices")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        text = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    else:
        text = content

    if not isinstance(text, str) or not text.strip():
        raise LocalVisionOCRError("LM Studio vision OCR returned an empty response")
    return _model_text_to_payload(text)


async def _call_vision_model(
    prompt: str, image_b64: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    provider = _resolved_provider()
    if provider == "lmstudio":
        return await _call_lmstudio(prompt, image_b64, client=client)
    return await _call_ollama(prompt, image_b64, client=client)

def _rows_from_payload(payload: dict[str, Any]) -> list[TimesheetRow]:
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raw_records = payload.get("records")
        if isinstance(raw_records, list) and raw_records:
            return _guia_records_to_rows(
                _records_from_payload(payload, include_meta=True)
            )

        raw_text = payload.get("_raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            return _timesheet_rows_from_text(raw_text)
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

        confidence = _confidence_label(raw)
        rows.append(
            TimesheetRow(
                data=date_str,
                marcacoes=marcacoes,
                ocorrencia_raw=occ_raw,
                ocorrencia_tipo=occ_tipo,
                ocr_confidence=confidence,
                ocr_warning=_ocr_warning_for_confidence(confidence),
            )
        )
    return rows


def _field_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return str(value).strip().lower()
    return ""

def _confidence_label(raw: dict[str, Any]) -> str | None:
    confidence = _field_text(raw, "confidence", "confianca")
    if confidence in {"high", "alta"}:
        return "high"
    if confidence in {"medium", "media", "mediana"}:
        return "medium"
    if confidence in {"low", "baixa", "none", "nenhuma"}:
        return "low"
    return None


def _ocr_warning_for_confidence(confidence: str | None) -> str | None:
    if confidence == "low":
        return "Baixa confianca OCR: conferir esta linha no PDF original."
    return None

def _confidence_score(raw: dict[str, Any]) -> int:
    confidence = _confidence_label(raw)
    if confidence == "high":
        return 3
    if confidence == "medium":
        return 2
    if confidence == "low":
        return 0
    return 1


def _looks_like_service_form_evidence(raw: dict[str, Any]) -> bool:
    data_source = _field_text(raw, "data_source", "date_source", "data_label", "date_label")
    entrada_source = _field_text(
        raw,
        "entrada_source",
        "entry_source",
        "start_source",
        "entrada_label",
        "entry_label",
    )
    saida_source = _field_text(
        raw,
        "saida_source",
        "exit_source",
        "end_source",
        "saida_label",
        "exit_label",
    )

    data_ok = "data" in data_source or "date" in data_source
    # "escala"/"garagem" cover the Papeleta de Servico Externo header
    # "HORA ESCALA / GARAGEM - PONTO", which names neither entrada nor inicio.
    entrada_ok = any(
        token in entrada_source
        for token in ("inicio", "entrada", "pegada", "escala", "garagem")
    )
    saida_ok = any(
        token in saida_source
        for token in ("termino", "saida", "largada", "chegada")
    )
    return data_ok and entrada_ok and saida_ok


def _records_from_payload(
    payload: dict[str, Any],
    *,
    require_service_evidence: bool = False,
    include_meta: bool = False,
) -> list[dict]:
    raw_records = payload.get("records") or payload.get("rows")
    if not isinstance(raw_records, list):
        return []

    records: list[dict] = []
    seen_dates: set[str] = set()
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        if require_service_evidence and not _looks_like_service_form_evidence(raw):
            continue

        date_str = normalize_date(str(raw.get("data") or raw.get("date") or ""))
        if not date_str or date_str in seen_dates:
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
            confidence = _confidence_label(raw)
            record = {"data": date_str, "entrada": entrada, "saida": saida}
            if include_meta:
                record["_confidence_score"] = _confidence_score(raw)
                record["_confidence"] = confidence
                warning = _ocr_warning_for_confidence(confidence)
                if warning:
                    record["_ocr_warning"] = warning
            records.append(record)
            seen_dates.add(date_str)
    return records


_TIMESHEET_PROMPT = """You are reading one page of a Brazilian work-time document.
Return strict JSON only, with this shape:
{"rows":[{"data":"DD/MM/YYYY","marcacoes":["HH:MM","HH:MM"],"ocorrencia":null,"confidence":"high|medium|low"}]}
Rules:
- Extract only values visibly anchored to the work-time table or form fields on this page.
- For a normal cartao de ponto table, output one row per visible work date. Keep the punch times visible on that same row/date, in reading order.
- For a single-service form (Guia Ministerial, Papeleta de Servico Externo, ordem de servico, or similar), output at most one row for the page: use the visible DATA field as data, and the visible work-start/work-end fields as marcacoes.
- Work-start labels may appear as INICIO/TRABALHO, INICIO, ENTRADA, HORA INICIO, PEGADA, or SAIDA GARAGEM. Work-end labels may appear as TERMINO/TRABALHO, TERMINO, SAIDA, HORA TERMINO, LARGADA, or CHEGADA GARAGEM.
- Ignore dates/times from signatures, electronic validation text, printed protocol text, QR codes, page numbers, addresses, phone numbers, totals, intervals, and footer/header metadata.
- Do not infer sequential dates. Do not duplicate a single-service page into multiple days.
- If the relevant date or work times are not readable, return {"rows":[]}.
"""


_SERVICE_FORM_PROMPT = """You are reading a candidate crop from one Brazilian single-service work document page.
Return strict JSON only, with this shape:
{"records":[{"data":"DD/MM/YYYY","entrada":"HH:MM","saida":"HH:MM","data_source":"DATA field","entrada_source":"INICIO/TRABALHO field","saida_source":"TERMINO/TRABALHO field","confidence":"high|medium|low"}]}
Rules:
- Use this prompt only for single-service forms such as Guia Ministerial, Papeleta de Servico Externo, ordem de servico, viagem/linha service sheet, or similar. If this crop is not from a single-service form, return {"records":[]}.
- Output at most one record for the page.
- The service date must come from the visible field labeled DATA or an equivalent service-date field near the form header/body. Dates may be written as DD/MM/YY; always expand YY to 20YY (22 means 2022, 21 means 2021, 19 means 2019). Do not reinterpret YY as a day or month.
- entrada must come from the visible work-start field/column, such as INICIO/TRABALHO, INICIO, ENTRADA, HORA INICIO, PEGADA, or SAIDA GARAGEM.
- saida must come from the visible work-end field/column, such as TERMINO/TRABALHO, TERMINO, SAIDA, HORA TERMINO, LARGADA, or CHEGADA GARAGEM.
- If INICIO and TRABALHO are stacked on two header rows, treat the value directly below that combined header as entrada. If TERMINO and TRABALHO are stacked, treat the value directly below that combined header as saida.
- Fill data_source, entrada_source, and saida_source with the exact label/anchor that justifies each extracted value. If any value is not anchored to one of those fields, return {"records":[]}.
- Ignore all dates/times from signatures, electronic validation text, printed protocol text, QR codes, page numbers, company addresses, phone numbers, totals, intervals, and footer/header metadata.
- Do not use earliest/latest time unless the times are visibly inside the service-start/service-end fields. If the service date, entrada, or saida is unreadable, return {"records":[]}.
"""


def _guia_records_to_rows(records: list[dict]) -> list[TimesheetRow]:
    rows: list[TimesheetRow] = []
    for record in records:
        date_str = normalize_date(str(record.get("data") or ""))
        if not date_str:
            continue
        marcacoes = [
            time
            for value in (record.get("entrada"), record.get("saida"))
            if (time := normalize_time(str(value or "")))
        ]
        if marcacoes:
            rows.append(TimesheetRow(
                data=date_str,
                marcacoes=marcacoes,
                ocr_confidence=record.get("_confidence"),
                ocr_warning=record.get("_ocr_warning"),
            ))
    return rows


def _record_key(record: dict) -> tuple[str, str, str] | None:
    date_str = normalize_date(str(record.get("data") or ""))
    entrada = normalize_time(str(record.get("entrada") or ""))
    saida = normalize_time(str(record.get("saida") or ""))
    if not date_str or not entrada or not saida:
        return None
    return (date_str, entrada, saida)


def _consensus_service_records(candidates: list[dict]) -> tuple[list[dict], bool]:
    if not candidates:
        return [], False

    grouped: dict[tuple[str, str, str], int] = {}
    first_by_key: dict[tuple[str, str, str], dict] = {}
    for record in candidates:
        key = _record_key(record)
        if key is None:
            continue
        grouped[key] = grouped.get(key, 0) + 1
        first_by_key.setdefault(key, record)

    for key, count in grouped.items():
        if count >= 2:
            return [first_by_key[key]], True
    return [], True


def _best_template_name(
    template_scores: dict[str, dict[str, int]],
    template_keys: dict[str, list] | None = None,
) -> str | None:
    viable = [
        (name, score["pages"], score["confidence"])
        for name, score in template_scores.items()
        if score["pages"] > 0
    ]
    if not viable:
        return None

    viable.sort(key=lambda item: (item[1], item[2]), reverse=True)
    best = viable[0]
    tied = [item for item in viable if (item[1], item[2]) == (best[1], best[2])]
    if len(tied) == 1:
        return best[0]

    # Crops that tie because they read the same values are agreeing, not
    # ambiguous. Only a genuine disagreement is unresolvable. This matters most
    # on a one-page guia, where a single sampled page ties nearly every crop.
    if template_keys:
        agreed = {tuple(template_keys.get(name) or ()) for name, _, _ in tied}
        if len(agreed) == 1 and agreed != {()}:
            order = [template.name for template in _SERVICE_CROP_TEMPLATES]
            return min((name for name, _, _ in tied), key=order.index)
    return None


async def _select_service_crop_template(
    crop_images,
    sample_count: int | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[_CropTemplate | None, bool]:
    sample_count = min(sample_count or _SAMPLE_PAGES, len(crop_images))
    if sample_count == 0:
        return None, False

    scores = {
        template.name: {"pages": 0, "confidence": 0}
        for template in _SERVICE_CROP_TEMPLATES
    }
    attempted = False
    keys_by_template: dict[str, list] = {name: [] for name in scores}
    for page_image in crop_images[:sample_count]:
        page_keys: dict[str, tuple[str, str, str]] = {}
        for template in _SERVICE_CROP_TEMPLATES:
            crop_b64 = _image_to_base64(_crop_image(page_image, template))
            payload = await _call_vision_model(_SERVICE_FORM_PROMPT, crop_b64, client=client)
            records = _records_from_payload(
                payload,
                require_service_evidence=True,
                include_meta=True,
            )
            if not records:
                continue
            attempted = True
            record = records[0]
            key = _record_key(record)
            if key is None:
                continue
            page_keys[template.name] = key
            keys_by_template[template.name].append(key)
            scores[template.name]["pages"] += 1
            scores[template.name]["confidence"] += int(record.get("_confidence_score") or 1)

        if len(set(page_keys.values())) > 1:
            logger.info(
                "local vision OCR: service-form sample page had disagreeing crop candidates: %s",
                page_keys,
            )

    best_name = _best_template_name(scores, keys_by_template)
    if best_name is None:
        return None, attempted

    best = next(t for t in _SERVICE_CROP_TEMPLATES if t.name == best_name)
    logger.info("local vision OCR: selected service-form crop template %s", best.name)
    return best, True


async def _service_form_record_from_template(
    page_image, template: _CropTemplate, *, client: httpx.AsyncClient | None = None
) -> tuple[list[dict], bool]:
    crop_b64 = _image_to_base64(_crop_image(page_image, template))
    payload = await _call_vision_model(_SERVICE_FORM_PROMPT, crop_b64, client=client)
    records = _records_from_payload(payload, require_service_evidence=True, include_meta=True)
    return records[:1], bool(records)


def _resize_to_dpi(image, from_dpi: int, to_dpi: int):
    # Downscales an already-preprocessed image. Only safe to reuse in place of a
    # native render when from_dpi == to_dpi (a no-op); otherwise this LANCZOS
    # downscale is not equivalent to rendering+preprocessing natively at to_dpi.
    if from_dpi == to_dpi:
        return image
    from PIL import Image

    scale = to_dpi / from_dpi
    new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(new_size, Image.LANCZOS)


def _render_full_page_images(pdf_bytes: bytes):
    from services.tesseract_ocr_service import _render_pdf_pages

    return [
        _preprocess_image_for_vision(image)
        for image in _render_pdf_pages(pdf_bytes, dpi=settings.LOCAL_VISION_OCR_DPI)
    ]


async def extract_timesheet_rows_local_vision(pdf_bytes: bytes) -> list[TimesheetRow]:
    if not is_local_vision_ocr_configured():
        return []

    crop_images = _render_crop_page_images(pdf_bytes)

    timeout = httpx.Timeout(settings.LOCAL_VISION_OCR_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        template, service_attempted = await _select_service_crop_template(
            crop_images, client=client
        )

        rows: list[TimesheetRow] = []
        if template is not None:
            for page_index, page_image in enumerate(crop_images, start=1):
                service_records, _ = await _service_form_record_from_template(
                    page_image, template, client=client
                )
                guia_rows = _guia_records_to_rows(service_records)
                if guia_rows:
                    logger.info(
                        "local vision OCR: page %d matched service-form template %s with %d row(s)",
                        page_index,
                        template.name,
                        len(guia_rows),
                    )
                    rows.extend(guia_rows)
                else:
                    logger.info(
                        "local vision OCR: page %d had no service-form row with template %s",
                        page_index,
                        template.name,
                    )
            if rows:
                return rows
            logger.info(
                "local vision OCR: selected service-form template yielded no rows; "
                "trying full page prompt"
            )
        elif service_attempted:
            logger.info(
                "local vision OCR: service-form crops were seen but no stable template "
                "was selected; trying full page prompt"
            )

        # crop_images are rendered at max(DPI, 300). When DPI >= 300 that's
        # already the resolution the full-page prompt needs, so reuse them
        # (resize is a no-op) instead of re-rendering the whole PDF a second
        # time. When DPI < 300, downscaling the 300dpi-preprocessed crops would
        # not match a native render+preprocess at the lower DPI, so render
        # natively instead to avoid drifting OCR results for that config.
        if settings.LOCAL_VISION_OCR_DPI >= 300:
            full_images = [
                _resize_to_dpi(image, max(settings.LOCAL_VISION_OCR_DPI, 300), settings.LOCAL_VISION_OCR_DPI)
                for image in crop_images
            ]
        else:
            full_images = _render_full_page_images(pdf_bytes)
        for page_index, image in enumerate(full_images, start=1):
            payload = await _call_vision_model(_TIMESHEET_PROMPT, _image_to_base64(image), client=client)
            page_rows = _rows_from_payload(payload)
            if not page_rows:
                logger.info(
                    "local vision OCR: page %d full-page payload yielded no rows (%s)",
                    page_index,
                    _payload_shape_summary(payload),
                )
            if page_rows:
                logger.info(
                    "local vision OCR: page %d found %d timesheet row(s)",
                    page_index,
                    len(page_rows),
                )
            rows.extend(page_rows)
        return rows


GUIA_PROBE_PAGES = 2
GUIA_CONFIDENCE_THRESHOLD = 0.9


@dataclass
class GuiaPageOutcome:
    page_number: int
    record: dict | None
    confidence: float


@dataclass
class GuiaLocalRun:
    """Result of reading a guia locally, and whether it was trustworthy."""

    confidence: float
    passed: bool
    outcomes: list[GuiaPageOutcome]
    reason: str | None = None


def _page_confidence(record: dict | None) -> float:
    """Score one page from the label the model reported for it.

    The model grades itself coarsely (high/medium/low), so a page is worth
    1.0, 2/3, or 0, and 1/3 when it declined to say. A record missing the
    date, entrada or saida scores 0 whatever the label claims: a confidently
    half-read form is still unusable.
    """
    if not record:
        return 0.0
    if not (record.get("data") and record.get("entrada") and record.get("saida")):
        return 0.0
    score = record.get("_confidence_score")
    # `low` scores 0, which must not be mistaken for an absent grade.
    return (1 if score is None else int(score)) / 3


async def _guia_page_outcome(
    page_image, template, page_number: int, *, client: httpx.AsyncClient | None = None
) -> GuiaPageOutcome:
    records, _ = await _service_form_record_from_template(page_image, template, client=client)
    record = records[0] if records else None
    return GuiaPageOutcome(page_number, record, _page_confidence(record))


async def run_guia_local(
    pdf_bytes: bytes,
    *,
    probe_pages: int = GUIA_PROBE_PAGES,
    threshold: float = GUIA_CONFIDENCE_THRESHOLD,
) -> GuiaLocalRun:
    """Read a guia locally, but only after a short probe proves it readable.

    Guias are handwritten forms whose only machine-readable text is often the
    electronic signature footer, so there is no safe text-scraping path: a
    vision model has to read them. This reads the first few pages, and leaves
    it to the caller to send the document to a paid model when that probe is
    not convincing enough.
    """
    if not is_local_vision_ocr_configured():
        return GuiaLocalRun(0.0, False, [], "local vision OCR is not configured")

    total = _page_count(pdf_bytes)
    if total == 0:
        return GuiaLocalRun(0.0, False, [], "document has no pages")

    probe_count = max(1, min(probe_pages, total))
    probe_images = _render_crop_page_images(pdf_bytes, range(probe_count))

    timeout = httpx.Timeout(settings.LOCAL_VISION_OCR_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        template, attempted = await _select_service_crop_template(
            probe_images, sample_count=probe_count, client=client
        )
        if template is None:
            return GuiaLocalRun(
                0.0, False, [],
                "no stable service-form crop template"
                if attempted else "no service-form fields found",
            )

        outcomes = [
            await _guia_page_outcome(image, template, number, client=client)
            for number, image in enumerate(probe_images, start=1)
        ]
        confidence = sum(o.confidence for o in outcomes) / len(outcomes)
        logger.info(
            "local vision OCR: guia probe read %d page(s) at %.0f%% confidence",
            len(outcomes), confidence * 100,
        )
        if confidence <= threshold:
            return GuiaLocalRun(
                confidence, False, [], f"probe confidence {confidence:.0%}"
            )

        # The probe pages are already read; only the remainder still needs a call.
        if total > probe_count:
            rest = _render_crop_page_images(pdf_bytes, range(probe_count, total))
            for number, image in enumerate(rest, start=probe_count + 1):
                outcomes.append(await _guia_page_outcome(image, template, number, client=client))

        return GuiaLocalRun(confidence, True, outcomes)
