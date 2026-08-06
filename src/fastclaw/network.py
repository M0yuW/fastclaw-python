"""Outbound networking primitives with DNS targets pinned per request."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

import httpcore
import httpx

_TargetKey = tuple[str, int]
_SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)
_PINNED_TARGETS: ContextVar[Mapping[_TargetKey, tuple[str, ...]] | None] = ContextVar(
    "fastclaw_pinned_network_targets",
    default=None,
)


def _normalized_host(host: str) -> str:
    return host.encode("idna").decode("ascii").lower()


@contextmanager
def pinned_network_target(host: str, port: int, addresses: Sequence[str]) -> Iterator[None]:
    """Pin one origin to already validated IP addresses for this async context."""

    values = tuple(dict.fromkeys(addresses))
    if not values:
        raise ValueError("at least one pinned address is required")
    targets = dict(_PINNED_TARGETS.get() or {})
    targets[(_normalized_host(host), port)] = values
    token = _PINNED_TARGETS.set(targets)
    try:
        yield
    finally:
        _PINNED_TARGETS.reset(token)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to the IP addresses pinned for the requested origin.

    HTTP remains addressed to the original hostname, so httpcore preserves the
    Host header and uses that hostname for TLS SNI and certificate validation.
    Only the TCP destination is replaced.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        targets = (_PINNED_TARGETS.get() or {}).get((_normalized_host(host), port))
        if not targets:
            raise httpcore.ConnectError("unpinned outbound connection denied")

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in targets:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("Unix sockets are denied for public web fetches")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose TCP connections require a validated DNS pin."""

    def __init__(self, network_backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PinnedNetworkBackend(network_backend),
        )


def create_pinned_http_client() -> httpx.AsyncClient:
    """Create the dedicated, proxy-free client used by ``web_fetch``."""

    return httpx.AsyncClient(
        transport=PinnedAsyncHTTPTransport(),
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
    )
