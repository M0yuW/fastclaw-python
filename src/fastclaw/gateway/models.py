"""Camel-case wire models used by the compatibility gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")


class LoginRequest(WireModel):
    login: str
    password: str


class OnboardRequest(WireModel):
    username: str
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""
    provider: str = ""
    api_base: str = ""
    api_key: str = ""
    api_type: str = "openai-compatible"
    auth_type: str = "bearer-token"
    model: str = ""
    agent_name: str = "default"
    sandbox_enabled: bool = False
    sandbox_backend: str | None = None
    sandbox_image: str | None = None
    sandbox_e2b_key: str | None = None


class ProviderWrite(WireModel):
    name: str
    api_base: str = ""
    api_key: str = ""
    api_type: str = "openai-compatible"
    auth_type: str = "bearer-token"
    models: list[dict[str, Any]] = Field(default_factory=list)
    scope: Literal["system", "user", "agent"] = "user"
    scope_id: str = ""


class ProviderUpdate(WireModel):
    name: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    api_type: str | None = None
    auth_type: str | None = None
    models: list[dict[str, Any]] | None = None
    enabled: bool | None = None


class ProviderTest(WireModel):
    api_base: str
    api_key: str
    model: str
    api_type: str = "openai-compatible"
    auth_type: str = "bearer-token"


class StoredProviderTest(WireModel):
    model: str


class ChatInput(WireModel):
    agent_id: str
    session_id: str
    message: str
    image_urls: list[str] = Field(default_factory=list)


class OpenAIMessage(WireModel):
    role: str
    content: str


class OpenAIChatInput(WireModel):
    model: str = ""
    messages: list[OpenAIMessage]
    stream: bool = False
    user: str = ""
    agent_id: str = ""


class AgentCreate(WireModel):
    name: str
    description: str = ""
    model: str = ""


class APIKeyCreate(WireModel):
    name: str
    agent_ids: list[str] = Field(default_factory=list)


class APIKeyAgents(WireModel):
    agent_ids: list[str] = Field(default_factory=list)


class ProviderSelection(BaseModel):
    name: str
    api_key: str
    api_base: str
    api_type: str
    model: str
    source: str
    config_id: str = ""


JSONDict = dict[str, Any]
