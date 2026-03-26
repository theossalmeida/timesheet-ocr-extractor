from __future__ import annotations
from typing import Literal
from pydantic import BaseModel

OcorrenciaTipo = Literal[
    "ferias",
    "feriado",
    "falta_justificada",
    "falta_injustificada",
    "licenca_medica",
    "afastamento",
    "folga",
    "dsr",
    "trabalho_normal",
    "meio_periodo",
    "outro",
]


class TimesheetRow(BaseModel):
    data: str | None = None
    entrada_1: str | None = None
    saida_1: str | None = None
    entrada_2: str | None = None
    saida_2: str | None = None
    ocorrencia_raw: str | None = None
    ocorrencia_tipo: OcorrenciaTipo | None = None
    worker_name: str | None = None


class ExtractionResult(BaseModel):
    rows: list[TimesheetRow]
    provider: Literal["pdfplumber", "gemini", "mistral", "pdfplumber+gemini", "pdfplumber+mistral", "gemini-guia"]
    pdf_type: Literal["native", "scanned", "mixed"]
    warnings: list[str] = []
    total_rows: int = 0

    def model_post_init(self, __context: object) -> None:
        if self.total_rows == 0:
            self.total_rows = len(self.rows)


class ExtractResponse(BaseModel):
    filename: str
    rows_extracted: int
    provider: Literal["pdfplumber", "gemini", "mistral"]
    pdf_type: Literal["native", "scanned", "mixed"]
    warnings: list[str] = []
    download_url: str | None = None
