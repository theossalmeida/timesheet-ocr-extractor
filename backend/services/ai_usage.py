"""Context-local ledger of paid AI calls.

Claude bills per token and one document is rarely one call: the adaptive
extractor splits the PDF into 2-page chunks and falls back to single-page
calls, so the cost of a document is the sum of the calls it actually made -
including calls whose answer we ended up discarding, because Anthropic bills those
too. Every call is appended here as it happens; pricing runs later
(services/ai_pricing.py) and each call is stored in the ai_usage table.

Usage:

    with ai_usage.recording() as calls:
        ...                     # anything that may reach Claude
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


def usage_from_claude(usage: object) -> dict | None:
    """Token counts from a Messages API `usage` object, or None when absent.

    None means the call happened but we cannot say what it cost - the caller
    records it as unmetered so the document reports an unknown cost instead of
    a falsely cheap one.
    """
    if usage is None:
        return None

    uncached = int(getattr(usage, "input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    output = int(getattr(usage, "output_tokens", 0) or 0)
    prompt = uncached + cache_read + cache_write
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cache_read,
        "output_tokens": output,
        "thought_tokens": 0,
        "total_tokens": prompt + output,
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
