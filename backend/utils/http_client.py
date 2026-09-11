from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx


@asynccontextmanager
async def ensure_async_client(
    client: httpx.AsyncClient | None, timeout: httpx.Timeout
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield `client` if given, otherwise a short-lived one scoped to this call."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        yield owned_client
