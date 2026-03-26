from __future__ import annotations

import httpx

from config import settings
from models.timesheet import TimesheetRow
from services.gemini_service import normalize_text_with_gemini

MISTRAL_FILES_URL = "https://api.mistral.ai/v1/files"
MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"


class MistralExtractionError(Exception):
    pass


async def extract_with_mistral(pdf_bytes: bytes) -> list[TimesheetRow]:
    headers = {"Authorization": f"Bearer {settings.MISTRAL_API_KEY}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
        # Phase 1: upload PDF
        upload_response = await client.post(
            MISTRAL_FILES_URL,
            headers=headers,
            files={"file": ("timesheet.pdf", pdf_bytes, "application/pdf")},
            data={"purpose": "ocr"},
        )
        if upload_response.status_code != 200:
            raise MistralExtractionError(
                f"Mistral file upload failed {upload_response.status_code}: "
                f"{upload_response.text[:300]}"
            )
        file_id = upload_response.json()["id"]

        # Phase 2: OCR
        ocr_response = await client.post(
            MISTRAL_OCR_URL,
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": "mistral-ocr-latest",
                "document": {"type": "file", "file_id": file_id},
                "include_image_base64": False,
            },
        )
        if ocr_response.status_code != 200:
            raise MistralExtractionError(
                f"Mistral OCR failed {ocr_response.status_code}: "
                f"{ocr_response.text[:300]}"
            )

    pages = ocr_response.json().get("pages", [])
    full_markdown = "\n\n".join(p.get("markdown", "") for p in pages)

    # Phase 3: normalize via Gemini
    try:
        return await normalize_text_with_gemini(full_markdown)
    except Exception as e:
        raise MistralExtractionError(f"Gemini normalization after Mistral OCR failed: {e}") from e
