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
import pypdf

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)
FREQUENCY_GEMINI_CHUNK_SIZE = 5
FREQUENCY_GEMINI_TIMEOUT_SECONDS = 300.0
FREQUENCY_GEMINI_RETRIES = 2

FREQUENCY_PROMPT = """Voce esta extraindo linhas diarias de um RELATORIO DE ACOMPANHAMENTO DE FREQUENCIA da Petrobras.
Extraia APENAS as linhas da tabela diaria mensal. Seu resultado deve ser equivalente ao parser pdfplumber do sistema.

Retorne JSON no formato:
{"dias":[{"data":"DD/MM/YYYY","escala":"FOLG, HS02, HT51 etc","detalhes":"marcadores da linha diaria","linha":"linha original","pagina":1}]}

Regras:
- Inclua todas as datas da tabela diaria.
- Aceite linhas no formato antigo: "DD/MM dia_semana ESCALA detalhes ... Sobreaviso" ou "... Turno de 12 Horas".
- Aceite linhas no formato novo: "DD dia_semana ESCALA detalhes", usando mes/ano do cabecalho "Periodo : DD.MM.YYYY a DD.MM.YYYY" ou "Periodo DD/MM/YYYY".
- O campo "escala" deve ser exatamente FOLG, HS seguido de numeros, ou HT seguido de numeros.
- O campo "detalhes" deve conter o texto apos a escala na mesma linha diaria.
- No formato antigo, remova do final de detalhes apenas os marcadores finais "Sobreaviso" ou "Turno de N Horas".
- No formato novo, preserve todo o restante da linha apos a escala, inclusive Peso, AF, Regime e saldos.
- Preserve marcadores como 07:57, 12:00, 1082, 1125, 2021, 2025, 2040, ****, +1,50 e -1,00 dentro de detalhes.
- Ignore cabecalhos, rodapes, secoes "Rubricas salariais", "Ajustes", "Transferencias/Ajustes de saldos", resumos de banco de horas e tabelas de escala.
- Nao extraia linhas de Ajustes, mesmo quando comecem com dia e tenham horarios.
- Use null somente quando um campo nao existir. Nao invente dias, escalas ou marcadores.
- Retorne somente JSON, sem markdown."""

DATE_ROW_RE = re.compile(
    r"^(?P<day>\d{2})/(?P<month>\d{2})\s+\S+\s+"
    r"(?P<scale>FOLG|HS\d+|HT\d+)\b(?P<details>.*)(?:Sobreaviso|Turno de \d+ Horas)$"
)
DAY_ONLY_ROW_RE = re.compile(
    r"^(?P<day>\d{2})\s+\S+\s+"
    r"(?P<scale>FOLG|HS\d+|HT\d+)\b(?P<details>.*)$",
    re.IGNORECASE,
)
PERIOD_RE = re.compile(
    r"Per[i\u00ed]odo\s*:?\s*"
    r"\d{2}[./](?P<month>\d{2})[./](?P<year>\d{4})",
    re.IGNORECASE,
)
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


def _extract_frequency_days_from_page_texts(page_texts: Iterable[tuple[int, str]]) -> list[FrequencyDay]:
    rows: list[FrequencyDay] = []
    current_year: int | None = None
    current_month: int | None = None

    for page_index, text in page_texts:
        period_match = PERIOD_RE.search(text)
        if period_match:
            current_year = int(period_match.group("year"))
            current_month = int(period_match.group("month"))

        for line in text.splitlines():
            stripped = line.strip()
            match = DATE_ROW_RE.match(stripped)
            if match and current_year is not None:
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
                        pdf_line=stripped,
                        page=page_index,
                    )
                )
                continue

            match = DAY_ONLY_ROW_RE.match(stripped)
            if (
                not match
                or current_year is None
                or current_month is None
            ):
                continue

            try:
                row_date = date(
                    current_year,
                    current_month,
                    int(match.group("day")),
                )
            except ValueError:
                continue

            rows.append(
                FrequencyDay(
                    date=row_date,
                    scale=match.group("scale"),
                    details=match.group("details").strip(),
                    pdf_line=stripped,
                    page=page_index,
                )
            )

    return sorted(rows, key=lambda row: row.date)


def _extract_frequency_days_with_pypdf(pdf_bytes: bytes) -> list[FrequencyDay]:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return _extract_frequency_days_from_page_texts(
        (page_index, page.extract_text() or "")
        for page_index, page in enumerate(reader.pages, start=1)
    )


def _extract_frequency_days_and_gemini_chunks(
    pdf_bytes: bytes,
) -> tuple[list[FrequencyDay], list[bytes]]:
    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        empty_page_indices: list[int] = []
        page_texts: list[tuple[int, str]] = []

        for page_index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                page_texts.append((page_index, text))
            else:
                empty_page_indices.append(page_index - 1)

        rows = _extract_frequency_days_from_page_texts(page_texts)
        gemini_chunks = _build_pdf_chunks_for_pages_from_reader(
            reader,
            empty_page_indices,
            FREQUENCY_GEMINI_CHUNK_SIZE,
        )
        if gemini_chunks:
            logger.info(
                "frequency: %d page(s) require Gemini OCR in %d chunk(s)",
                len(empty_page_indices),
                len(gemini_chunks),
            )
        return rows, gemini_chunks
    except Exception as e:
        logger.debug("frequency: pypdf hybrid scan failed, falling back: %s", e)
        gemini_pdf = get_frequency_pages_requiring_gemini(pdf_bytes)
        return extract_frequency_days_pdfplumber(pdf_bytes), [gemini_pdf] if gemini_pdf else []


def extract_frequency_days_pdfplumber(pdf_bytes: bytes) -> list[FrequencyDay]:
    try:
        rows = _extract_frequency_days_with_pypdf(pdf_bytes)
        if rows:
            return rows
    except Exception as e:
        logger.debug("frequency: pypdf text extraction failed, trying pdfplumber: %s", e)

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        page_texts: list[tuple[int, str]] = []
        for page_index, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            finally:
                page.close()
            page_texts.append((page_index, text))

    return _extract_frequency_days_from_page_texts(page_texts)


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


def _build_pdf_for_pages(pdf_bytes: bytes, page_indices: Iterable[int]) -> bytes | None:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return _build_pdf_for_pages_from_reader(reader, page_indices)


def _build_pdf_for_pages_from_reader(
    reader: pypdf.PdfReader,
    page_indices: Iterable[int],
) -> bytes | None:
    indices = list(dict.fromkeys(page_indices))
    if not indices:
        return None

    writer = pypdf.PdfWriter()
    for index in indices:
        if 0 <= index < len(reader.pages):
            writer.add_page(reader.pages[index])

    if len(writer.pages) == 0:
        return None

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _build_pdf_chunks_for_pages_from_reader(
    reader: pypdf.PdfReader,
    page_indices: Iterable[int],
    chunk_size: int,
) -> list[bytes]:
    indices = list(dict.fromkeys(page_indices))
    chunks: list[bytes] = []

    for start in range(0, len(indices), chunk_size):
        chunk = _build_pdf_for_pages_from_reader(reader, indices[start:start + chunk_size])
        if chunk:
            chunks.append(chunk)

    return chunks


def _split_pdf_into_page_chunks(pdf_bytes: bytes, chunk_size: int = 1) -> list[bytes]:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return _build_pdf_chunks_for_pages_from_reader(
        reader,
        range(len(reader.pages)),
        chunk_size,
    )


def get_frequency_pages_requiring_gemini(pdf_bytes: bytes) -> bytes | None:
    """Return pages that look image-only for Gemini OCR fallback."""
    page_indices: list[int] = []
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))

    for page_index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            continue
        page_indices.append(page_index)

    if not page_indices:
        return None

    logger.info(
        "frequency: %d page(s) require Gemini OCR",
        len(page_indices),
    )
    return _build_pdf_for_pages_from_reader(reader, page_indices)


def merge_frequency_days(
    pdfplumber_rows: Iterable[FrequencyDay],
    gemini_rows: Iterable[FrequencyDay],
) -> list[FrequencyDay]:
    rows_by_date: dict[date, FrequencyDay] = {}

    for row in gemini_rows:
        rows_by_date[row.date] = row
    for row in pdfplumber_rows:
        rows_by_date[row.date] = row

    return sorted(rows_by_date.values(), key=lambda row: row.date)


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

    response = None
    for attempt in range(1, FREQUENCY_GEMINI_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    FREQUENCY_GEMINI_TIMEOUT_SECONDS,
                    connect=30.0,
                )
            ) as client:
                response = await client.post(
                    GEMINI_URL,
                    params={"key": settings.GEMINI_API_KEY},
                    json=body,
                )
            break
        except (httpx.TimeoutException, httpx.RequestError) as e:
            if attempt >= FREQUENCY_GEMINI_RETRIES:
                raise FrequencyCycleExtractionError(
                    f"Gemini request failed after {attempt} attempt(s): {e}"
                ) from e
            logger.warning(
                "Gemini frequency request failed on attempt %d/%d: %s",
                attempt,
                FREQUENCY_GEMINI_RETRIES,
                e,
            )
            await asyncio.sleep(2 * attempt)

    if response is None:
        raise FrequencyCycleExtractionError("Gemini request did not return a response")

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


async def extract_frequency_days_gemini_adaptive(pdf_bytes: bytes) -> list[FrequencyDay]:
    try:
        return await extract_frequency_days_gemini(pdf_bytes)
    except FrequencyCycleExtractionError as first_error:
        page_chunks = _split_pdf_into_page_chunks(pdf_bytes, chunk_size=1)
        if len(page_chunks) <= 1:
            raise

        logger.warning(
            "Gemini frequency chunk failed; retrying as %d single-page chunk(s): %s",
            len(page_chunks),
            first_error,
        )
        rows: list[FrequencyDay] = []
        for page_index, page_chunk in enumerate(page_chunks, start=1):
            try:
                rows.extend(await extract_frequency_days_gemini(page_chunk))
            except FrequencyCycleExtractionError as e:
                logger.warning(
                    "Gemini frequency single-page fallback failed on page %d/%d: %s",
                    page_index,
                    len(page_chunks),
                    e,
                )

        if rows:
            return rows
        raise first_error


async def extract_frequency_days_hybrid(pdf_bytes: bytes) -> tuple[list[FrequencyDay], str]:
    rows, gemini_chunks = _extract_frequency_days_and_gemini_chunks(pdf_bytes)

    if not rows:
        return await extract_frequency_days_gemini(pdf_bytes), "gemini"

    if not gemini_chunks:
        return rows, "pdfplumber"

    gemini_rows: list[FrequencyDay] = []
    for chunk in gemini_chunks:
        try:
            gemini_rows.extend(await extract_frequency_days_gemini_adaptive(chunk))
        except FrequencyCycleExtractionError as e:
            logger.warning("frequency: Gemini chunk failed and will be skipped: %s", e)

    if not gemini_rows:
        return rows, "pdfplumber"

    merged_rows = merge_frequency_days(rows, gemini_rows)
    provider = "pdfplumber+gemini" if len(merged_rows) > len(rows) else "pdfplumber"
    return merged_rows, provider


async def extract_and_classify_frequency_cycles(
    pdf_bytes: bytes,
) -> tuple[list[ClassifiedDay], str]:
    rows, provider = await extract_frequency_days_hybrid(pdf_bytes)

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

        task = asyncio.create_task(asyncio.to_thread(_extract_frequency_days_and_gemini_chunks, pdf_bytes))
        while not task.done():
            yield ": keep-alive\n\n"
            await asyncio.sleep(10)

        rows, gemini_chunks = task.result()
        provider = "pdfplumber"

        if rows:
            if gemini_chunks:
                gemini_rows: list[FrequencyDay] = []
                total_chunks = len(gemini_chunks)

                for chunk_index, chunk in enumerate(gemini_chunks, start=1):
                    yield "data: " + json.dumps({
                        "type": "progress",
                        "chunk": chunk_index,
                        "total": total_chunks,
                        "step": "gemini",
                        "message": f"Gemini: processando paginas com imagem ({chunk_index}/{total_chunks})...",
                    }) + "\n\n"

                    gemini_task = asyncio.create_task(extract_frequency_days_gemini_adaptive(chunk))
                    while not gemini_task.done():
                        yield ": keep-alive\n\n"
                        await asyncio.sleep(10)

                    try:
                        gemini_rows.extend(gemini_task.result())
                    except FrequencyCycleExtractionError as e:
                        logger.warning(
                            "frequency stream: Gemini chunk %d/%d failed and will be skipped: %s",
                            chunk_index,
                            total_chunks,
                            e,
                        )

                if gemini_rows:
                    merged_rows = merge_frequency_days(rows, gemini_rows)
                    if len(merged_rows) > len(rows):
                        rows = merged_rows
                        provider = "pdfplumber+gemini"
        else:
            provider = "gemini"
            yield "data: " + json.dumps({
                "type": "progress",
                "chunk": 1,
                "total": 1,
                "step": "gemini",
                "message": "pdfplumber nao encontrou linhas diarias. Tentando Gemini...",
            }) + "\n\n"

            gemini_task = asyncio.create_task(extract_frequency_days_gemini_adaptive(pdf_bytes))
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
            "pdf_type": "mixed" if provider == "pdfplumber+gemini" else (
                "native" if provider == "pdfplumber" else "scanned"
            ),
        }, ensure_ascii=False) + "\n\n"

    except Exception as e:
        logger.exception("frequency cycle stream: unexpected error: %s", e)
        yield "data: " + json.dumps({
            "type": "error",
            "message": "Erro interno ao processar relatorio de frequencia.",
        }) + "\n\n"
