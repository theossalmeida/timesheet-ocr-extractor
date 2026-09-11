from __future__ import annotations

import json
import logging
from functools import lru_cache
from importlib.metadata import distribution

from services import fx

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def model_prices() -> dict:
    path = distribution('litellm').locate_file('litellm/model_prices_and_context_window_backup.json')
    return json.loads(path.read_text(encoding='utf-8'))


def usd_cost(call: dict) -> tuple[float, float] | None:
    if not call.get('metered'):
        return None

    model = str(call.get('model') or '').strip().removeprefix('models/').removeprefix('gemini/')
    try:
        if call.get('provider') != 'gemini':
            raise ValueError('Unsupported pricing provider')
        prices = model_prices()['gemini/' + model]
        prompt_tokens = int(call.get('prompt_tokens') or 0)
        cached_tokens = int(call.get('cached_tokens') or 0)
        output_tokens = int(call.get('output_tokens') or 0)
        if not 0 <= cached_tokens <= prompt_tokens or output_tokens < 0:
            raise ValueError('Invalid token counts')
        tier = ''
        for threshold in (128_000, 200_000):
            suffix = f'_above_{threshold // 1000}k_tokens'
            if prompt_tokens > threshold and 'input_cost_per_token' + suffix in prices:
                tier = suffix
        input_rate = prices['input_cost_per_token' + tier]
        output_rate = prices['output_cost_per_token' + tier]
        cached_cost = cached_tokens * prices['cache_read_input_token_cost' + tier] if cached_tokens else 0
        input_usd = (prompt_tokens - cached_tokens) * input_rate + cached_cost
        output_usd = output_tokens * output_rate
    except Exception as error:
        logger.error('No price available for gemini/%s: %s: %s', model, type(error).__name__, error)
        return None
    return round(input_usd, 8), round(output_usd, 8)


def price_calls(calls: list[dict], rate: float | None = None) -> list[dict]:
    if rate is None:
        rate = fx.usd_brl() if calls else None
    priced = []
    for call in calls:
        costs = usd_cost(call)
        cost_usd = round(sum(costs), 8) if costs else None
        priced.append({
            **call,
            'input_usd': costs[0] if costs else None,
            'output_usd': costs[1] if costs else None,
            'cost_usd': cost_usd,
            'usd_brl_rate': rate if cost_usd is not None else None,
            'cost_brl': round(cost_usd * rate, 6) if cost_usd is not None and rate else None,
        })
    return priced
