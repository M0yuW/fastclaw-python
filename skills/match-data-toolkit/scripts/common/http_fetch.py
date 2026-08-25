"""Bounded HTTP GET for the football toolkit.

Why this exists instead of `urllib.request.urlopen(url, timeout=N)`: urlopen's
timeout is per connect attempt, not per call. `socket.create_connection` walks
every address `getaddrinfo` returns and spends the full timeout on each one
before moving on. www.thesportsdb.com resolves to three Cloudflare anycast
addresses, and at least one of them blackholes SYN from some networks — so a
"25 second" request measured 51.7s here (two dead addresses first), and a 90s
timeout measured 151s. The server itself answers in 0.2s once reached: connect
was 100% of the cost.

That failure mode is invisible in the response and expensive for an agent: the
tool call looks hung for a minute and a half, and the model concludes the
primary data source is unavailable when it is merely behind a dead route.

So this module:
  * probes addresses one at a time with a short per-address connect budget, so
    a dead route costs seconds rather than a minute;
  * enforces one deadline across the whole call, including retries, so the
    caller's stated timeout is the real worst case;
  * falls back to a slow second pass using whatever budget is left, so a
    genuinely slow-but-alive network still succeeds;
  * refuses cross-host redirects, keeping the reviewed host the only host a
    script can be steered to.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.parse
from typing import Any

# One deadline for the whole call. Sized so a coordinator waiting on several
# specialists doesn't stall a turn: worst case is this, not a multiple of it.
DEFAULT_TIMEOUT = 20.0

# Per-address budget for the first pass. Long enough for a healthy TLS-capable
# host anywhere in the world, short enough that two dead routes cost ~8s.
FAST_CONNECT_TIMEOUT = 4.0

# Redirect ceiling. The reviewed endpoints don't redirect; this exists so a
# provider-side change degrades to an error instead of a loop.
MAX_REDIRECTS = 3


class FetchError(RuntimeError):
    """Transport-level failure, already reduced to a safe category.

    `kind` is one of "timeout", "connect", "http", "too_large", "malformed".
    Callers map it onto their own error_code taxonomy; the detail is safe to
    surface because it never contains credentials or a full URL.
    """

    def __init__(self, kind: str, detail: str, status: int = 0) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status


class _Deadline:
    """Monotonic wall-clock budget shared by every attempt in one call."""

    def __init__(self, total: float) -> None:
        self.total = max(0.5, float(total))
        self._start = time.monotonic()

    def remaining(self) -> float:
        return self.total - (time.monotonic() - self._start)

    def expired(self) -> bool:
        return self.remaining() <= 0

    def slice(self, want: float) -> float:
        """Return `want`, clamped to what's left. Raises when nothing is left."""
        left = self.remaining()
        if left <= 0:
            raise FetchError("timeout", f"request exceeded its {self.total:.0f}s budget")
        return max(0.25, min(want, left))


def _addresses(host: str, port: int, deadline: _Deadline) -> list[tuple[int, Any]]:
    """Resolve host to (family, sockaddr) pairs, preserving resolver order."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError("connect", f"could not resolve {host}") from exc
    if deadline.expired():
        raise FetchError("timeout", f"request exceeded its {deadline.total:.0f}s budget")
    return [(info[0], info[4]) for info in infos]


def _connect(host: str, port: int, deadline: _Deadline, per_address: float) -> socket.socket:
    """Connect to the first reachable address, one address at a time.

    This is the whole point of the module. Calling create_connection would hand
    the same timeout to every address in turn; here each address gets a slice of
    one shared budget, so a blackholed anycast IP costs `per_address` seconds
    instead of the caller's full timeout.
    """
    last: Exception | None = None
    for family, sockaddr in _addresses(host, port, deadline):
        budget = deadline.slice(per_address)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(budget)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
            if deadline.expired():
                break
    if deadline.expired():
        raise FetchError("timeout", f"request exceeded its {deadline.total:.0f}s budget") from last
    raise FetchError("connect", f"no reachable address for {host}") from last


def _one_attempt(
    url: str,
    headers: dict[str, str],
    max_bytes: int,
    deadline: _Deadline,
    per_address: float,
) -> tuple[bytes, dict[str, str]]:
    """Perform one GET, following same-host redirects.

    Returns (body, response_headers) where the header names are lowercased so
    callers can look up quota fields without worrying about provider casing.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(current)
        if parts.scheme != "https":
            raise FetchError("connect", "only https requests are allowed")
        host = parts.hostname or ""
        port = parts.port or 443
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"

        sock = _connect(host, port, deadline, per_address)
        try:
            sock.settimeout(deadline.slice(deadline.remaining()))
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except OSError as exc:
            sock.close()
            if isinstance(exc, socket.timeout):
                raise FetchError("timeout", "TLS handshake timed out") from exc
            raise FetchError("connect", "TLS handshake failed") from exc

        conn = http.client.HTTPSConnection(host, port)
        conn.sock = tls
        try:
            conn.timeout = deadline.slice(deadline.remaining())
            tls.settimeout(conn.timeout)
            conn.request("GET", target, headers=headers)
            response = conn.getresponse()
            status = response.status
            location = response.getheader("Location") or ""
            received = {name.lower(): value for name, value in response.getheaders()}
            # Read the body before deciding: an unread response leaves the
            # socket unusable, and we close it either way.
            body = response.read(max_bytes + 1)
        except socket.timeout as exc:
            raise FetchError("timeout", "the provider did not respond in time") from exc
        except OSError as exc:
            raise FetchError("connect", "connection failed mid-request") from exc
        except http.client.HTTPException as exc:
            raise FetchError("malformed", "the provider sent an unreadable response") from exc
        finally:
            conn.close()

        if status in (301, 302, 303, 307, 308) and location:
            nxt = urllib.parse.urljoin(current, location)
            # A redirect to another host would let a provider-side change
            # silently move a reviewed request somewhere unreviewed.
            if urllib.parse.urlsplit(nxt).hostname != host:
                raise FetchError("http", f"refused cross-host redirect from {host}", status)
            current = nxt
            continue

        if status != 200:
            raise FetchError("http", f"the provider returned HTTP {status}", status)
        if len(body) > max_bytes:
            raise FetchError("too_large", "the provider response exceeded the size limit")
        return body, received

    raise FetchError("http", "too many redirects")


def fetch_with_headers(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 2_000_000,
) -> tuple[bytes, dict[str, str]]:
    """GET `url` within `timeout` seconds total, returning body and headers.

    Two passes: the first gives each address a short connect budget so dead
    routes are skipped quickly; if every address looks dead, the second pass
    spends whatever budget remains on a single attempt, so a network that is
    slow rather than broken still gets an answer.

    Response header names are lowercased. Callers that need a provider's quota
    or rate-limit headers use this; everything else uses fetch_bytes/fetch_json.
    """
    deadline = _Deadline(timeout)
    hdrs = {"Accept": "application/json", "User-Agent": "fastclaw-football-go/1.0"}
    if headers:
        hdrs.update(headers)

    try:
        return _one_attempt(url, hdrs, max_bytes, deadline, FAST_CONNECT_TIMEOUT)
    except FetchError as first:
        # Only a connect-level failure is worth a slower retry. A timeout means
        # the budget is gone, and an HTTP or size error will repeat.
        if first.kind != "connect" or deadline.remaining() < 1.0:
            raise
        return _one_attempt(url, hdrs, max_bytes, deadline, deadline.remaining())


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 2_000_000,
) -> bytes:
    """fetch_with_headers, discarding the response headers."""
    body, _ = fetch_with_headers(
        url, headers=headers, timeout=timeout, max_bytes=max_bytes
    )
    return body


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError("malformed", "the provider returned malformed JSON") from exc


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 2_000_000,
) -> Any:
    """fetch_bytes + JSON decode, with decode failures reported as "malformed"."""
    return _decode_json(
        fetch_bytes(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    )


def fetch_json_with_headers(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 2_000_000,
) -> tuple[Any, dict[str, str]]:
    """fetch_with_headers + JSON decode."""
    body, received = fetch_with_headers(
        url, headers=headers, timeout=timeout, max_bytes=max_bytes
    )
    return _decode_json(body), received
