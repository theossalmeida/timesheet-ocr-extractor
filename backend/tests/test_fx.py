from unittest.mock import MagicMock, patch

import httpx
import pytest

from services import fx


@pytest.fixture(autouse=True)
def clear_cache():
    fx._cache.update(rate=None, fetched_at=0.0)
    yield
    fx._cache.update(rate=None, fetched_at=0.0)


def _quote(bid):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {'skus': [{'pricingInfo': [{'currencyConversionRate': bid}]}]}
    response.raise_for_status = MagicMock()
    return response


def test_fetches_and_caches_the_daily_quote():
    fx.settings.GOOGLE_CLOUD_API_KEY = 'test-key'
    with patch('httpx.get', return_value=_quote('5.4321')) as get:
        assert fx.usd_brl() == pytest.approx(5.4321)
        assert fx.usd_brl() == pytest.approx(5.4321)

    assert get.call_count == 1
    assert get.call_args.kwargs['params'] == {'currencyCode': 'BRL', 'pageSize': 1, 'key': 'test-key'}


def test_refetches_after_the_cache_expires():
    fx.settings.GOOGLE_CLOUD_API_KEY = 'test-key'
    with patch('httpx.get', return_value=_quote('5.10')):
        fx.usd_brl()
    fx._cache['fetched_at'] -= fx.FX_TTL_SECONDS + 1

    with patch('httpx.get', return_value=_quote('5.90')):
        assert fx.usd_brl() == pytest.approx(5.90)


def test_falls_back_to_the_configured_rate(monkeypatch):
    monkeypatch.setattr(fx.settings, 'USD_BRL_RATE', 5.25)

    with patch('httpx.get', side_effect=httpx.ConnectError('offline')):
        assert fx.usd_brl() == pytest.approx(5.25)


def test_prefers_a_stale_quote_over_the_configured_fallback(monkeypatch):
    monkeypatch.setattr(fx.settings, 'GOOGLE_CLOUD_API_KEY', 'test-key')
    monkeypatch.setattr(fx.settings, 'USD_BRL_RATE', 5.25)
    with patch('httpx.get', return_value=_quote('5.80')):
        fx.usd_brl()
    fx._cache['fetched_at'] -= fx.FX_TTL_SECONDS + 1

    with patch('httpx.get', side_effect=httpx.ConnectError('offline')):
        assert fx.usd_brl() == pytest.approx(5.80)


def test_rejects_an_out_of_range_quote(monkeypatch):
    monkeypatch.setattr(fx.settings, 'USD_BRL_RATE', 5.25)

    monkeypatch.setattr(fx.settings, 'GOOGLE_CLOUD_API_KEY', 'test-key')
    with patch('httpx.get', return_value=_quote('0.0')):
        assert fx.usd_brl() == pytest.approx(5.25)


def test_returns_none_when_no_rate_can_be_established(monkeypatch):
    monkeypatch.setattr(fx.settings, 'USD_BRL_RATE', 0.0)

    with patch('httpx.get', side_effect=httpx.ConnectError('offline')):
        assert fx.usd_brl() is None
