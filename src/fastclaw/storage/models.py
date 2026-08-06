"""SQLAlchemy persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="user")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSessionModel(Base):
    __tablename__ = "web_sessions"

    sid: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class APIKeyModel(Base):
    __tablename__ = "apikeys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String, default="")
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str] = mapped_column(String, default="agent", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class APIKeyAgentModel(Base):
    __tablename__ = "apikey_agents"

    apikey_id: Mapped[str] = mapped_column(
        ForeignKey("apikeys.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class AgentModel(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTeamModel(Base):
    __tablename__ = "agent_teams"
    __table_args__ = (UniqueConstraint("user_id", "client_request_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    template_key: Mapped[str] = mapped_column(String, default="custom")
    template_version: Mapped[str] = mapped_column(String, default="v1")
    status: Mapped[str] = mapped_column(String, default="provisioning", index=True)
    revision: Mapped[int] = mapped_column(default=1)
    client_request_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTeamMemberModel(Base):
    __tablename__ = "agent_team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "role_key"),
        Index("uq_agent_team_members_agent_id", "agent_id", unique=True),
    )

    team_id: Mapped[str] = mapped_column(
        ForeignKey("agent_teams.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), primary_key=True
    )
    role_key: Mapped[str] = mapped_column(String)
    member_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    display_order: Mapped[int] = mapped_column(default=0)


class SessionModel(Base):
    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, default="")
    account_id: Mapped[str] = mapped_column(String, default="")
    chat_id: Mapped[str] = mapped_column(String, default="")
    project_id: Mapped[str] = mapped_column(String, default="")
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String, default="")
    message_count: Mapped[int] = mapped_column(default=0)
    chatter_user_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AgentFileModel(Base):
    __tablename__ = "agent_files"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String, primary_key=True, default="")
    filename: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConfigModel(Base):
    __tablename__ = "configs"
    __table_args__ = (UniqueConstraint("kind", "scope", "scope_id", "name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    scope: Mapped[str] = mapped_column(String, default="system", index=True)
    scope_id: Mapped[str] = mapped_column(String, default="", index=True)
    user_id: Mapped[str] = mapped_column(String, default="", index=True)
    agent_id: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credential_key: Mapped[str] = mapped_column(String, default="", index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CronJobModel(Base):
    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, default="", index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    schedule: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String, default="")
    chat_id: Mapped[str] = mapped_column(String, default="")
    account_id: Mapped[str] = mapped_column(String, default="")
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ImportRunModel(Base):
    __tablename__ = "import_runs"

    source_sha256: Mapped[str] = mapped_column(String, primary_key=True)
    source_path: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict[str, Any]] = mapped_column(JSON)
