from __future__ import annotations

import base64
import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Iterable

import httpx
import pdfplumber

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)

FREQUENCY_PROMPT = """Voce esta extraindo linhas diarias de um RELATORIO DE ACOMPANHAMENTO DE FREQUENCIA da Petrobras.
Extraia APENAS as linhas da tabela diaria com Dia, Escala e Regime.

Retorne JSON no formato:
{"dias":[{"data":"DD/MM/YYYY","escala":"FOLG ou HS02","detalhes":"texto entre escala e Sobreaviso","linha":"linha original"}]}

Regras:
- Inclua todas as datas da tabela diaria.
- Preserve marcadores como 08:00, 12:00, 1082, 2025, 2040 e **** dentro de detalhes.
- Ignore secoes de rubricas, ajustes e resumos.
- Retorne somente JSON, sem markdown."""

DATE_ROW_RE = re.compile(
    r"^(?P<day>\d{2})/(?P<month>\d{2})\s+\S+\s+"
    r"(?P<scale>FOLG|HS\d+|HT\d+)\b(?P<details>.*)(?:Sobreaviso|Turno de \d+ Horas)$"
)
PERIOD_RE = re.compile(r"Per.odo\s+\d{2}/\d{2}/(?P<year>\d{4})")
TIME_RE = re.compile(r"\b\d{2}:\d{2}\b")

EMBARKED_START = "EMBARCADO - In\u00edcio do ciclo"
EMBARKED = "EMBARCADO"
WORK_ON_DAY_OFF = "TRABALHO NA FOLGA"
DAY_OFF = "FOLGA"


class FrequencyCycleExtractionError(Exception):
    pass


@dataclass(frozen=True)
class FrequencyDay:
    date: date
    scale: str
    details: str
    pdf_line: str
    page: int


@dataclass(frozen=True)
class ClassifiedDay:
    date: date
    cycle_day: int
    situation: str
    core_situation: str
    scale: str
    details: str
    pdf_line: str
    page: int
    expected_cycle_day: int | None = None
    expected_situation: str | None = None
    exact_match: bool | None = None
    core_match: bool | None = None


def parse_excel_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    return None


def normalize_label(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip().upper()
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def core_label(value: str | None) -> str:
    normalized = normalize_label(value)
    normalized = re.sub(r"\s+-\s+FIM DO CICLO$", "", normalized)
    normalized = re.sub(r"\s+-\s+INICIO DO CICLO$", " - INICIO DO CICLO", normalized)
    return normalized


def has_work_on_day_off_marker(details: str) -> bool:
    details = details.strip()
    if not details:
        return False

    # In the sample, 2025 appears on FOLG rows but remains FOLGA in the expected Excel.
    if re.search(r"\b2025\b", details) and not TIME_RE.search(details):
        return False

    if TIME_RE.search(details):
        return True

    # 1082 appears as a standalone marker on 23/02/2022 and the Excel classifies it
    # as TRABALHO NA FOLGA even without an explicit HH:MM marker on the daily row.
    if re.search(r"\b1082\b", details):
        return True

    return False


def extract_frequency_days_pdfplumber(pdf_bytes: bytes) -> list[FrequencyDay]:
    rows: list[FrequencyDay] = []
    current_year: int | None = None

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            finally:
                page.close()

            period_match = PERIOD_RE.search(text)
            if period_match:
                current_year = int(period_match.group("year"))

            for line in text.splitlines():
                match = DATE_ROW_RE.match(line.strip())
                if not match or current_year is None:
                    continue

                row_date = date(
                    current_year,
                    int(match.group("month")),
                    int(match.group("day")),
                )
                rows.append(
                    FrequencyDay(
                        date=row_date,
                        scale=match.group("scale"),
                        details=match.group("details").strip(),
                        pdf_line=line.strip(),
                        page=page_index,
                    )
                )

    return sorted(rows, key=lambda row: row.date)


def _base_situation(day: FrequencyDay) -> str:
    if day.scale != "FOLG":
        return EMBARKED
    if has_work_on_day_off_marker(day.details):
        return WORK_ON_DAY_OFF
    return DAY_OFF


def classify_frequency_days(days: Iterable[FrequencyDay]) -> list[ClassifiedDay]:
    ordered_days = sorted(days, key=lambda row: row.date)
    base_situations = [_base_situation(day) for day in ordered_days]
    classified: list[ClassifiedDay] = []
    cycle_day = 0
    previous_group: str | None = None

    for index, day in enumerate(ordered_days):
        base = base_situations[index]
        group = "embarked" if base == EMBARKED else "off"
        next_group = (
            "embarked"
            if index + 1 < len(base_situations) and base_situations[index + 1] == EMBARKED
            else "off"
        )

        if group != previous_group:
            cycle_day = 1
        else:
            cycle_day += 1

        situation = base
        if base == EMBARKED and group != previous_group:
            situation = EMBARKED_START
        elif base in {DAY_OFF, WORK_ON_DAY_OFF} and next_group == "embarked":
            situation = f"{base} - fim do ciclo"

        classified.append(
            ClassifiedDay(
                date=day.date,
                cycle_day=cycle_day,
                situation=situation,
                core_situation=base if base != EMBARKED else situation,
                scale=day.scale,
                details=day.details,
                pdf_line=day.pdf_line,
                page=day.page,
            )
        )
        previous_group = group

    return classified


def compare_with_expected(
    classified_days: Iterable[ClassifiedDay],
    expected: dict[date, tuple[int | None, str]],
) -> list[ClassifiedDay]:
    compared: list[ClassifiedDay] = []

    for day in classified_days:
        expected_row = expected.get(day.date)
        if expected_row is None:
            compared.append(day)
            continue

        expected_cycle_day, expected_situation = expected_row
        compared.append(
            ClassifiedDay(
                date=day.date,
                cycle_day=day.cycle_day,
                situation=day.situation,
                core_situation=day.core_situation,
                scale=day.scale,
                details=day.details,
                pdf_line=day.pdf_line,
                page=day.page,
                expected_cycle_day=expected_cycle_day,
                expected_situation=expected_situation,
                exact_match=(
                    day.cycle_day == expected_cycle_day
                    and normalize_label(day.situation) == normalize_label(expected_situation)
                ),
                core_match=(
                    day.cycle_day == expected_cycle_day
                    and core_label(day.core_situation) == core_label(expected_situation)
                ),
            )
        )

    return compared


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def extract_frequency_days_gemini(pdf_bytes: bytes) -> list[FrequencyDay]:
    from config import settings

    encoded = base64.b64encode(pdf_bytes).decode("utf-8")
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": encoded}},
                {"text": FREQUENCY_PROMPT},
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
            "Gemini frequency extraction error - status=%d body=%s",
            response.status_code,
            response.text[:300],
        )
        raise FrequencyCycleExtractionError(
            f"Gemini error {response.status_code}: {response.text[:300]}"
        )

    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(_clean_json(text))
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise FrequencyCycleExtractionError(f"Failed to parse Gemini response: {e}") from e

    rows: list[FrequencyDay] = []
    for index, item in enumerate(data.get("dias") or [], start=1):
        row_date = parse_excel_date(item.get("data"))
        scale = str(item.get("escala") or "").strip().upper()
        if row_date is None or not re.match(r"^(FOLG|HS\d+|HT\d+)$", scale):
            continue
        rows.append(
            FrequencyDay(
                date=row_date,
                scale=scale,
                details=str(item.get("detalhes") or "").strip(),
                pdf_line=str(item.get("linha") or "").strip(),
                page=int(item.get("pagina") or index),
            )
        )

    return sorted(rows, key=lambda row: row.date)


async def extract_and_classify_frequency_cycles(
    pdf_bytes: bytes,
) -> tuple[list[ClassifiedDay], str]:
    rows = extract_frequency_days_pdfplumber(pdf_bytes)
    provider = "pdfplumber"

    if not rows:
        provider = "gemini"
        rows = await extract_frequency_days_gemini(pdf_bytes)

    if not rows:
        raise FrequencyCycleExtractionError(
            "Nenhuma linha diaria de frequencia encontrada no PDF."
        )

    return classify_frequency_days(rows), provider


async def stream_frequency_cycle_extraction(
    pdf_bytes: bytes,
    original_stem: str,
):
    from services.frequency_cycle_excel_builder import build_frequency_cycle_excel

    try:
        yield "data: " + json.dumps({
            "type": "progress",
            "chunk": 0,
            "total": 1,
            "step": "pdfplumber",
            "message": "Analisando relatorio de frequencia com pdfplumber...",
        }) + "\n\n"

        task = asyncio.create_task(asyncio.to_thread(extract_frequency_days_pdfplumber, pdf_bytes))
        while not task.done():
            yield ": keep-alive\n\n"
            await asyncio.sleep(10)

        rows = task.result()
        provider = "pdfplumber"

        if not rows:
            provider = "gemini"
            yield "data: " + json.dumps({
                "type": "progress",
                "chunk": 1,
                "total": 1,
                "step": "gemini",
                "message": "pdfplumber nao encontrou linhas diarias. Tentando Gemini...",
            }) + "\n\n"

            gemini_task = asyncio.create_task(extract_frequency_days_gemini(pdf_bytes))
            while not gemini_task.done():
                yield ": keep-alive\n\n"
                await asyncio.sleep(10)
            rows = gemini_task.result()

        if not rows:
            yield "data: " + json.dumps({
                "type": "error",
                "message": "Nenhuma linha diaria de frequencia encontrada no PDF.",
            }) + "\n\n"
            return

        classified = classify_frequency_days(rows)
        excel_bytes = await asyncio.to_thread(
            build_frequency_cycle_excel,
            classified,
            provider,
        )

        yield "data: " + json.dumps({
            "type": "done",
            "excel_b64": base64.b64encode(excel_bytes).decode(),
            "excel_filename": f"frequencia_{original_stem}.xlsx",
            "rows_extracted": len(classified),
            "provider": provider,
            "pdf_type": "native" if provider == "pdfplumber" else "scanned",
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("frequency cycle stream: unexpected error: %s", e)
        yield "data: " + json.dumps({
            "type": "error",
            "message": "Erro interno ao processar relatorio de frequencia.",
        }) + "\n\n"
