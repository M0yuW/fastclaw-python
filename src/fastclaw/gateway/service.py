"""Authentication, ACL, and provider-resolution services for the gateway."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status

from fastclaw.gateway.models import ProviderSelection
from fastclaw.gateway.settings import GatewaySettings
from fastclaw.identity import Identity, hash_api_key, verify_password
from fastclaw.providers import Provider, create_provider
from fastclaw.runtime import Runtime
from fastclaw.storage import AgentRecord, ConfigRecord, Database, UnitOfWork, UserRecord
from fastclaw.storage.records import WebSessionRecord

SESSION_COOKIE = "fastclaw_session"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


@dataclass(frozen=True, slots=True)
class AuthContext:
    identity: Identity
    user: UserRecord


class GatewayService:
    def __init__(self, settings: GatewaySettings, database: Database, runtime: Runtime) -> None:
        self.settings = settings
        self.database = database
        self.runtime = runtime
        self.started_at = datetime.now(UTC)
        self.onboard_lock = asyncio.Lock()

    async def optional_auth(self, request: Request) -> AuthContext | None:
        try:
            return await self.authenticate(request)
        except HTTPException:
            return None

    async def authenticate(self, request: Request) -> AuthContext:
        authorization = request.headers.get("authorization", "")
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            if authorization.lower().startswith("bearer "):
                token = authorization[7:].strip()
                key = await store.get_api_key_by_hash(hash_api_key(token)) if token else None
                if key is None:
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
                user = await store.get_user(key.user_id)
                if user is None or user.status != "active":
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "inactive API-key owner")
                agents = tuple(await store.list_api_key_agents(key.id))
                identity = Identity(
                    user_id=user.id,
                    # An agent-scoped key owned by an administrator must not
                    # inherit the owner's platform-wide session authority.
                    role="super_admin" if key.type == "admin" else "user",
                    auth_method="apikey",
                    api_key_id=key.id,
                    api_key_agents=agents,
                )
                return AuthContext(identity, user)

            sid = request.cookies.get(SESSION_COOKIE, "")
            session = await store.get_web_session(sid) if sid else None
            if session is None or _utc(session.expires_at) <= datetime.now(UTC):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
            user = await store.get_user(session.user_id)
            if user is None or user.status != "active":
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "inactive session owner")
            return AuthContext(
                Identity(user_id=user.id, role=user.role, auth_method="cookie"), user
            )

    async def login(self, login: str, password: str) -> tuple[UserRecord, WebSessionRecord]:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            user = await store.get_user_by_login(login)
            if (
                user is None
                or user.status != "active"
                or not verify_password(password, user.password_hash)
            ):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
            now = datetime.now(UTC)
            session = WebSessionRecord(
                sid=secrets.token_urlsafe(32),
                user_id=user.id,
                created_at=now,
                expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds),
            )
            await store.save_web_session(session)
            return user, session

    async def revoke(self, sid: str) -> None:
        if not sid:
            return
        async with UnitOfWork(self.database) as unit:
            await unit.require_store().delete_web_session(sid)

    async def require_agent(self, auth: AuthContext, agent_id: str) -> AgentRecord:
        async with UnitOfWork(self.database) as unit:
            agent = await unit.require_store().get_agent(agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
        identity = auth.identity
        if identity.role != "super_admin" and agent.user_id != identity.effective_user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
        if not identity.can_access_agent(agent.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
        return agent

    async def provider_selection(
        self, auth: AuthContext, agent: AgentRecord, requested_model: str = ""
    ) -> ProviderSelection:
        model = requested_model or str(agent.config.get("model") or self.settings.default_model)
        provider_name = model.split("/", 1)[0] if "/" in model else self.settings.provider_name
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            layers = [
                await store.list_configs(kind="provider", user_id="", agent_id=""),
                await store.list_configs(
                    kind="provider", user_id=auth.identity.effective_user_id, agent_id=""
                ),
                await store.list_configs(
                    kind="provider",
                    user_id=auth.identity.effective_user_id,
                    agent_id=agent.id,
                ),
            ]
        configs: dict[str, ConfigRecord] = {}
        for layer in layers:
            configs.update({item.name: item for item in layer if item.enabled})
        selected = configs.get(provider_name)
        if selected is not None:
            data = selected.data
            api_key = str(data.get("apiKey") or "")
            api_base = str(data.get("apiBase") or "")
            api_type = str(data.get("apiType") or "openai-compatible")
            configured_model = str(data.get("model") or "")
            model = model or configured_model
            if api_key and api_base and model:
                return ProviderSelection(
                    name=selected.name,
                    api_key=api_key,
                    api_base=api_base,
                    api_type=api_type,
                    model=model,
                    source=selected.scope,
                    config_id=selected.id,
                )
        if (
            self.settings.provider_name
            and self.settings.provider_api_key
            and self.settings.provider_api_base
            and model
            and (not provider_name or provider_name == self.settings.provider_name)
        ):
            return ProviderSelection(
                name=self.settings.provider_name,
                api_key=self.settings.provider_api_key,
                api_base=self.settings.provider_api_base,
                api_type=self.settings.provider_api_type,
                model=model,
                source="environment",
            )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no usable provider is configured for this agent",
        )

    async def create_provider(self, selection: ProviderSelection) -> Provider:
        provider = create_provider(
            name=selection.name,
            api_key=selection.api_key,
            api_base=selection.api_base,
            api_type=selection.api_type,
        )
        await provider.start(self.runtime.http_client)
        return provider

    @staticmethod
    def public_user(user: UserRecord) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "displayName": user.display_name,
            "role": user.role,
            "status": user.status,
        }
