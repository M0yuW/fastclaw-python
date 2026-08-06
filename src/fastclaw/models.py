"""Pydantic API models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fastclaw.runtime import RuntimeState


class HealthResponse(BaseModel):
    """Liveness response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    state: RuntimeState


class ReadinessResponse(BaseModel):
    """Runtime and provider readiness response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    state: RuntimeState
    providers: dict[str, bool]
    checks: dict[str, bool] = Field(default_factory=dict)
