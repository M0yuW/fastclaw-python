"""Persistence-neutral records exposed by repository protocols."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserRecord(Record):
    id: str
    username: str
    email: str
    password_hash: str
    display_name: str = ""
    role: str = "user"
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WebSessionRecord(Record):
    sid: str
    user_id: str
    created_at: datetime
    expires_at: datetime


class APIKeyRecord(Record):
    id: str
    user_id: str
    name: str = ""
    key_hash: str
    key_prefix: str = ""
    type: str = "agent"
    created_at: datetime = Field(default_factory=utc_now)


class AgentRecord(Record):
    id: str
    user_id: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    is_public: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SessionRecord(Record):
    user_id: str
    agent_id: str
    key: str
    channel: str = ""
    account_id: str = ""
    chat_id: str = ""
    project_id: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    title: str = ""
    message_count: int = 0
    chatter_user_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentFileRecord(Record):
    agent_id: str
    user_id: str
    filename: str
    data: bytes
    updated_at: datetime = Field(default_factory=utc_now)


class ConfigRecord(Record):
    id: str
    kind: str
    user_id: str = ""
    agent_id: str = ""
    name: str
    enabled: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CronJobRecord(Record):
    id: str
    user_id: str = ""
    agent_id: str
    name: str
    type: str
    schedule: str
    message: str
    channel: str = ""
    chat_id: str = ""
    account_id: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    last_run: datetime | None = None
    next_run: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
