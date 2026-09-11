from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))

import httpx

from config import settings
from services.ai_pricing import price_calls
from services.fx import usd_brl
from services.gemini_service import EXTRACTION_PROMPT


MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = PROJECT_ROOT / "504.pdf"
OUTPUT_DIR = PROJECT_ROOT / "comparison_output"


def request_model(model: str, pdf_bytes: bytes) -> dict:
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
                {"text": EXTRACTION_PROMPT},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
            "maxOutputTokens": 8192,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = httpx.post(url, params={"key": settings.GEMINI_API_KEY}, json=body, timeout=180)
    response.raise_for_status()
    return response.json()


def main() -> None:
    if not settings.GEMINI_API_KEY.strip():
        raise SystemExit("GEMINI_API_KEY não está configurada.")
    if not PDF_PATH.is_file():
        raise SystemExit(f"PDF não encontrado: {PDF_PATH}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_bytes = PDF_PATH.read_bytes()
    rate = usd_brl()
    results = []

    for model in MODELS:
        output_path = OUTPUT_DIR / f"{model}.json"
        try:
            raw = request_model(model, pdf_bytes)
            output_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            usage = raw.get("usageMetadata", {})
            call = {
                "provider": "gemini",
                "model": model,
                "metered": True,
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "cached_tokens": usage.get("cachedContentTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
            }
            priced = price_calls([call], rate=rate)[0]
            results.append({"model": model, "tokens": usage.get("totalTokenCount", 0), "cost_brl": priced["cost_brl"], "output": str(output_path)})
        except Exception as error:
            results.append({"model": model, "error": f"{type(error).__name__}: {error}", "output": str(output_path)})

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps({"usd_brl_rate": rate, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("modelo\ttokens\tcusto_brl")
    for result in results:
        print(f"{result['model']}\t{result.get('tokens', '-') }\t{result.get('cost_brl', result.get('error', '-'))}")
    print(f"\nResumo: {summary_path}")


if __name__ == "__main__":
    main()
