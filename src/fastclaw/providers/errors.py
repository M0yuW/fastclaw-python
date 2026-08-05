"""Provider-specific error types."""


class ProviderError(Exception):
    """Base class for provider failures."""


class ProviderNotStartedError(ProviderError):
    """Raised when a provider is used outside its lifecycle."""


class ProviderStreamError(ProviderError):
    """Raised when a provider stream is incomplete or malformed."""


class ProviderHTTPError(ProviderError):
    """An upstream provider returned a non-success HTTP response."""

    def __init__(
        self,
        *,
        provider: str,
        status_code: int,
        body: str,
        retryable: bool,
    ) -> None:
        super().__init__(f"{provider} returned HTTP {status_code}")
        self.provider = provider
        self.status_code = status_code
        self.body = body
        self.retryable = retryable
