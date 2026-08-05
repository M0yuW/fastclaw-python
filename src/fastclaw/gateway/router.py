"""FastAPI routes for the first usable Gateway/Auth/API vertical slice."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from fastclaw.agent import AgentEvent, AgentEventType, AgentRunner, AgentRunRequest
from fastclaw.agent.persistence import DatabaseSessionPersistence
from fastclaw.execution import ExecutionContext
from fastclaw.gateway.models import (
    AgentCreate,
    APIKeyAgents,
    APIKeyCreate,
    ChatInput,
    LoginRequest,
    OnboardRequest,
    OpenAIChatInput,
    ProviderTest,
    ProviderUpdate,
    ProviderWrite,
    StoredProviderTest,
)
from fastclaw.gateway.service import (
    SESSION_COOKIE,
    AuthContext,
    GatewayService,
    mask_secret,
)
from fastclaw.gateway.settings import GatewaySettings
from fastclaw.identity import generate_api_key, hash_api_key, hash_password, use_identity
from fastclaw.providers import ChatMessage, ChatRequest, MessageRole, create_provider
from fastclaw.runtime import Runtime
from fastclaw.storage import (
    AgentRecord,
    APIKeyRecord,
    ConfigRecord,
    Database,
    UnitOfWork,
    UserRecord,
)
from fastclaw.tools import ToolRegistry


class Gateway:
    def __init__(
        self,
        settings: GatewaySettings,
        database: Database,
        runtime: Runtime,
    ) -> None:
        self.settings = settings
        self.database = database
        self.service = GatewayService(settings, database, runtime)


def _agent_json(agent: AgentRecord) -> dict[str, Any]:
    config = agent.config
    return {
        "id": agent.id,
        "name": agent.name,
        "description": str(config.get("description") or ""),
        "model": str(config.get("model") or ""),
        "workspace": str(config.get("workspace") or ""),
        "maxTokens": int(config.get("maxTokens") or 4096),
        "temperature": float(config.get("temperature") or 0.7),
        "maxToolIterations": int(config.get("maxToolIterations") or 8),
        "soul": str(config.get("soul") or ""),
        "userId": agent.user_id,
        "isPublic": agent.is_public,
        "avatarUrl": f"/api/agents/{agent.id}/files/avatar.png",
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _web_event(event: AgentEvent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "turnId": event.turn_id,
        "messageId": event.message_id,
        "round": event.round,
        "seq": event.seq,
    }
    if event.type is AgentEventType.CONTENT_DELTA:
        data["delta"] = event.content
    elif event.type is AgentEventType.CONTENT:
        data["content"] = event.content
    elif event.type is AgentEventType.TOOL_CALL and event.tool_call is not None:
        data.update(
            {
                "id": event.tool_call.id,
                "name": event.tool_call.function.name,
                "arguments": event.tool_call.function.arguments,
            }
        )
    elif event.type is AgentEventType.TOOL_RESULT:
        data["result"] = event.tool_result
    elif event.type is AgentEventType.ERROR:
        data["message"] = event.error
    return {"version": 2, "type": event.type.value, "data": data}


def create_gateway_router(gateway: Gateway) -> APIRouter:
    router = APIRouter()
    service = gateway.service

    async def require_auth(request: Request) -> AsyncIterator[AuthContext]:
        auth = await service.authenticate(request)
        with use_identity(auth.identity):
            yield auth

    auth_dependency = Depends(require_auth)

    @router.get("/", include_in_schema=False, response_model=None)
    async def root() -> Response | dict[str, Any]:
        if gateway.settings.web_root is not None:
            index = gateway.settings.web_root / "index.html"
            if index.is_file():
                return FileResponse(index)
        return {
            "service": "fastclaw-python",
            "version": "0.1.0",
            "status": "/api/status",
            "health": "/healthz",
            "ready": "/readyz",
            "docs": "/docs",
        }

    @router.get("/api/status")
    async def api_status(request: Request) -> dict[str, Any]:
        auth = await service.optional_auth(request)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            configured = await store.count_users() > 0
            owned_agents = (
                await store.list_agents(auth.identity.effective_user_id) if auth is not None else ()
            )
            agents = (
                tuple(agent for agent in owned_agents if auth.identity.can_access_agent(agent.id))
                if auth is not None
                else ()
            )
        resolved_agents = [(await service.agent_runtime_profile(agent)).agent for agent in agents]
        provider: dict[str, Any] | None = None
        if auth is not None and resolved_agents:
            try:
                selected = await service.provider_selection(auth, resolved_agents[0])
            except HTTPException:
                selected = None
            if selected is not None:
                provider = {
                    "name": selected.name,
                    "model": selected.model,
                    "apiBase": selected.api_base,
                    "apiKey": mask_secret(selected.api_key),
                }
        response: dict[str, Any] = {
            "configured": configured,
            "running": service.runtime.state.value == "running",
            "port": gateway.settings.port,
            "mode": "self-hosted",
            "uptime": str(datetime.now(UTC) - service.started_at).split(".", 1)[0],
            "agents": [_agent_json(agent) for agent in resolved_agents],
            "channels": [],
            "provider": provider,
        }
        if auth is not None:
            response.update(
                {
                    "userId": auth.identity.user_id,
                    "isAdmin": auth.identity.role == "super_admin",
                }
            )
        return response

    async def onboard_once(payload: OnboardRequest) -> dict[str, Any]:
        now = datetime.now(UTC)
        user_id = f"usr_{secrets.token_hex(10)}"
        agent_id = f"agt_{secrets.token_hex(10)}"
        provider_name = payload.provider or (
            payload.model.split("/", 1)[0] if "/" in payload.model else ""
        )
        model = payload.model
        if provider_name and model and "/" not in model:
            model = f"{provider_name}/{model}"
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            if await store.count_users() > 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "runtime is already configured")
            await store.save_user(
                UserRecord(
                    id=user_id,
                    username=payload.username,
                    email=payload.email,
                    password_hash=hash_password(payload.password),
                    display_name=payload.display_name,
                    role="super_admin",
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_agent(
                AgentRecord(
                    id=agent_id,
                    user_id=user_id,
                    name=payload.agent_name,
                    config={"model": model},
                    created_at=now,
                    updated_at=now,
                )
            )
            if provider_name and payload.api_base and payload.api_key:
                await store.save_config(
                    ConfigRecord(
                        id=f"cfg_{secrets.token_hex(10)}",
                        kind="provider",
                        scope="user",
                        scope_id=user_id,
                        user_id=user_id,
                        name=provider_name,
                        data={
                            "apiBase": payload.api_base,
                            "apiKey": payload.api_key,
                            "apiType": payload.api_type,
                            "authType": payload.auth_type,
                            "model": model,
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
            if payload.sandbox_enabled:
                await store.save_config(
                    ConfigRecord(
                        id=f"cfg_{secrets.token_hex(10)}",
                        kind="setting",
                        scope="user",
                        scope_id=user_id,
                        user_id=user_id,
                        name="sandbox",
                        data={
                            "enabled": True,
                            "backend": payload.sandbox_backend or "docker",
                            "image": payload.sandbox_image or "",
                            "e2bKey": payload.sandbox_e2b_key or "",
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
        return {"ok": True, "userId": user_id, "agentId": agent_id}

    @router.post("/api/onboard")
    async def onboard(payload: OnboardRequest) -> dict[str, Any]:
        async with service.onboard_lock:
            return await onboard_once(payload)

    @router.post("/api/login")
    async def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        user, session = await service.login(payload.login, payload.password)
        response.set_cookie(
            SESSION_COOKIE,
            session.sid,
            max_age=gateway.settings.session_ttl_seconds,
            httponly=True,
            secure=gateway.settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return {"ok": True, "user": service.public_user(user)}

    @router.post("/api/logout")
    async def logout(
        response: Response,
        auth: AuthContext = auth_dependency,
        sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> dict[str, bool]:
        del auth
        await service.revoke(sid or "")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.get("/api/me")
    async def me(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        return {
            "ok": True,
            "user": service.public_user(auth.user),
            "authMethod": auth.identity.auth_method,
            "actAsUserId": auth.identity.act_as_user_id,
            "readOnly": auth.identity.read_only,
        }

    @router.get("/api/agents")
    async def list_agents(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        async with UnitOfWork(gateway.database) as unit:
            agents = await unit.require_store().list_agents(auth.identity.effective_user_id)
        visible = [agent for agent in agents if auth.identity.can_access_agent(agent.id)]
        resolved = [(await service.agent_runtime_profile(agent)).agent for agent in visible]
        return {"agents": [_agent_json(agent) for agent in resolved]}

    @router.post("/api/agents", status_code=status.HTTP_201_CREATED)
    async def create_agent(
        payload: AgentCreate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if auth.identity.read_only or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential cannot create agents")
        now = datetime.now(UTC)
        agent = AgentRecord(
            id=f"agt_{secrets.token_hex(10)}",
            user_id=auth.identity.effective_user_id,
            name=payload.name,
            config={"description": payload.description, "model": payload.model},
            created_at=now,
            updated_at=now,
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_agent(agent)
        return {"ok": True, "agent": _agent_json(agent)}

    @router.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str, auth: AuthContext = auth_dependency) -> dict[str, Any]:
        agent = await service.require_agent(auth, agent_id)
        return {"agent": _agent_json((await service.agent_runtime_profile(agent)).agent)}

    async def require_owned_api_key(auth: AuthContext, api_key_id: str) -> APIKeyRecord:
        if auth.identity.auth_method == "apikey" or auth.identity.read_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential cannot manage API keys")
        async with UnitOfWork(gateway.database) as unit:
            record = await unit.require_store().get_api_key(api_key_id)
        if record is None or record.user_id != auth.identity.effective_user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
        return record

    async def validate_api_key_agents(auth: AuthContext, agent_ids: list[str]) -> list[str]:
        unique = list(dict.fromkeys(agent_ids))
        for agent_id in unique:
            await service.require_agent(auth, agent_id)
        return unique

    async def api_key_json(record: APIKeyRecord) -> dict[str, Any]:
        async with UnitOfWork(gateway.database) as unit:
            agents = await unit.require_store().list_api_key_agents(record.id)
        return {
            "id": record.id,
            "userId": record.user_id,
            "name": record.name,
            "key": f"{record.key_prefix}****",
            "agents": list(agents),
            "createdAt": record.created_at.isoformat(),
        }

    @router.get("/api/apikeys")
    async def list_api_keys(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        if auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "API keys cannot enumerate keys")
        async with UnitOfWork(gateway.database) as unit:
            records = await unit.require_store().list_api_keys(auth.identity.effective_user_id)
        return {"apikeys": [await api_key_json(record) for record in records]}

    @router.post("/api/apikeys", status_code=status.HTTP_201_CREATED)
    async def create_api_key(
        payload: APIKeyCreate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if not payload.name.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "API key name is required")
        if auth.identity.auth_method == "apikey" or auth.identity.read_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential cannot manage API keys")
        agent_ids = await validate_api_key_agents(auth, payload.agent_ids)
        token = generate_api_key()
        record = APIKeyRecord(
            id=f"key_{secrets.token_hex(10)}",
            user_id=auth.identity.effective_user_id,
            name=payload.name.strip(),
            key_hash=hash_api_key(token),
            key_prefix=token[:12],
        )
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            await store.save_api_key(record)
            await store.set_api_key_agents(record.id, agent_ids)
        return {"ok": True, "apikey": await api_key_json(record), "token": token}

    @router.delete("/api/apikeys/{api_key_id}")
    async def delete_api_key(
        api_key_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        await require_owned_api_key(auth, api_key_id)
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().delete_api_key(api_key_id)
        return {"ok": True}

    @router.post("/api/apikeys/{api_key_id}/rotate")
    async def rotate_api_key(
        api_key_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        record = await require_owned_api_key(auth, api_key_id)
        token = generate_api_key()
        rotated = record.model_copy(
            update={"key_hash": hash_api_key(token), "key_prefix": token[:12]}
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_api_key(rotated)
        return {"ok": True, "apikey": await api_key_json(rotated), "token": token}

    @router.put("/api/apikeys/{api_key_id}/agents")
    async def set_api_key_agents(
        api_key_id: str,
        payload: APIKeyAgents,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        await require_owned_api_key(auth, api_key_id)
        agent_ids = await validate_api_key_agents(auth, payload.agent_ids)
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().set_api_key_agents(api_key_id, agent_ids)
        return {"ok": True}

    async def provider_records(auth: AuthContext, agent_id: str = "") -> list[ConfigRecord]:
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            system = await store.list_configs(kind="provider", user_id="", agent_id="")
            user = await store.list_configs(
                kind="provider", user_id=auth.identity.effective_user_id, agent_id=""
            )
            agent = (
                await store.list_configs(
                    kind="provider",
                    user_id=auth.identity.effective_user_id,
                    agent_id=agent_id,
                )
                if agent_id
                else ()
            )
        merged: dict[str, ConfigRecord] = {}
        for layer in (system, user, agent):
            merged.update({record.name: record for record in layer if record.enabled})
        return list(merged.values())

    @router.get("/api/providers")
    async def list_providers(
        scope: str = "",
        scopeId: str = "",
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        if scope:
            if scope not in {"system", "user", "agent"}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid provider scope")
            if scope == "system":
                if auth.identity.role != "super_admin":
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
                scope_user_id, scope_agent_id = "", ""
            elif scope == "user":
                target = scopeId or auth.identity.effective_user_id
                if target != auth.identity.effective_user_id:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-tenant scope denied")
                scope_user_id, scope_agent_id = target, ""
            else:
                await service.require_agent(auth, scopeId)
                scope_user_id, scope_agent_id = auth.identity.effective_user_id, scopeId
            async with UnitOfWork(gateway.database) as unit:
                records = list(
                    await unit.require_store().list_configs(
                        kind="provider", user_id=scope_user_id, agent_id=scope_agent_id
                    )
                )
        else:
            records = await provider_records(auth)
        return {
            "providers": [
                {
                    "id": item.id,
                    "name": item.name,
                    "scope": item.scope,
                    "scopeId": item.scope_id,
                    "enabled": item.enabled,
                    "apiBase": str(item.data.get("apiBase") or ""),
                    "apiType": str(item.data.get("apiType") or "openai-compatible"),
                    "authType": str(item.data.get("authType") or "bearer-token"),
                    "apiKey": mask_secret(str(item.data.get("apiKey") or "")),
                    "models": item.data.get("models") or [],
                    "updatedAt": item.updated_at.isoformat(),
                }
                for item in records
            ]
        }

    @router.post("/api/providers", status_code=status.HTTP_201_CREATED)
    async def save_provider(
        payload: ProviderWrite, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if auth.identity.read_only or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential is read-only")
        if payload.scope == "system":
            if auth.identity.role != "super_admin":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
            scope_id = ""
        elif payload.scope == "agent":
            scope_id = payload.scope_id
            await service.require_agent(auth, scope_id)
        else:
            scope_id = payload.scope_id or auth.identity.effective_user_id
            if scope_id != auth.identity.effective_user_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-tenant provider scope denied")
        now = datetime.now(UTC)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            existing = await store.find_config(
                kind="provider", scope=payload.scope, scope_id=scope_id, name=payload.name
            )
        record = ConfigRecord(
            id=existing.id if existing is not None else f"cfg_{secrets.token_hex(10)}",
            kind="provider",
            scope=payload.scope,
            scope_id=scope_id,
            user_id=auth.identity.effective_user_id,
            agent_id=scope_id if payload.scope == "agent" else "",
            name=payload.name,
            data={
                "apiBase": payload.api_base,
                "apiKey": payload.api_key,
                "apiType": payload.api_type,
                "authType": payload.auth_type,
                "models": payload.models,
            },
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_config(record)
        return {"ok": True, "provider": {"id": record.id, "name": record.name}}

    async def require_provider_record(auth: AuthContext, provider_id: str) -> ConfigRecord:
        async with UnitOfWork(gateway.database) as unit:
            record = await unit.require_store().get_config(provider_id)
        if record is None or record.kind != "provider":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
        if record.scope == "system" and auth.identity.role != "super_admin":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
        if record.scope == "user" and record.scope_id != auth.identity.effective_user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
        if record.scope == "agent":
            await service.require_agent(auth, record.scope_id)
        return record

    @router.put("/api/providers/{provider_id}")
    async def update_provider(
        provider_id: str,
        payload: ProviderUpdate,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        if auth.identity.read_only or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential is read-only")
        record = await require_provider_record(auth, provider_id)
        patch = payload.model_dump(exclude_unset=True, by_alias=True)
        enabled = bool(patch.pop("enabled", record.enabled))
        name = str(patch.pop("name", record.name))
        data = dict(record.data)
        if patch.get("apiKey", None) == "":
            patch.pop("apiKey")
        data.update(patch)
        updated = record.model_copy(
            update={"name": name, "enabled": enabled, "data": data, "updated_at": datetime.now(UTC)}
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_config(updated)
        return {"ok": True}

    @router.delete("/api/providers/{provider_id}")
    async def delete_provider(
        provider_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if auth.identity.read_only or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential is read-only")
        await require_provider_record(auth, provider_id)
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().delete_config(provider_id)
        return {"ok": True}

    async def run_provider_test(
        *, name: str, api_base: str, api_key: str, api_type: str, model: str
    ) -> dict[str, Any]:
        provider = create_provider(
            name=name or "test", api_key=api_key, api_base=api_base, api_type=api_type
        )
        await provider.start(service.runtime.http_client)
        try:
            await provider.chat(
                ChatRequest(
                    model=model,
                    messages=(ChatMessage(role=MessageRole.USER, content="Reply with OK."),),
                )
            )
        except Exception as exc:
            reason = str(exc).replace("\n", " ")[:240]
            return {"ok": False, "error": f"{type(exc).__name__}: {reason}"}
        finally:
            await provider.stop()
        return {"ok": True}

    @router.post("/api/test-provider")
    async def test_provider(payload: ProviderTest) -> dict[str, Any]:
        return await run_provider_test(
            name="test",
            api_base=payload.api_base,
            api_key=payload.api_key,
            api_type=payload.api_type,
            model=payload.model,
        )

    @router.post("/api/providers/{provider_id}/test")
    async def test_stored_provider(
        provider_id: str,
        payload: StoredProviderTest,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        record = await require_provider_record(auth, provider_id)
        return await run_provider_test(
            name=record.name,
            api_base=str(record.data.get("apiBase") or ""),
            api_key=str(record.data.get("apiKey") or ""),
            api_type=str(record.data.get("apiType") or "openai-compatible"),
            model=payload.model,
        )

    @router.get("/api/config")
    async def get_config(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        records = await provider_records(auth)
        providers = {
            item.name: {
                "apiKey": mask_secret(str(item.data.get("apiKey") or "")),
                "apiBase": str(item.data.get("apiBase") or ""),
                "apiType": str(item.data.get("apiType") or "openai-compatible"),
                "authType": str(item.data.get("authType") or "bearer-token"),
                "models": item.data.get("models") or [],
            }
            for item in records
        }
        return {
            "providers": providers,
            "agents": {
                "defaults": {
                    "model": gateway.settings.default_model,
                    "maxTokens": 4096,
                    "temperature": 0.7,
                    "maxToolIterations": 8,
                },
                "list": [],
            },
            "channels": {},
            "storage": {"type": "sqlite"},
            "hooks": {"enabled": False},
        }

    async def prepare_runner(
        auth: AuthContext, agent_id: str, session_id: str, requested_model: str = ""
    ) -> tuple[Any, AgentRunner, AgentRunRequest, ExecutionContext]:
        agent = await service.require_agent(auth, agent_id)
        profile = await service.agent_runtime_profile(agent)
        agent = profile.agent
        selection = await service.provider_selection(auth, agent, requested_model)
        provider = await service.create_provider(selection)
        runner = AgentRunner(
            provider,
            ToolRegistry(),
            DatabaseSessionPersistence(gateway.database),
        )
        request = AgentRunRequest(
            model=selection.model,
            message="",
            system_prompt=profile.system_prompt or str(agent.config.get("soul") or ""),
            max_rounds=int(agent.config.get("maxToolIterations") or 8),
        )
        context = ExecutionContext(
            user_id=auth.identity.effective_user_id,
            agent_id=agent.id,
            session_id=session_id,
            root_execution_id=f"run_{uuid4().hex}",
        )
        return provider, runner, request, context

    @router.get("/api/chat/history")
    async def chat_history(
        agentId: str,
        sessionId: str,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        await service.require_agent(auth, agentId)
        async with UnitOfWork(gateway.database) as unit:
            session = await unit.require_store().get_session(
                auth.identity.effective_user_id, agentId, sessionId
            )
        return {"history": session.messages if session is not None else []}

    @router.get("/api/chat/sessions")
    async def chat_sessions(agentId: str, auth: AuthContext = auth_dependency) -> dict[str, Any]:
        await service.require_agent(auth, agentId)
        async with UnitOfWork(gateway.database) as unit:
            sessions = await unit.require_store().list_sessions(
                auth.identity.effective_user_id, agentId
            )
        return {
            "sessions": [
                {
                    "id": item.key,
                    "title": item.title,
                    "preview": next(
                        (
                            str(message.get("content") or "")[:160]
                            for message in reversed(item.messages)
                            if message.get("role") == "assistant"
                        ),
                        "",
                    ),
                    "createdAt": int(item.created_at.timestamp() * 1000),
                    "updatedAt": int(item.updated_at.timestamp() * 1000),
                }
                for item in sessions
            ]
        }

    @router.post("/api/chat")
    async def chat(payload: ChatInput, auth: AuthContext = auth_dependency) -> dict[str, str]:
        provider, runner, template, context = await prepare_runner(
            auth, payload.agent_id, payload.session_id
        )
        try:
            message = await runner.chat(
                template.model_copy(update={"message": payload.message}), context
            )
            return {"response": str(message.content or "")}
        finally:
            await provider.stop()

    @router.post("/api/chat/stream")
    async def chat_stream(
        payload: ChatInput, auth: AuthContext = auth_dependency
    ) -> StreamingResponse:
        provider, runner, template, context = await prepare_runner(
            auth, payload.agent_id, payload.session_id
        )

        async def events() -> AsyncIterator[str]:
            stream = runner.stream(
                template.model_copy(update={"message": payload.message}), context
            )
            try:
                async for event in stream:
                    yield _sse(_web_event(event))
            finally:
                await stream.aclose()
                await provider.stop()

        return StreamingResponse(events(), media_type="text/event-stream")

    @router.get("/v1/agents")
    async def v1_agents(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        result = await list_agents(auth)
        return {"object": "list", "data": result["agents"]}

    @router.post("/v1/chat/completions")
    async def v1_chat(
        payload: OpenAIChatInput,
        request: Request,
        auth: AuthContext = auth_dependency,
    ) -> Response:
        if not payload.messages:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "messages is required")
        message = next(
            (item.content for item in reversed(payload.messages) if item.role == "user"), ""
        )
        if not message:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "a user message is required")
        agent_id = payload.agent_id or request.headers.get("x-fastclaw-agent-id", "")
        if not agent_id:
            agents = await list_agents(auth)
            data = agents["agents"]
            agent_id = str(data[0]["id"]) if data else ""
        if not agent_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
        session_id = request.headers.get("x-fastclaw-session-key") or f"api-{uuid4().hex}"
        provider, runner, template, context = await prepare_runner(
            auth, agent_id, session_id, payload.model
        )
        completion_id = f"chatcmpl-{uuid4().hex}"
        if payload.stream:

            async def chunks() -> AsyncIterator[str]:
                stream = runner.stream(template.model_copy(update={"message": message}), context)
                try:
                    async for event in stream:
                        if event.type is AgentEventType.CONTENT_DELTA:
                            yield _sse(
                                {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": template.model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": event.content},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                            )
                    yield "data: [DONE]\n\n"
                finally:
                    await stream.aclose()
                    await provider.stop()

            return StreamingResponse(chunks(), media_type="text/event-stream")
        try:
            result = await runner.chat(template.model_copy(update={"message": message}), context)
        finally:
            await provider.stop()
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": template.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.content or ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    return router
