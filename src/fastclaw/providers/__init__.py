"""Provider extension points."""

from fastclaw.providers.anthropic import AnthropicProvider
from fastclaw.providers.base import Provider, ProviderContextCompactor
from fastclaw.providers.errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderNotStartedError,
    ProviderStreamError,
)
from fastclaw.providers.factory import create_provider
from fastclaw.providers.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ContentPart,
    FunctionCall,
    ImageURL,
    MessageRole,
    NativeCompactionResult,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    ToolDefinition,
    ToolFunction,
    Usage,
)
from fastclaw.providers.openai import OpenAIProvider
from fastclaw.providers.stream import ProviderStream

__all__ = [
    "AnthropicProvider",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ContentPart",
    "FunctionCall",
    "ImageURL",
    "MessageRole",
    "NativeCompactionResult",
    "OpenAIProvider",
    "Provider",
    "ProviderContextCompactor",
    "ProviderError",
    "ProviderEvent",
    "ProviderEventType",
    "ProviderHTTPError",
    "ProviderNotStartedError",
    "ProviderStream",
    "ProviderStreamError",
    "ToolCall",
    "ToolDefinition",
    "ToolFunction",
    "Usage",
    "create_provider",
]
