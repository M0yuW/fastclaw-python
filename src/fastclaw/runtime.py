"""Runtime lifecycle and provider orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from enum import StrEnum
from types import MappingProxyType

import httpx

from fastclaw.network import create_pinned_http_client
from fastclaw.providers import Provider

HTTPClientFactory = Callable[[], httpx.AsyncClient]


class RuntimeState(StrEnum):
    """Observable runtime lifecycle states."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeError(Exception):
    """Base class for runtime errors."""


class RuntimeStateError(RuntimeError):
    """Raised when an operation is invalid for the current state."""


class RuntimeStartupError(RuntimeError):
    """Raised when a provider cannot be started."""

    def __init__(self, provider_name: str) -> None:
        super().__init__(f"provider {provider_name!r} failed to start")
        self.provider_name = provider_name


class RuntimeShutdownError(RuntimeError):
    """Raised after shutdown completes with one or more cleanup failures."""

    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__(f"runtime shutdown completed with {len(errors)} error(s)")
        self.errors = tuple(errors)


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


class Runtime:
    """Own provider and shared HTTP client lifecycles.

    Providers are started in registration order and stopped in reverse order.
    If startup fails, already-started providers are rolled back before the
    startup error is raised.
    """

    def __init__(
        self,
        providers: tuple[Provider, ...] = (),
        *,
        http_client_factory: HTTPClientFactory = _default_http_client,
        web_http_client_factory: HTTPClientFactory = create_pinned_http_client,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self._http_client_factory = http_client_factory
        self._web_http_client_factory = web_http_client_factory
        self._http_client: httpx.AsyncClient | None = None
        self._web_http_client: httpx.AsyncClient | None = None
        self._state = RuntimeState.CREATED
        self._lock = asyncio.Lock()

        for provider in providers:
            self.register_provider(provider)

    @property
    def state(self) -> RuntimeState:
        """Return the current lifecycle state."""

        return self._state

    @property
    def providers(self) -> Mapping[str, Provider]:
        """Return a read-only view of registered providers."""

        return MappingProxyType(self._providers)

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Return the shared client while the runtime is running."""

        if self._state is not RuntimeState.RUNNING or self._http_client is None:
            raise RuntimeStateError("the HTTP client is only available while running")
        return self._http_client

    @property
    def web_http_client(self) -> httpx.AsyncClient:
        """Return the DNS-pinned client dedicated to public web tools."""

        if self._state is not RuntimeState.RUNNING or self._web_http_client is None:
            raise RuntimeStateError("the web HTTP client is only available while running")
        return self._web_http_client

    def register_provider(self, provider: Provider) -> None:
        """Register a provider before the first startup."""

        if self._state is not RuntimeState.CREATED:
            raise RuntimeStateError("providers can only be registered before startup")
        if provider.name in self._providers:
            raise ValueError(f"provider name {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    async def start(self) -> None:
        """Start the shared client and all providers.

        Calling this method more than once while running is safe.
        """

        async with self._lock:
            if self._state is RuntimeState.RUNNING:
                return
            if self._state is not RuntimeState.CREATED:
                raise RuntimeStateError(f"cannot start runtime in state {self._state}")

            self._state = RuntimeState.STARTING
            try:
                client = self._http_client_factory()
            except BaseException as exc:
                self._state = RuntimeState.FAILED
                raise RuntimeStartupError("http-client") from exc
            try:
                web_client = self._web_http_client_factory()
            except BaseException as exc:
                try:
                    await client.aclose()
                except BaseException:
                    pass
                self._state = RuntimeState.FAILED
                raise RuntimeStartupError("web-http-client") from exc

            started: list[Provider] = []
            current_provider: Provider | None = None

            try:
                for current_provider in self._providers.values():
                    await current_provider.start(client)
                    started.append(current_provider)
            except BaseException as exc:
                for provider in reversed(started):
                    try:
                        await provider.stop()
                    except BaseException:
                        pass
                try:
                    await client.aclose()
                except BaseException:
                    pass
                try:
                    await web_client.aclose()
                except BaseException:
                    pass
                self._state = RuntimeState.FAILED
                if isinstance(exc, asyncio.CancelledError):
                    raise
                provider_name = current_provider.name if current_provider else "unknown"
                raise RuntimeStartupError(provider_name) from exc

            self._http_client = client
            self._web_http_client = web_client
            self._state = RuntimeState.RUNNING

    async def stop(self) -> None:
        """Stop providers and close the shared client.

        Cleanup is attempted for every resource even when one provider fails.
        Calling this method more than once is safe.
        """

        async with self._lock:
            if self._state is RuntimeState.STOPPED:
                return
            if self._state in {RuntimeState.CREATED, RuntimeState.FAILED}:
                self._state = RuntimeState.STOPPED
                return
            if self._state is not RuntimeState.RUNNING:
                raise RuntimeStateError(f"cannot stop runtime in state {self._state}")

            self._state = RuntimeState.STOPPING
            errors: list[BaseException] = []

            for provider in reversed(tuple(self._providers.values())):
                try:
                    await provider.stop()
                except BaseException as exc:
                    errors.append(exc)

            if self._http_client is not None:
                try:
                    await self._http_client.aclose()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    self._http_client = None

            if self._web_http_client is not None:
                try:
                    await self._web_http_client.aclose()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    self._web_http_client = None

            self._state = RuntimeState.FAILED if errors else RuntimeState.STOPPED
            if errors:
                raise RuntimeShutdownError(errors)

    async def readiness(self) -> dict[str, bool]:
        """Return the readiness result for every provider.

        Probe errors are represented as ``False`` so health endpoints never
        leak provider exceptions.
        """

        providers = tuple(self._providers.values())
        if self._state is not RuntimeState.RUNNING:
            return {provider.name: False for provider in providers}

        results = await asyncio.gather(
            *(provider.ready() for provider in providers), return_exceptions=True
        )
        return {
            provider.name: bool(result) if not isinstance(result, BaseException) else False
            for provider, result in zip(providers, results, strict=True)
        }

    async def is_ready(self) -> bool:
        """Return whether the runtime and all providers are ready."""

        if self._state is not RuntimeState.RUNNING:
            return False
        readiness = await self.readiness()
        return all(readiness.values())

    async def __aenter__(self) -> Runtime:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.stop()
