"""Interfaces implemented by FastClaw providers."""

from typing import Protocol, runtime_checkable

import httpx


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
