"""Interfaces implemented by FastClaw LLM providers."""

from typing import Protocol, runtime_checkable

import httpx

from fastclaw.providers.models import ChatRequest, ChatResponse
from fastclaw.providers.stream import ProviderStream


@runtime_checkable
class Provider(Protocol):
    """A lifecycle-aware service used by the FastClaw runtime.

    Providers receive the runtime's shared HTTP client when they start. A
    provider must release only the resources it owns when ``stop`` is called;
    the runtime closes the shared client after all providers have stopped.
    """

    @property
    def name(self) -> str:
        """Return a stable, unique provider name."""

        ...

    async def start(self, client: httpx.AsyncClient) -> None:
        """Initialize the provider."""

        ...

    async def stop(self) -> None:
        """Release provider-owned resources."""

        ...

    async def ready(self) -> bool:
        """Return whether the provider can currently serve requests."""

        ...

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run a chat request and return its complete response."""

        ...

    def stream(self, request: ChatRequest) -> ProviderStream:
        """Start a lazily-executed streaming chat request."""

        ...
