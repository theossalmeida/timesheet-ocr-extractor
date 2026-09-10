"""Prices the calls recorded by services/ai_usage.py.

The per-token rates come from litellm's model price map, which tracks Google's
published pricing (including the higher tier above a 200k-token prompt and the
discounted rate for cached input) instead of a table we would have to maintain
by hand. litellm is used purely as a price book here - the HTTP calls stay in
services/gemini_service.py.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from services import fx

logger = logging.getLogger(__name__)

# Price with the map bundled in the pinned litellm release: deterministic, and
# no network call while litellm is imported. Set LITELLM_LOCAL_MODEL_COST_MAP
# to "false" in the environment to let litellm refresh prices from its repo.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# litellm addresses the Gemini Developer API (generativelanguage.googleapis.com)
# as "gemini/<model>"; Vertex AI models are priced under a different prefix.
_MODEL_PREFIX = {"gemini": "gemini/"}


@lru_cache(maxsize=1)
def _litellm():
    """Import litellm lazily - it costs ~3s and is only needed when pricing."""
    from litellm import cost_per_token
    from litellm.types.utils import (
        CompletionTokensDetailsWrapper,
        PromptTokensDetailsWrapper,
        Usage,
    )

    return cost_per_token, Usage, PromptTokensDetailsWrapper, CompletionTokensDetailsWrapper


def usd_cost(call: dict) -> tuple[float, float] | None:
    """(input_usd, output_usd) for one recorded call, or None if unpriceable."""
    if not call.get("metered"):
        return None

    provider = call.get("provider", "")
    model = _MODEL_PREFIX.get(provider, "") + call.get("model", "")
    prompt_tokens = int(call.get("prompt_tokens") or 0)
    # Thinking tokens are already inside output_tokens: litellm bills the
    # completion count and reads reasoning_tokens only as a breakdown.
    output_tokens = int(call.get("output_tokens") or 0)
    try:
        cost_per_token, Usage, PromptDetails, CompletionDetails = _litellm()
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=int(call.get("total_tokens") or prompt_tokens + output_tokens),
            prompt_tokens_details=PromptDetails(cached_tokens=int(call.get("cached_tokens") or 0)),
            completion_tokens_details=CompletionDetails(reasoning_tokens=int(call.get("thought_tokens") or 0)),
        )
        input_usd, output_usd = cost_per_token(
            model=model,
            custom_llm_provider=provider or None,
            call_type="generate_content",
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            usage_object=usage,
        )
    except Exception as e:
        logger.warning("No price available for %s: %s", model, e)
        return None
    return round(float(input_usd), 8), round(float(output_usd), 8)


def price_calls(calls: list[dict], rate: float | None = None) -> list[dict]:
    """Copy of `calls` with input_usd/output_usd/cost_usd/rate/cost_brl filled.

    `rate` is the USD/BRL quote to use; omitted, today's is fetched. Costs stay
    None only when the call itself cannot be priced - an unmapped model or a
    response with no usage metadata - so callers can tell "free" from "unknown".
    """
    if rate is None:
        rate = fx.usd_brl() if calls else None
    priced = []
    for call in calls:
        costs = usd_cost(call)
        cost_usd = round(sum(costs), 8) if costs else None
        priced.append({
            **call,
            "input_usd": costs[0] if costs else None,
            "output_usd": costs[1] if costs else None,
            "cost_usd": cost_usd,
            "usd_brl_rate": rate if cost_usd is not None else None,
            "cost_brl": round(cost_usd * rate, 6) if cost_usd is not None and rate else None,
        })
    return priced
