import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pypdf
import pytest

from services import ai_pricing, ai_usage, fx
from services.claude_service import (
    ClaudeExtractionError,
    extract_with_claude,
    extract_with_claude_adaptive,
)

MODEL = "claude-haiku-5-5"
# Published rates for the model above, per token (litellm's price map).
INPUT_USD = 1e-07
OUTPUT_USD = 5e-07
CACHED_USD = 1e-08


@pytest.fixture(autouse=True)
def configure_claude(monkeypatch):
    from services import claude_service

    monkeypatch.setattr(claude_service.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(claude_service.settings, "ANTHROPIC_MODEL", MODEL)


@pytest.fixture(autouse=True)
def fixed_exchange_rate(monkeypatch):
    monkeypatch.setattr(fx, "usd_brl", lambda: 5.0)


def _usage(input_tokens=1000, output=400, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _response(rows=None, usage=None, text=None):
    body = text if text is not None else json.dumps(rows if rows is not None else [])
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)],
        stop_reason="end_turn",
        usage=usage,
    )


def _client(*responses):
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _pdf(pages):
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _patched(client):
    return patch("services.claude_service._new_client", return_value=client)


def test_usage_counts_cached_and_cache_written_tokens_as_prompt():
    usage = ai_usage.usage_from_claude(_usage(input_tokens=100, output=70, cache_read=800, cache_write=50))

    assert usage["prompt_tokens"] == 950
    assert usage["cached_tokens"] == 800
    assert usage["output_tokens"] == 70
    assert usage["total_tokens"] == 1020


def test_response_without_usage_is_unmetered():
    assert ai_usage.usage_from_claude(None) is None

    with ai_usage.recording() as calls:
        ai_usage.record("claude", MODEL, "extract", None)

    assert calls[0]["metered"] is False
    assert ai_pricing.price_calls(calls)[0]["cost_usd"] is None


def test_records_one_call_per_chunk_and_page_fallback():
    """A 3-page PDF is two chunk calls; a failed chunk retried page by page adds more."""
    responses = [
        _response(rows=[{"data": "01/03/2024", "marcacoes": ["08:00", "17:00"]}], usage=_usage(1200, 300)),
        _response(text="not json {{{", usage=_usage(800, 50)),
        _response(rows=[], usage=_usage(800, 20)),
    ]

    with _patched(_client(*responses)):
        with ai_usage.recording() as calls:
            rows = asyncio.run(extract_with_claude_adaptive(_pdf(3)))

    assert len(rows) == 1
    # chunk(2 pages) + failed chunk(page 3) + its single-page retry: Anthropic
    # billed all three, including the response we could not parse.
    assert [call["kind"] for call in calls] == ["extract", "extract", "extract"]
    assert sorted(call["prompt_tokens"] for call in calls) == [800, 800, 1200]
    assert all(call["metered"] for call in calls)


def test_failed_extraction_still_records_usage():
    with _patched(_client(_response(text="garbage", usage=_usage()))):
        with ai_usage.recording() as calls:
            with pytest.raises(ClaudeExtractionError):
                asyncio.run(extract_with_claude(b"fake pdf"))

    assert len(calls) == 1
    assert calls[0]["metered"] is True


@pytest.mark.parametrize("make_error, expected_calls", [
    (lambda request: anthropic.APITimeoutError(request=request), 1),
    (lambda request: anthropic.APIConnectionError(request=request), 0),
])
def test_transport_failure_records_uncertain_requests(make_error, expected_calls):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = _client()
    client.messages.create = AsyncMock(side_effect=make_error(request))

    with _patched(client):
        with ai_usage.recording() as calls:
            with pytest.raises(ClaudeExtractionError):
                asyncio.run(extract_with_claude(b"fake pdf"))

    assert len(calls) == expected_calls
    assert all(not call["metered"] for call in calls)


def test_recording_is_scoped_to_the_block():
    with ai_usage.recording() as calls:
        ai_usage.record("claude", MODEL, "extract", ai_usage.usage_from_claude(_usage()))
    ai_usage.record("claude", MODEL, "extract", ai_usage.usage_from_claude(_usage()))

    assert len(calls) == 1


def test_price_follows_published_per_token_rates():
    with ai_usage.recording() as calls:
        ai_usage.record("claude", MODEL, "extract", ai_usage.usage_from_claude(_usage(1000, 500)))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced["input_usd"] == pytest.approx(1000 * INPUT_USD)
    assert priced["output_usd"] == pytest.approx(500 * OUTPUT_USD)
    assert priced["cost_usd"] == pytest.approx(1000 * INPUT_USD + 500 * OUTPUT_USD)
    assert priced["usd_brl_rate"] == 5.0
    assert priced["cost_brl"] == pytest.approx(priced["cost_usd"] * 5.0)


def test_cached_prompt_tokens_are_charged_at_the_cache_rate():
    with ai_usage.recording() as calls:
        ai_usage.record("claude", MODEL, "extract", ai_usage.usage_from_claude(_usage(200, 0, cache_read=800)))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced["input_usd"] == pytest.approx(1000 * INPUT_USD - 800 * INPUT_USD + 800 * CACHED_USD)


def test_unmapped_model_has_no_price():
    with ai_usage.recording() as calls:
        ai_usage.record("claude", "claude-9-ultra-preview", "extract", ai_usage.usage_from_claude(_usage()))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced["cost_usd"] is None
    assert priced["cost_brl"] is None


def test_foreign_provider_has_no_price():
    call = {"provider": "gemini", "model": MODEL, "metered": True, "prompt_tokens": 1000, "output_tokens": 500}
    assert ai_pricing.usd_cost(call) is None


def test_no_exchange_rate_keeps_usd_and_drops_brl(monkeypatch):
    monkeypatch.setattr(fx, "usd_brl", lambda: None)
    with ai_usage.recording() as calls:
        ai_usage.record("claude", MODEL, "extract", ai_usage.usage_from_claude(_usage()))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced["cost_usd"] > 0
    assert priced["cost_brl"] is None


def test_pricing_does_not_import_litellm_runtime(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def reject_litellm(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise ImportError("Runtime unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_litellm)
    ai_pricing.model_prices.cache_clear()
    call = {"provider": "claude", "model": MODEL, "metered": True, "prompt_tokens": 1000, "output_tokens": 500}
    assert ai_pricing.usd_cost(call) == pytest.approx((1000 * INPUT_USD, 500 * OUTPUT_USD))
