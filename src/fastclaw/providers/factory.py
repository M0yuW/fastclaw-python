"""Provider construction from FastClaw configuration."""

from fastclaw.providers.anthropic import AnthropicProvider
from fastclaw.providers.base import Provider
from fastclaw.providers.openai import OpenAIProvider


def create_provider(
    *,
    name: str,
    api_key: str,
    api_base: str,
    api_type: str = "openai-compatible",
) -> Provider:
    """Create a provider for a FastClaw API type."""

    if api_type == "anthropic-messages":
        return AnthropicProvider(name=name, api_key=api_key, api_base=api_base)
    return OpenAIProvider(name=name, api_key=api_key, api_base=api_base)
