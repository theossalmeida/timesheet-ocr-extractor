"""USD -> BRL conversion for AI usage costs.

Gemini is billed in dollars and the history panel shows reais, so every priced
call is converted with the rate of the day it ran. The rate is fetched at most
once a day, and the value used is frozen onto the ai_usage row so historical
costs never change under a later rate.
"""
from __future__ import annotations

import logging
import time

import httpx

from config import settings

logger = logging.getLogger(__name__)

GOOGLE_CLOUD_SKUS_URL = "https://cloudbilling.googleapis.com/v1/services/{service_id}/skus"
GOOGLE_CLOUD_SERVICE_ID = "2A08-2A2D-BEF9"
FX_TTL_SECONDS = 24 * 3600
FX_TIMEOUT_SECONDS = 5.0
# Sanity band for a USD/BRL quote. A malformed or misread response (0, 1.0,
# cents instead of reais) would silently mis-bill every document, so anything
# outside this range is rejected in favour of the previous/configured rate.
FX_MIN_RATE = 1.0
FX_MAX_RATE = 100.0

_cache: dict = {"rate": None, "fetched_at": 0.0}


def _fetch_usd_brl() -> float:
    api_key = settings.GOOGLE_CLOUD_API_KEY.strip()
    if not api_key:
        raise ValueError("GOOGLE_CLOUD_API_KEY is not configured")
    response = httpx.get(
        GOOGLE_CLOUD_SKUS_URL.format(service_id=GOOGLE_CLOUD_SERVICE_ID),
        params={"currencyCode": "BRL", "pageSize": 1, "key": api_key},
        timeout=FX_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    rate = float(response.json()["skus"][0]["pricingInfo"][0]["currencyConversionRate"])
    if not FX_MIN_RATE <= rate <= FX_MAX_RATE:
        raise ValueError(f"USD-BRL rate out of range: {rate}")
    return rate


def usd_brl() -> float | None:
    """Today's USD/BRL rate, or None when no rate can be established.

    Order of preference: the rate cached in the last 24h, a fresh quote, the
    last rate we managed to fetch (stale but real), then the configured
    USD_BRL_RATE. None makes the cost unknown rather than invented.
    """
    now = time.monotonic()
    if _cache["rate"] and now - _cache["fetched_at"] < FX_TTL_SECONDS:
        return _cache["rate"]

    try:
        rate = _fetch_usd_brl()
    except Exception as e:
        logger.warning("USD-BRL quote unavailable (%s); falling back", e)
        if _cache["rate"]:
            return _cache["rate"]
        fallback = float(settings.USD_BRL_RATE or 0)
        return fallback if FX_MIN_RATE <= fallback <= FX_MAX_RATE else None

    _cache["rate"] = rate
    _cache["fetched_at"] = now
    logger.info("USD-BRL rate refreshed: %.4f", rate)
    return rate
