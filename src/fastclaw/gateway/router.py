"""FastAPI routes for the first usable Gateway/Auth/API vertical slice."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from fastclaw.agent import AgentEvent, AgentEventType
from fastclaw.agent.manager import AgentRuntimeManager
from fastclaw.gateway.models import (
    AdminUserCreate,
    AdminUserUpdate,
    AgentCreate,
    AgentUpdate,
    APIKeyAgents,
    APIKeyCreate,
    ChatInput,
    LoginRequest,
    OnboardRequest,
    OpenAIChatInput,
    PasswordReset,
    PluginUpdate,
    ProviderTest,
    ProviderUpdate,
    ProviderWrite,
    SessionUpdate,
    StoredProviderTest,
    SystemFileWrite,
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
from fastclaw.skills import SkillError
from fastclaw.storage import (
    AgentFileRecord,
    AgentRecord,
    APIKeyRecord,
    ConfigRecord,
    Database,
    UnitOfWork,
    UserRecord,
)

_SYSTEM_FILES = frozenset(
    {
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "TOOLS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "MEMORY.md",
        "AGENTS.md",
    }
)
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class Gateway:
    def __init__(
        self,
        settings: GatewaySettings,
        database: Database,
        runtime: Runtime,
        agent_manager: AgentRuntimeManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.agent_manager = agent_manager
        self.service = GatewayService(settings, database, runtime, agent_manager)


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


def _safe_child(root: Path, relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid file path")
    root = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file path escapes workspace")
    return candidate


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _find_plaintext_credentials(value: Any, path: str = "") -> list[str]:
    sensitive = {"apikey", "bottoken", "apptoken", "token", "password", "secret"}
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if (
                str(key).lower() in sensitive
                and isinstance(item, str)
                and item
                and "****" not in item
            ):
                found.append(child)
            else:
                found.extend(_find_plaintext_credentials(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_plaintext_credentials(item, f"{path}[{index}]"))
    return found


def _drop_credentials(value: Any) -> Any:
    sensitive = {"apikey", "bottoken", "apptoken", "token", "password", "secret"}
    if isinstance(value, dict):
        return {
            key: _drop_credentials(item)
            for key, item in value.items()
            if str(key).lower() not in sensitive
        }
    if isinstance(value, list):
        return [_drop_credentials(item) for item in value]
    return value


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
        if event.tool_call is not None:
            data["id"] = event.tool_call.id
            data["name"] = event.tool_call.function.name
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

    def require_mutation(auth: AuthContext, *, admin: bool = False) -> None:
        if auth.identity.read_only or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "credential is read-only")
        if admin and auth.identity.role != "super_admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")

    async def require_mutable_agent(auth: AuthContext, agent_id: str) -> AgentRecord:
        require_mutation(auth)
        agent = await service.require_agent(auth, agent_id)
        if agent.user_id != auth.identity.effective_user_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "cross-tenant Agent access is read-only; use actAs for inspection",
            )
        return agent

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
            if provider_name and payload.api_base:
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
        return {
            "ok": True,
            "userId": user_id,
            "agentId": agent_id,
            "providerCredentialStored": False,
        }

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

    @router.get("/api/admin/users")
    async def admin_users(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        async with UnitOfWork(gateway.database) as unit:
            users = await unit.require_store().list_users()
        return {"users": [service.public_user(user) for user in users]}

    @router.post("/api/admin/users", status_code=status.HTTP_201_CREATED)
    async def admin_create_user(
        payload: AdminUserCreate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        now = datetime.now(UTC)
        record = UserRecord(
            id=f"usr_{secrets.token_hex(10)}",
            username=payload.username.strip(),
            email=payload.email.strip(),
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            role=payload.role,
            created_at=now,
            updated_at=now,
        )
        if not record.username or not record.email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "username and email are required")
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            if await store.get_user_by_login(record.username) or await store.get_user_by_login(
                record.email
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "username or email already exists")
            await store.save_user(record)
        return {"ok": True, "user": service.public_user(record)}

    async def require_admin_user(auth: AuthContext, user_id: str) -> UserRecord:
        require_mutation(auth, admin=True)
        async with UnitOfWork(gateway.database) as unit:
            user = await unit.require_store().get_user(user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        return user

    @router.put("/api/admin/users/{user_id}")
    async def admin_update_user(
        user_id: str, payload: AdminUserUpdate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        record = await require_admin_user(auth, user_id)
        patch = payload.model_dump(exclude_unset=True)
        if user_id == auth.identity.user_id and patch.get("status") == "disabled":
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot disable the current user")
        if user_id == auth.identity.user_id and patch.get("role") not in {None, "super_admin"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot demote the current user")
        updated = record.model_copy(update={**patch, "updated_at": datetime.now(UTC)})
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_user(updated)
        return {"ok": True, "user": service.public_user(updated)}

    @router.post("/api/admin/users/{user_id}/password")
    async def admin_reset_password(
        user_id: str, payload: PasswordReset, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        record = await require_admin_user(auth, user_id)
        updated = record.model_copy(
            update={
                "password_hash": hash_password(payload.password),
                "updated_at": datetime.now(UTC),
            }
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_user(updated)
        return {"ok": True}

    @router.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(
        user_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        await require_admin_user(auth, user_id)
        if user_id == auth.identity.user_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot delete the current user")
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            agents = await store.list_agents(user_id)
            await store.delete_user(user_id)
        for agent in agents:
            gateway.agent_manager.remove_profile(agent.id)
        return {"ok": True}

    @router.get("/api/admin/agents")
    async def admin_agents(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            users = await store.list_users()
            agents = [agent for user in users for agent in await store.list_agents(user.id)]
        resolved = [(await service.agent_runtime_profile(agent)).agent for agent in agents]
        owners = {user.id: user.username for user in users}
        return {
            "agents": [
                {**_agent_json(agent), "ownerUsername": owners.get(agent.user_id, "")}
                for agent in resolved
            ]
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

    @router.get("/api/agents/{agent_id}/config")
    async def get_agent_config(
        agent_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        agent = await service.require_agent(auth, agent_id)
        async with UnitOfWork(gateway.database) as unit:
            raw = await unit.require_store().get_agent_file(agent.id, agent.user_id, "agent.json")
        if raw is not None:
            try:
                parsed = json.loads(raw.data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                return {**parsed, **agent.config}
        return dict(agent.config)

    @router.put("/api/agents/{agent_id}")
    async def update_agent(
        agent_id: str, payload: AgentUpdate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        agent = await require_mutable_agent(auth, agent_id)
        values = payload.model_dump(exclude_unset=True, by_alias=True)
        if _find_plaintext_credentials(values):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Agent credentials must use declared environment variables",
            )
        values = _drop_credentials(values)
        name = str(values.pop("name", agent.name)).strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent name is required")
        config = dict(agent.config)
        if values.get("policy") == "custom":
            values.pop("policy")
            config.pop("policy", None)
        config.update(values)
        updated = agent.model_copy(
            update={"name": name, "config": config, "updated_at": datetime.now(UTC)}
        )
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_agent(updated)
        resolved = (await service.agent_runtime_profile(updated)).agent
        return {"ok": True, "agent": _agent_json(resolved)}

    @router.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str, auth: AuthContext = auth_dependency) -> dict[str, Any]:
        await require_mutable_agent(auth, agent_id)
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().delete_agent(agent_id)
        gateway.agent_manager.remove_profile(agent_id)
        return {"ok": True, "assetsRetained": True}

    @router.get("/api/agents/{agent_id}/system-files/{filename}")
    async def get_system_file(
        agent_id: str, filename: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if filename not in _SYSTEM_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "system file not found")
        agent = await service.require_agent(auth, agent_id)
        async with UnitOfWork(gateway.database) as unit:
            override = await unit.require_store().get_agent_file(agent.id, agent.user_id, filename)
        base_path = gateway.settings.data_root / "agents" / agent.id / filename
        base = base_path.read_text(encoding="utf-8") if base_path.is_file() else ""
        if override is not None:
            return {
                "content": override.data.decode("utf-8"),
                "source": "db",
                "baseContent": base,
            }
        return {"content": base, "source": "fs" if base else "default"}

    @router.put("/api/agents/{agent_id}/system-files/{filename}")
    async def put_system_file(
        agent_id: str,
        filename: str,
        payload: SystemFileWrite,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        if filename not in _SYSTEM_FILES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported system file")
        agent = await require_mutable_agent(auth, agent_id)
        now = datetime.now(UTC)
        updated = agent.model_copy(update={"updated_at": now})
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            await store.save_agent_file(
                AgentFileRecord(
                    agent_id=agent.id,
                    user_id=agent.user_id,
                    filename=filename,
                    data=payload.content.encode("utf-8"),
                    updated_at=now,
                )
            )
            await store.save_agent(updated)
        await gateway.agent_manager.reload_profile(updated)
        return {"ok": True}

    @router.delete("/api/agents/{agent_id}/system-files/{filename}")
    async def delete_system_file(
        agent_id: str, filename: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        if filename not in _SYSTEM_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "system file not found")
        agent = await require_mutable_agent(auth, agent_id)
        now = datetime.now(UTC)
        updated = agent.model_copy(update={"updated_at": now})
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            await store.delete_agent_file(agent.id, agent.user_id, filename)
            await store.save_agent(updated)
        await gateway.agent_manager.reload_profile(updated)
        return {"ok": True}

    @router.get("/api/agents/{agent_id}/files")
    async def list_agent_workspace_files(
        agent_id: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        await service.require_agent(auth, agent_id)
        root = gateway.settings.data_root / "workspaces" / agent_id
        files: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                stat_result = path.stat()
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": stat_result.st_size,
                        "modTime": int(stat_result.st_mtime * 1000),
                    }
                )
        return {"files": files}

    @router.post("/api/agents/{agent_id}/files")
    async def upload_agent_workspace_files(
        agent_id: str,
        request: Request,
        sessionId: str = "",
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        await require_mutable_agent(auth, agent_id)
        form = await request.form()
        uploads = [item for item in form.getlist("file") if isinstance(item, StarletteUploadFile)]
        if not uploads:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files supplied")
        root = gateway.settings.data_root / "workspaces" / agent_id
        base = _safe_child(root, f"sessions/{sessionId}") if sessionId else root.resolve()
        written: list[dict[str, Any]] = []
        total = 0
        for upload in uploads:
            filename = Path(upload.filename or "").name
            if not filename or filename != (upload.filename or ""):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid upload filename")
            data = await upload.read(_MAX_UPLOAD_BYTES + 1)
            total += len(data)
            if len(data) > _MAX_UPLOAD_BYTES or total > _MAX_UPLOAD_BYTES:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "upload is too large")
            target = _safe_child(base, filename)
            _write_atomic(target, data)
            written.append(
                {"path": target.relative_to(root.resolve()).as_posix(), "size": len(data)}
            )
        return {"ok": True, "files": written}

    @router.get("/api/agents/{agent_id}/files/{filename:path}", response_model=None)
    async def get_agent_workspace_file(
        agent_id: str, filename: str, auth: AuthContext = auth_dependency
    ) -> Response:
        await service.require_agent(auth, agent_id)
        target = _safe_child(gateway.settings.data_root / "workspaces" / agent_id, filename)
        if not target.is_file() or target.is_symlink():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
        return FileResponse(target)

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
            agent = await service.require_agent(auth, agent_id)
            if agent.user_id != auth.identity.effective_user_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-tenant Agent ACL denied")
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
                    "apiKey": mask_secret(gateway.agent_manager.provider_credential(item.name)),
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
        if payload.api_key:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"set FASTCLAW_PROVIDER_{payload.name.upper().replace('-', '_')}_API_KEY instead",
            )
        if payload.scope == "system":
            if auth.identity.role != "super_admin":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
            scope_id = ""
        elif payload.scope == "agent":
            scope_id = payload.scope_id
            await require_mutable_agent(auth, scope_id)
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

    async def require_mutable_provider(auth: AuthContext, provider_id: str) -> ConfigRecord:
        require_mutation(auth)
        record = await require_provider_record(auth, provider_id)
        if record.scope != "system" and record.user_id != auth.identity.effective_user_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-tenant provider access denied")
        return record

    @router.put("/api/providers/{provider_id}")
    async def update_provider(
        provider_id: str,
        payload: ProviderUpdate,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        record = await require_mutable_provider(auth, provider_id)
        patch = payload.model_dump(exclude_unset=True, by_alias=True)
        enabled = bool(patch.pop("enabled", record.enabled))
        name = str(patch.pop("name", record.name))
        data = dict(record.data)
        data.pop("apiKey", None)
        requested_key = str(patch.pop("apiKey", ""))
        if requested_key and "****" not in requested_key:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"set FASTCLAW_PROVIDER_{record.name.upper().replace('-', '_')}_API_KEY instead",
            )
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
        await require_mutable_provider(auth, provider_id)
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
        record = await require_mutable_provider(auth, provider_id)
        return await run_provider_test(
            name=record.name,
            api_base=str(record.data.get("apiBase") or ""),
            api_key=gateway.agent_manager.provider_credential(record.name),
            api_type=str(record.data.get("apiType") or "openai-compatible"),
            model=payload.model,
        )

    @router.get("/api/config")
    async def get_config(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        records = await provider_records(auth)
        providers = {
            item.name: {
                "apiKey": mask_secret(gateway.agent_manager.provider_credential(item.name)),
                "apiBase": str(item.data.get("apiBase") or ""),
                "apiType": str(item.data.get("apiType") or "openai-compatible"),
                "authType": str(item.data.get("authType") or "bearer-token"),
                "models": item.data.get("models") or [],
            }
            for item in records
        }
        async with UnitOfWork(gateway.database) as unit:
            stored = await unit.require_store().find_config(
                kind="setting", scope="system", scope_id="", name="system.config"
            )
        result: dict[str, Any] = {
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
        if stored is not None:
            result = _deep_merge(result, stored.data)
            result["providers"] = providers
        return result

    @router.post("/api/config")
    async def save_config(
        payload: dict[str, Any], auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        forbidden = _find_plaintext_credentials(payload)
        if forbidden:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "credentials must use FASTCLAW_PROVIDER_* or declared skill environment variables",
            )
        now = datetime.now(UTC)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            current = await store.find_config(
                kind="setting", scope="system", scope_id="", name="system.config"
            )
            safe_payload = _drop_credentials(payload)
            data = _deep_merge(current.data if current else {}, safe_payload)
            await store.save_config(
                ConfigRecord(
                    id=current.id if current else f"cfg_{secrets.token_hex(10)}",
                    kind="setting",
                    scope="system",
                    scope_id="",
                    name="system.config",
                    data=data,
                    created_at=current.created_at if current else now,
                    updated_at=now,
                )
            )
            agents_config = safe_payload.get("agents")
            defaults = agents_config.get("defaults") if isinstance(agents_config, dict) else None
            if isinstance(defaults, dict):
                current_defaults = await store.find_config(
                    kind="setting", scope="system", scope_id="", name="agents.defaults"
                )
                await store.save_config(
                    ConfigRecord(
                        id=(
                            current_defaults.id
                            if current_defaults is not None
                            else f"cfg_{secrets.token_hex(10)}"
                        ),
                        kind="setting",
                        scope="system",
                        scope_id="",
                        name="agents.defaults",
                        data=_deep_merge(
                            current_defaults.data if current_defaults is not None else {},
                            defaults,
                        ),
                        created_at=current_defaults.created_at if current_defaults else now,
                        updated_at=now,
                    )
                )
        await gateway.agent_manager.reload_profiles()
        return {"ok": True}

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

    @router.put("/api/chat/sessions/{session_id}")
    async def rename_chat_session(
        session_id: str, payload: SessionUpdate, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        require_mutation(auth)
        await service.require_agent(auth, payload.agent_id)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            record = await store.get_session(
                auth.identity.effective_user_id, payload.agent_id, session_id
            )
            if record is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
            await store.save_session(
                record.model_copy(
                    update={"title": payload.title.strip(), "updated_at": datetime.now(UTC)}
                )
            )
        return {"ok": True}

    @router.delete("/api/chat/sessions/{session_id}")
    async def delete_chat_session(
        session_id: str, agentId: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        require_mutation(auth)
        await service.require_agent(auth, agentId)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            record = await store.get_session(auth.identity.effective_user_id, agentId, session_id)
            if record is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
            await store.delete_session(auth.identity.effective_user_id, agentId, session_id)
        return {"ok": True}

    def skill_json(skill: Any, *, location: str = "global") -> dict[str, Any]:
        return {
            "name": skill.name,
            "description": skill.description,
            "location": location,
            "type": "python",
            "envSpec": [
                {"name": name, "required": True, "secret": True} for name in skill.environment_names
            ],
            "prepared": gateway.agent_manager.skill_catalog.is_prepared(skill),
        }

    @router.get("/api/skills")
    async def list_skills(auth: AuthContext = auth_dependency) -> list[dict[str, Any]]:
        del auth
        return [skill_json(skill) for skill in gateway.agent_manager.skill_catalog.skills]

    @router.get("/api/agents/{agent_id}/skills")
    async def list_agent_skills(
        agent_id: str, auth: AuthContext = auth_dependency
    ) -> list[dict[str, Any]]:
        agent = await service.require_agent(auth, agent_id)
        profile = await service.agent_runtime_profile(agent)
        return [skill_json(skill, location="agent") for skill in profile.skills]

    @router.delete("/api/agents/{agent_id}/skills/{skill_name}")
    async def unbind_agent_skill(
        agent_id: str, skill_name: str, auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        agent = await require_mutable_agent(auth, agent_id)
        config = dict(agent.config)
        skill_config = dict(config.get("skills") or {})
        always = [str(item) for item in skill_config.get("alwaysLoad", [])]
        skill_config["alwaysLoad"] = [item for item in always if item != skill_name]
        config["skills"] = skill_config
        updated = agent.model_copy(update={"config": config, "updated_at": datetime.now(UTC)})
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_agent(updated)
        await service.agent_runtime_profile(updated)
        return {"ok": True}

    @router.get("/api/tools")
    async def get_tools(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        del auth
        return {"categories": [], "toolProviders": {}, "tools": {}}

    @router.put("/api/tools")
    async def save_tools_config(
        payload: dict[str, Any], auth: AuthContext = auth_dependency
    ) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        if _find_plaintext_credentials(payload):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "tool credentials must use environment variables",
            )
        return await save_config({"tools": payload}, auth)

    @router.get("/api/tasks")
    async def tasks(auth: AuthContext = auth_dependency) -> list[dict[str, Any]]:
        if auth.identity.role != "super_admin" or auth.identity.auth_method == "apikey":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
        result: list[dict[str, Any]] = []
        for task in gateway.agent_manager.recent_tasks():
            item: dict[str, Any] = {
                "id": task.id,
                "agentId": task.agent_id,
                "chatKey": task.chat_key,
                "status": task.status,
                "createdAt": task.created_at.isoformat(),
            }
            if task.started_at is not None and task.done_at is not None:
                item["duration"] = int((task.done_at - task.started_at).total_seconds() * 1000)
            if task.error:
                item["error"] = task.error
            result.append(item)
        return result

    def unsupported(feature: str) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "ok": False,
                "error": "unsupported",
                "code": "not_implemented",
                "feature": feature,
            },
        )

    def unsupported_handler(feature: str) -> Any:
        async def handler() -> Response:
            return unsupported(feature)

        return handler

    @router.get("/api/skills/search", response_model=None)
    async def search_skills(auth: AuthContext = auth_dependency) -> Response:
        del auth
        return unsupported("remote skill registry")

    @router.post("/api/skills/install", response_model=None)
    async def install_skill(
        payload: dict[str, Any], auth: AuthContext = auth_dependency
    ) -> Response | dict[str, Any]:
        require_mutation(auth)
        name = str(payload.get("name") or "").strip()
        try:
            skill = gateway.agent_manager.skill_catalog.require(name)
        except SkillError:
            return unsupported("remote skill installation")
        agent_id = str(payload.get("agent") or "").strip()
        if not agent_id:
            if auth.identity.role != "super_admin":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
            return {"ok": True, "source": "local", "name": skill.name, "files": 0}
        agent = await require_mutable_agent(auth, agent_id)
        config = dict(agent.config)
        skill_config = dict(config.get("skills") or {})
        always = [str(item) for item in skill_config.get("alwaysLoad", [])]
        skill_config["alwaysLoad"] = list(dict.fromkeys([*always, skill.name]))
        config["skills"] = skill_config
        updated = agent.model_copy(update={"config": config, "updated_at": datetime.now(UTC)})
        async with UnitOfWork(gateway.database) as unit:
            await unit.require_store().save_agent(updated)
        await gateway.agent_manager.reload_profile(updated)
        return {"ok": True, "source": "local", "name": skill.name, "files": 0}

    @router.delete("/api/skills/{skill_name}", response_model=None)
    async def delete_skill(skill_name: str, auth: AuthContext = auth_dependency) -> Response:
        del skill_name
        require_mutation(auth, admin=True)
        return unsupported("global skill deletion")

    for method, path, feature in (
        ("GET", "/api/scoped-channels", "channels"),
        ("POST", "/api/scoped-channels", "channels"),
        ("GET", "/api/channels", "channels"),
        ("GET", "/api/cron", "cron"),
        ("POST", "/api/cron", "cron"),
        ("GET", "/api/agent-bindings", "agent bindings"),
    ):
        router.add_api_route(
            path,
            unsupported_handler(feature),
            methods=[method],
            dependencies=[Depends(require_auth)],
            response_model=None,
        )

    @router.put("/api/scoped-channels/{channel_id}", response_model=None)
    async def update_unsupported_channel(
        channel_id: str, auth: AuthContext = auth_dependency
    ) -> Response:
        del channel_id, auth
        return unsupported("channels")

    @router.delete("/api/scoped-channels/{channel_id}", response_model=None)
    async def delete_unsupported_channel(
        channel_id: str, auth: AuthContext = auth_dependency
    ) -> Response:
        del channel_id, auth
        return unsupported("channels")

    def plugin_json(instance: Any) -> dict[str, Any]:
        return {
            "id": instance.manifest.id,
            "type": instance.manifest.type,
            "version": instance.manifest.version,
            "status": "running"
            if instance.process.running
            else ("error" if instance.error else "stopped"),
            "enabled": instance.enabled,
            "config": {"timeoutSeconds": int(instance.config.get("timeoutSeconds") or 45)},
            "error": instance.error,
        }

    @router.get("/api/plugins")
    async def list_plugins(auth: AuthContext = auth_dependency) -> list[dict[str, Any]]:
        del auth
        return [
            plugin_json(instance) for instance in gateway.agent_manager.plugin_manager.instances
        ]

    @router.put("/api/plugins/{plugin_id}")
    async def update_plugin(
        plugin_id: str,
        payload: PluginUpdate,
        auth: AuthContext = auth_dependency,
    ) -> dict[str, Any]:
        require_mutation(auth, admin=True)
        current = next(
            (
                instance
                for instance in gateway.agent_manager.plugin_manager.instances
                if instance.manifest.id == plugin_id
            ),
            None,
        )
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
        config = dict(current.config)
        if payload.config is not None:
            unknown = set(payload.config) - {"timeoutSeconds"}
            if unknown:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "bundled plugin paths and interpreters are Runtime-managed",
                )
            try:
                timeout_seconds = int(payload.config.get("timeoutSeconds", 45))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "timeoutSeconds must be an integer"
                ) from exc
            if not 1 <= timeout_seconds <= 300:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "timeoutSeconds must be from 1 to 300"
                )
            config["timeoutSeconds"] = timeout_seconds
        instance = await gateway.agent_manager.plugin_manager.configure(
            plugin_id,
            enabled=payload.enabled,
            config=config if payload.config is not None else None,
            restart=payload.restart,
        )
        now = datetime.now(UTC)
        async with UnitOfWork(gateway.database) as unit:
            store = unit.require_store()
            record = await store.find_config(
                kind="plugin", scope="system", scope_id="", name=plugin_id
            )
            await store.save_config(
                ConfigRecord(
                    id=record.id if record else f"cfg_plugin_{secrets.token_hex(8)}",
                    kind="plugin",
                    scope="system",
                    name=plugin_id,
                    enabled=instance.enabled,
                    data={"timeoutSeconds": int(instance.config.get("timeoutSeconds") or 45)},
                    created_at=record.created_at if record else now,
                    updated_at=now,
                )
            )
        return {"ok": True, "plugin": plugin_json(instance)}

    @router.put("/api/cron/{job_id}", response_model=None)
    @router.delete("/api/cron/{job_id}", response_model=None)
    async def mutate_unsupported_cron(job_id: str, auth: AuthContext = auth_dependency) -> Response:
        del job_id, auth
        return unsupported("cron")

    @router.put("/api/agents/{agent_id}/binding", response_model=None)
    async def update_unsupported_binding(
        agent_id: str, auth: AuthContext = auth_dependency
    ) -> Response:
        await service.require_agent(auth, agent_id)
        return unsupported("agent bindings")

    @router.post("/api/chat")
    async def chat(payload: ChatInput, auth: AuthContext = auth_dependency) -> dict[str, str]:
        if auth.identity.read_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "actAs is read-only")
        await service.require_agent(auth, payload.agent_id)
        message = await gateway.agent_manager.chat(
            user_id=auth.identity.effective_user_id,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            message=payload.message,
        )
        return {"response": str(message.content or "")}

    @router.post("/api/chat/stream")
    async def chat_stream(
        payload: ChatInput, auth: AuthContext = auth_dependency
    ) -> StreamingResponse:
        if auth.identity.read_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "actAs is read-only")
        await service.require_agent(auth, payload.agent_id)
        stream = await gateway.agent_manager.stream(
            user_id=auth.identity.effective_user_id,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            message=payload.message,
        )

        async def events() -> AsyncIterator[str]:
            try:
                async for event in stream:
                    yield _sse(_web_event(event))
            finally:
                await stream.aclose()

        return StreamingResponse(events(), media_type="text/event-stream")

    @router.get("/v1/agents")
    async def v1_agents(auth: AuthContext = auth_dependency) -> dict[str, Any]:
        result = await list_agents(auth)
        return {
            "agents": [
                {"id": item["id"], "name": item["name"], "model": item["model"]}
                for item in result["agents"]
            ]
        }

    @router.post("/v1/chat/completions")
    async def v1_chat(
        payload: OpenAIChatInput,
        request: Request,
        auth: AuthContext = auth_dependency,
    ) -> Response:
        if auth.identity.read_only:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "actAs is read-only")
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
        await service.require_agent(auth, agent_id)
        stream = await gateway.agent_manager.stream(
            user_id=auth.identity.effective_user_id,
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            requested_model=payload.model,
        )
        completion_id = f"chatcmpl-{uuid4().hex}"
        if payload.stream:

            async def chunks() -> AsyncIterator[str]:
                try:
                    async for event in stream:
                        if event.type is AgentEventType.CONTENT_DELTA:
                            yield _sse(
                                {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": stream.model,
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

            return StreamingResponse(chunks(), media_type="text/event-stream")
        try:
            async for _ in stream:
                pass
            result = stream.result()
        finally:
            await stream.aclose()
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": stream.model,
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
