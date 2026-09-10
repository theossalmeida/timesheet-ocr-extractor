"""Context-local ledger of paid AI calls.

Gemini bills per token and one document is rarely one call: the adaptive
extractor splits the PDF into 2-page chunks and falls back to single-page
calls, so the cost of a document is the sum of the calls it actually made -
including calls whose answer we ended up discarding, because Google bills those
too. Every call is appended here as it happens; pricing runs later
(services/ai_pricing.py) and each call is stored in the ai_usage table.

Usage:

    with ai_usage.recording() as calls:
        ...                     # anything that may reach Gemini
    # calls is now one dict per HTTP call, in order.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

_calls: ContextVar[list[dict] | None] = ContextVar("ai_usage_calls", default=None)


@contextmanager
def recording() -> Iterator[list[dict]]:
    """Collect every AI call made inside the block.

    The ledger is context-local, so calls awaited from the same task (the whole
    extraction pipeline) land in it, while unrelated concurrent requests keep
    their own list.
    """
    calls: list[dict] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def usage_from_gemini(response_json: dict) -> dict | None:
    """Token counts from a generateContent response, or None when absent.

    None means the call happened but we cannot say what it cost - the caller
    records it as unmetered so the document reports an unknown cost instead of
    a falsely cheap one.
    """
    meta = response_json.get("usageMetadata")
    if not isinstance(meta, dict) or not meta:
        return None

    prompt = int(meta.get("promptTokenCount") or 0)
    tool_use = int(meta.get("toolUsePromptTokenCount") or 0)
    candidates = int(meta.get("candidatesTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    cached = int(meta.get("cachedContentTokenCount") or 0)
    return {
        "prompt_tokens": prompt + tool_use,
        # Reported inside promptTokenCount, but billed at the cheaper cached rate.
        "cached_tokens": cached,
        # Thinking tokens are billed at the output rate and are reported
        # outside candidatesTokenCount.
        "output_tokens": candidates + thoughts,
        "thought_tokens": thoughts,
        "total_tokens": int(meta.get("totalTokenCount") or (prompt + tool_use + candidates + thoughts)),
    }


def record(provider: str, model: str, kind: str, usage: dict | None) -> dict:
    """Append one call to the active ledger (a no-op outside `recording`)."""
    call = {
        "provider": provider,
        "model": model,
        "kind": kind,
        "metered": usage is not None,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "thought_tokens": 0,
        "total_tokens": 0,
        **(usage or {}),
    }
    if usage is None:
        logger.warning("%s call (%s) reported no usage metadata; cost will be unknown", provider, kind)
    else:
        logger.info(
            "%s call metered - model=%s kind=%s prompt=%d cached=%d output=%d",
            provider,
            model,
            kind,
            call["prompt_tokens"],
            call["cached_tokens"],
            call["output_tokens"],
        )

    calls = _calls.get()
    if calls is not None:
        calls.append(call)
    return call
