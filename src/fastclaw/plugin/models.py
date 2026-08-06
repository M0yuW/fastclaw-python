"""JSON-RPC plugin manifests and wire contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PluginModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PluginManifest(PluginModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str
    version: str
    description: str = ""
    type: Literal["tool", "channel", "provider", "hook"] = "tool"
    command: str
    capabilities: tuple[str, ...] = ()
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    root: Path


class PluginToolDefinition(PluginModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class PluginError(RuntimeError):
    pass


class PluginProtocolError(PluginError):
    pass


class PluginRPCError(PluginError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


class PluginUnavailableError(PluginError):
    pass
