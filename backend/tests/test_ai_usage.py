import asyncio
import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pypdf
import pytest

from services import ai_pricing, ai_usage, fx
from services.gemini_service import (
    GeminiExtractionError,
    extract_with_gemini,
    extract_with_gemini_adaptive,
)

MODEL = 'gemini-3.8-flash'
# Published rates for the model above, per token (litellm's price map).
INPUT_USD = 2e-06
OUTPUT_USD = 1.2e-05
CACHED_USD = 2e-07
INPUT_USD_ABOVE_200K = 4e-06
OUTPUT_USD_ABOVE_200K = 1.8e-05


@pytest.fixture(autouse=True)
def configure_gemini(monkeypatch):
    from services import gemini_service

    monkeypatch.setattr(gemini_service.settings, 'GEMINI_API_KEY', 'test-key')
    monkeypatch.setattr(gemini_service.settings, 'GEMINI_MODEL', MODEL)


@pytest.fixture(autouse=True)
def fixed_exchange_rate(monkeypatch):
    monkeypatch.setattr(fx, 'usd_brl', lambda: 5.0)


def _usage(prompt=1000, candidates=400, thoughts=0, cached=0):
    return {
        'promptTokenCount': prompt,
        'candidatesTokenCount': candidates,
        'thoughtsTokenCount': thoughts,
        'cachedContentTokenCount': cached,
        'totalTokenCount': prompt + candidates + thoughts,
    }


def _response(rows=None, usage=None, text=None):
    response = MagicMock()
    response.status_code = 200
    body = text if text is not None else json.dumps(rows if rows is not None else [])
    response.json.return_value = {
        'candidates': [{'content': {'parts': [{'text': body}]}}],
        **({'usageMetadata': usage} if usage else {}),
    }
    response.text = body
    return response


def _client(*responses):
    client = AsyncMock()
    client.post = AsyncMock(side_effect=list(responses))
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


def test_usage_bills_thinking_tokens_as_output():
    usage = ai_usage.usage_from_gemini({'usageMetadata': _usage(prompt=1000, candidates=400, thoughts=250, cached=600)})

    assert usage['prompt_tokens'] == 1000
    assert usage['cached_tokens'] == 600
    # Thinking tokens are billed at the output rate and reported apart from
    # candidatesTokenCount, so they must be folded into the output count.
    assert usage['output_tokens'] == 650
    assert usage['thought_tokens'] == 250


def test_usage_counts_tool_use_prompt_tokens():
    usage = ai_usage.usage_from_gemini({'usageMetadata': {'promptTokenCount': 900, 'toolUsePromptTokenCount': 100, 'candidatesTokenCount': 10, 'totalTokenCount': 1010}})

    assert usage['prompt_tokens'] == 1000
    assert usage['total_tokens'] == 1010


def test_response_without_usage_metadata_is_unmetered():
    assert ai_usage.usage_from_gemini({'candidates': []}) is None

    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', None)

    assert calls[0]['metered'] is False
    assert ai_pricing.price_calls(calls)[0]['cost_usd'] is None


def test_records_one_call_per_chunk_and_page_fallback():
    """A 3-page PDF is two chunk calls; a failed chunk retried page by page adds more."""
    responses = [
        _response(rows=[{'data': '01/03/2024', 'marcacoes': ['08:00', '17:00']}], usage=_usage(prompt=1200, candidates=300)),
        _response(text='not json {{{', usage=_usage(prompt=800, candidates=50)),
        _response(rows=[], usage=_usage(prompt=800, candidates=20)),
    ]

    with patch('httpx.AsyncClient', return_value=_client(*responses)):
        with ai_usage.recording() as calls:
            rows = asyncio.run(extract_with_gemini_adaptive(_pdf(3)))

    assert len(rows) == 1
    # chunk(2 pages) + failed chunk(page 3) + its single-page retry: Google
    # billed all three, including the response we could not parse.
    assert [call['kind'] for call in calls] == ['extract', 'extract', 'extract']
    assert [call['prompt_tokens'] for call in calls] == [1200, 800, 800]
    assert all(call['metered'] for call in calls)


def test_failed_extraction_still_records_usage():
    with patch('httpx.AsyncClient', return_value=_client(_response(text='garbage', usage=_usage()))):
        with ai_usage.recording() as calls:
            with pytest.raises(GeminiExtractionError):
                asyncio.run(extract_with_gemini(b'fake pdf'))

    assert len(calls) == 1
    assert calls[0]['metered'] is True


@pytest.mark.parametrize('error, expected_calls', [(httpx.ReadTimeout('slow'), 2), (httpx.ConnectTimeout('unreachable'), 0)])
def test_transport_failure_records_uncertain_requests(error, expected_calls):
    client = _client()
    client.post = AsyncMock(side_effect=error)

    with patch('httpx.AsyncClient', return_value=client), patch('services.gemini_service.asyncio.sleep', AsyncMock()):
        with ai_usage.recording() as calls:
            with pytest.raises(GeminiExtractionError):
                asyncio.run(extract_with_gemini(b'fake pdf'))

    assert len(calls) == expected_calls
    assert all(not call['metered'] for call in calls)


def test_recording_is_scoped_to_the_block():
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage()}))
    ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage()}))

    assert len(calls) == 1


def test_price_follows_published_per_token_rates():
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage(prompt=1000, candidates=400, thoughts=100)}))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced['input_usd'] == pytest.approx(1000 * INPUT_USD)
    assert priced['output_usd'] == pytest.approx(500 * OUTPUT_USD)
    assert priced['cost_usd'] == pytest.approx(1000 * INPUT_USD + 500 * OUTPUT_USD)
    assert priced['usd_brl_rate'] == 5.0
    assert priced['cost_brl'] == pytest.approx(priced['cost_usd'] * 5.0)


def test_price_uses_the_higher_tier_above_200k_prompt_tokens():
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage(prompt=300_000, candidates=500)}))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced['input_usd'] == pytest.approx(300_000 * INPUT_USD_ABOVE_200K)
    assert priced['output_usd'] == pytest.approx(500 * OUTPUT_USD_ABOVE_200K)


def test_cached_prompt_tokens_are_charged_at_the_cache_rate():
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage(prompt=1000, candidates=0, cached=800)}))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced['input_usd'] == pytest.approx(200 * INPUT_USD + 800 * CACHED_USD)


def test_unmapped_model_has_no_price():
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', 'gemini-9-ultra-preview', 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage()}))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced['cost_usd'] is None
    assert priced['cost_brl'] is None


def test_no_exchange_rate_keeps_usd_and_drops_brl(monkeypatch):
    monkeypatch.setattr(fx, 'usd_brl', lambda: None)
    with ai_usage.recording() as calls:
        ai_usage.record('gemini', MODEL, 'extract', ai_usage.usage_from_gemini({'usageMetadata': _usage()}))

    priced = ai_pricing.price_calls(calls)[0]

    assert priced['cost_usd'] > 0
    assert priced['cost_brl'] is None


def test_pricing_does_not_import_litellm_runtime(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def reject_litellm(name, *args, **kwargs):
        if name == 'litellm' or name.startswith('litellm.'):
            raise ImportError('Runtime unavailable')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_litellm)
    ai_pricing.model_prices.cache_clear()
    call = {'provider': 'gemini', 'model': MODEL, 'metered': True, 'prompt_tokens': 1000, 'output_tokens': 500}
    assert ai_pricing.usd_cost(call) == pytest.approx((0.002, 0.006))


def test_exactly_200k_uses_standard_rates():
    call = {'provider': 'gemini', 'model': MODEL, 'metered': True, 'prompt_tokens': 200_000, 'cached_tokens': 100_000, 'output_tokens': 1000}
    assert ai_pricing.usd_cost(call) == pytest.approx((0.22, 0.012))


def test_long_prompt_cached_tokens_use_higher_tier():
    call = {'provider': 'gemini', 'model': MODEL, 'metered': True, 'prompt_tokens': 300_000, 'cached_tokens': 100_000, 'output_tokens': 1000}
    assert ai_pricing.usd_cost(call) == pytest.approx((0.84, 0.018))
