from __future__ import annotations
import re
import unicodedata

OCORRENCIA_MAP: dict[str, str] = {
    # Férias
    "ferias": "ferias",
    "fer": "ferias",
    "fer.": "ferias",
    "gozo de ferias": "ferias",
    "gozo ferias": "ferias",
    # Feriado
    "feriado": "feriado",
    "fer. nacional": "feriado",
    "feriado nacional": "feriado",
    "feriado municipal": "feriado",
    "feriado estadual": "feriado",
    # Falta justificada
    "falta justificada": "falta_justificada",
    "falt. just.": "falta_justificada",
    "falt.just.": "falta_justificada",
    # Falta injustificada
    "falta": "falta_injustificada",
    "falta injustificada": "falta_injustificada",
    "falt.": "falta_injustificada",
    "falt. injust.": "falta_injustificada",
    # Licença médica
    "licenca medica": "licenca_medica",
    "lic.med.": "licenca_medica",
    "lic. med.": "licenca_medica",
    "atestado": "licenca_medica",
    "atestado medico": "licenca_medica",
    "licenca": "licenca_medica",
    # Afastamento
    "afastamento": "afastamento",
    "afast.": "afastamento",
    "afast": "afastamento",
    "afastado": "afastamento",
    # Folga
    "folga": "folga",
    "folga/dsr": "folga",
    "folga dsr": "folga",
    # DSR
    "dsr": "dsr",
    "d.s.r.": "dsr",
    "descanso semanal remunerado": "dsr",
    # Meio período
    "meio periodo": "meio_periodo",
    "meio-periodo": "meio_periodo",
    "1/2 periodo": "meio_periodo",
    "meio ponto": "meio_periodo",
    # Trabalho normal (códigos de presença)
    "prese": "trabalho_normal",
    "presente": "trabalho_normal",
    "normal": "trabalho_normal",
    # Folga extra
    "folga extra": "folga",
}


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


_PT_MONTHS: dict[str, int] = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def normalize_date(val: str) -> str | None:
    if not val or not val.strip():
        return None
    val = val.strip()
    # Numeric separators: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    match = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})$", val)
    if match:
        day, month, year = match.group(1), match.group(2), match.group(3)
        if len(year) == 2:
            year = "20" + year
        try:
            d, m, y = int(day), int(month), int(year)
        except ValueError:
            return None
        if not (1 <= d <= 31 and 1 <= m <= 12 and y > 2000):
            return None
        return f"{d:02d}/{m:02d}/{y}"
    # PT-BR month abbreviation: DD/mmm/YY or DD/mmm/YYYY (e.g. 23/jun/15)
    match = re.match(r"^(\d{1,2})[/\-\.]([a-záéíóú]{3})[/\-\.](\d{2,4})$", val, re.IGNORECASE)
    if match:
        day, month_str, year = match.group(1), match.group(2).lower(), match.group(3)
        m = _PT_MONTHS.get(month_str)
        if m is None:
            return None
        if len(year) == 2:
            year = "20" + year
        try:
            d, y = int(day), int(year)
        except ValueError:
            return None
        if not (1 <= d <= 31 and y > 2000):
            return None
        return f"{d:02d}/{m:02d}/{y}"
    return None


def normalize_time(val: str) -> str | None:
    if not val or not val.strip():
        return None
    val = val.strip()
    # Format HH:MM or H:MM or HH.MM or H.MM
    match = re.match(r"^(\d{1,2})[:\.](\d{2})$", val)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
        return None
    # Format HHMM (4 digits)
    match = re.match(r"^(\d{4})$", val)
    if match:
        h, m = int(val[:2]), int(val[2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
        return None
    return None


def normalize_ocorrencia(texto: str) -> tuple[str | None, str | None]:
    if not texto or not texto.strip():
        return None, None
    raw = texto.strip()
    key = _strip_accents(raw.lower()).strip()
    # Direct lookup
    if key in OCORRENCIA_MAP:
        return raw, OCORRENCIA_MAP[key]
    # Try stripping trailing punctuation variants
    key_stripped = key.rstrip(".")
    if key_stripped in OCORRENCIA_MAP:
        return raw, OCORRENCIA_MAP[key_stripped]
    # Partial match: check if any map key is contained in the text
    for map_key, tipo in OCORRENCIA_MAP.items():
        if map_key in key:
            return raw, tipo
    return raw, "outro"
