"""Progress frames shared by every extraction pipeline.

Every mode reports the same server-side phases, so the UI can render one bar
without knowing which pipeline ran (uploading is earlier, and client-side):

    queued      the single processing slot is busy, this document has not begun
    detecting   choosing an approach - native text, local OCR, or a paid model
    processing  the real work, counted in finished units
    building    assembling the spreadsheet once extraction is done

`done`/`total` always count *finished* units, never positions, because chunks
run concurrently and complete out of order. A stage with nothing to count
reports a single unit, so the bar never has to special-case it.

`payload` is the frame as a dict, for the polled job endpoint; `progress` wraps
the same dict as an SSE line. Both carry `phase`, which is what the UI reads -
a frame without one cannot be placed on the bar at all.
"""
from __future__ import annotations

import json

QUEUED = "queued"
DETECTING = "detecting"
PROCESSING = "processing"
BUILDING = "building"

# One wording per phase, whatever the pipeline underneath. The stage names the
# pipeline actually went through (pdfplumber, tesseract, gemini) stay in `step`
# and in the logs: they mean nothing to the person watching the bar.
QUEUED_LABEL = "Aguardando processamento..."
DETECTING_LABEL = "Identificando melhor abordagem..."
PROCESSING_LABEL = "Processando arquivo..."
BUILDING_LABEL = "Montando planilha..."


def payload(
    phase: str,
    message: str,
    done: int = 0,
    total: int = 1,
    step: str | None = None,
) -> dict:
    """One progress frame as a dict. `step` names the stage, for logs only."""
    frame: dict = {
        "type": "progress",
        "phase": phase,
        "message": message,
        "chunk": done,
        "total": max(1, total),
    }
    if step:
        frame["step"] = step
    return frame


def progress(
    phase: str,
    message: str,
    done: int = 0,
    total: int = 1,
    step: str | None = None,
) -> str:
    """The same frame as an SSE line."""
    return "data: " + json.dumps(payload(phase, message, done, total, step), ensure_ascii=False) + "\n\n"


def queued_payload() -> dict:
    """For the polled endpoint, where a queued document has no live stream."""
    return payload(QUEUED, QUEUED_LABEL)


def queued(step: str | None = None) -> str:
    return progress(QUEUED, QUEUED_LABEL, 0, 1, step=step)


def detecting(step: str | None = None) -> str:
    return progress(DETECTING, DETECTING_LABEL, 0, 1, step=step)


def processing(done: int = 0, total: int = 1, step: str | None = None) -> str:
    return progress(PROCESSING, PROCESSING_LABEL, done, total, step=step)


def building(step: str | None = None) -> str:
    return progress(BUILDING, BUILDING_LABEL, 0, 1, step=step)


def keep_alive() -> str:
    """A comment frame: holds the connection open without moving the bar."""
    return ": keep-alive\n\n"
