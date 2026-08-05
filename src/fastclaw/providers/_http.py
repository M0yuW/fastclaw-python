"""Shared HTTP and SSE helpers for provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from fastclaw.providers.errors import ProviderHTTPError


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete SSE data payloads, including multi-line frames."""

    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if separator and value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


async def raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    body = (await response.aread()).decode("utf-8", errors="replace")[:2048]
    raise ProviderHTTPError(
        provider=provider,
        status_code=response.status_code,
        body=body,
        retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500,
    )


def strip_provider_prefix(model: str) -> str:
    return model.split("/", 1)[-1]
