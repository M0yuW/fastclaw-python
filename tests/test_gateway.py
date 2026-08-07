from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from fastclaw.agent import AgentEvent, AgentEventType
from fastclaw.app import create_app
from fastclaw.gateway import GatewaySettings
from fastclaw.gateway.router import _web_event, _web_history_message
from fastclaw.identity import hash_api_key, hash_password
from fastclaw.orchestration import TaskSnapshot
from fastclaw.providers import FunctionCall, ToolCall
from fastclaw.runtime import Runtime
from fastclaw.storage import (
    AgentFileRecord,
    AgentRecord,
    APIKeyRecord,
    Database,
    SessionRecord,
    UnitOfWork,
    UserRecord,
)


@asynccontextmanager
async def gateway_client(
    path: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[tuple[httpx.AsyncClient, Database, FastAPI]]:
    settings = GatewaySettings(
        database_url=f"sqlite+aiosqlite:///{path}",
        data_root=path.parent / "data",
        legacy_data_root=path.parent / "legacy",
        port=18954,
        provider_name="fixture",
        provider_api_key="provider-secret",
        provider_api_base="https://llm.test/v1",
        provider_api_type="openai-compatible",
    )
    runtime = Runtime(
        http_client_factory=lambda: (
            httpx.AsyncClient(transport=transport) if transport is not None else httpx.AsyncClient()
        )
    )
    database = Database(settings.database_url)
    app: FastAPI = create_app(runtime, settings=settings, database=database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, database, app


async def onboard(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/onboard",
        json={
            "username": "alice",
            "email": "alice@example.test",
            "password": "correct horse battery staple",
            "displayName": "Alice",
            "provider": "fixture",
            "apiBase": "https://llm.test/v1",
            "apiKey": "provider-secret",
            "model": "fixture/model-1",
            "agentName": "Analyst",
        },
    )
    assert response.status_code == 200
    data = response.json()
    return {"userId": str(data["userId"]), "agentId": str(data["agentId"])}


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/login",
        json={"login": "alice", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    assert "password_hash" not in response.json()["user"]
    assert client.cookies.get("fastclaw_session")


async def test_team_api_is_idempotent_and_enforces_lifecycle(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "teams.db") as (client, database, _):
        await onboard(client)
        await login(client)
        preview = await client.post(
            "/api/agent-teams/preview",
            json={
                "name": "Markets",
                "templateKey": "finance-market-research",
                "clientRequestId": "preview-request",
            },
        )
        assert preview.status_code == 200
        checks = preview.json()["checks"]
        assert checks["skills"]["required"] == ["findata-toolkit-cn", "findata-toolkit-us"]
        assert checks["skills"]["prepared"] is False
        assert "finance-tools.screen_stocks" in checks["tools"]["required"]
        blocked = await client.post(
            "/api/agent-teams",
            json={
                "name": "Markets",
                "templateKey": "finance-market-research",
                "clientRequestId": "blocked-team-request",
            },
        )
        assert blocked.status_code == 422
        assert "prerequisites" in blocked.json()["error"]
        creation = {
            "name": "Custom research",
            "templateKey": "custom",
            "clientRequestId": "team-request-1",
            "specialists": [{"key": "research", "name": "Research specialist"}],
        }
        first = await client.post("/api/agent-teams", json=creation)
        second = await client.post("/api/agent-teams", json=creation)
        assert first.status_code == second.status_code == 201
        team = first.json()["team"]
        assert second.json()["team"]["id"] == team["id"]
        assert len(team["members"]) == 2

        conflict = await client.patch(
            f"/api/agent-teams/{team['id']}", json={"name": "no", "revision": 0}
        )
        assert conflict.status_code == 409
        added = await client.post(
            f"/api/agent-teams/{team['id']}/members",
            json={"key": "review", "name": "Review specialist", "revision": team["revision"]},
        )
        assert added.status_code == 201
        assert len(added.json()["team"]["members"]) == 3
        team = (await client.get(f"/api/agent-teams/{team['id']}")).json()["team"]
        coordinator = next(
            member for member in team["members"] if member["memberType"] == "coordinator"
        )
        coordinator_update = await client.patch(
            f"/api/agent-teams/{team['id']}/members/{coordinator['agentId']}",
            json={
                "agentId": coordinator["agentId"],
                "status": "archived",
                "revision": team["revision"],
            },
        )
        assert coordinator_update.status_code == 422
        research = next(member for member in team["members"] if member["roleKey"] == "research")
        member_update = await client.patch(
            f"/api/agent-teams/{team['id']}/members/{research['agentId']}",
            json={
                "agentId": research["agentId"],
                "status": "archived",
                "revision": team["revision"],
            },
        )
        assert member_update.status_code == 200
        team = member_update.json()["team"]
        archived = await client.post(
            f"/api/agent-teams/{team['id']}/archive", json={"revision": team["revision"]}
        )
        assert archived.status_code == 200
        deleted = await client.request(
            "DELETE",
            f"/api/agent-teams/{team['id']}",
            json={"teamId": team["id"], "revision": archived.json()["team"]["revision"]},
        )
        assert deleted.status_code == 200
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            assert await store.get_team(team["id"]) is None
            deleted_agents = [
                await store.get_agent(member["agentId"]) for member in team["members"]
            ]
            assert all(agent is None for agent in deleted_agents)


def test_sse_tool_result_preserves_call_identity_for_pairing() -> None:
    call = ToolCall(id="call-1", function=FunctionCall(name="read_file", arguments="{}"))
    payload = _web_event(
        AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            turn_id="turn-1",
            message_id="message-1",
            round=0,
            seq=1,
            tool_call=call,
            tool_result="content",
            tool_metadata={"sandbox": True},
            is_error=True,
        )
    )

    assert payload["data"]["id"] == "call-1"
    assert payload["data"]["name"] == "read_file"
    assert payload["data"]["result"] == "content"
    assert payload["data"]["metadata"] == {"sandbox": True}
    assert payload["data"]["isError"] is True

    success = _web_event(
        AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            turn_id="turn-1",
            message_id="message-1",
            round=0,
            seq=2,
            tool_call=call.model_copy(update={"id": "call-2"}),
            tool_result="ok",
        )
    )
    assert "isError" not in success["data"]


def test_web_history_flattens_internal_provider_tool_calls() -> None:
    message = {
        "role": "assistant",
        "content": "",
        "toolCalls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
            }
        ],
        "_raw": {"provider": "fixture"},
    }

    assert _web_history_message(message) == {
        "role": "assistant",
        "content": "",
        "toolCalls": [{"id": "call-1", "name": "read_file", "arguments": '{"path":"a.txt"}'}],
        "_raw": {"provider": "fixture"},
    }


def test_sse_content_delta_preserves_legacy_content_alias() -> None:
    payload = _web_event(
        AgentEvent(
            type=AgentEventType.CONTENT_DELTA,
            turn_id="turn-1",
            message_id="message-1",
            round=0,
            seq=0,
            content="delta",
        )
    )

    assert payload["data"]["delta"] == "delta"
    assert payload["data"]["content"] == "delta"


async def test_onboard_cookie_auth_status_agents_and_masked_provider(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "gateway.db") as (client, database, _app):
        root = await client.get("/")
        before = await client.get("/api/status")
        created = await onboard(client)
        second = await client.post(
            "/api/onboard",
            json={
                "username": "other",
                "email": "other@example.test",
                "password": "long-enough-password",
            },
        )
        invalid_login = await client.post(
            "/api/login", json={"login": "alice", "password": "wrong-password"}
        )
        await login(client)
        me = await client.get("/api/me")
        status = await client.get("/api/status")
        agents = await client.get("/api/agents")
        v1_agents = await client.get("/v1/agents")
        tasks = await client.get("/api/tasks")
        providers = await client.get("/api/providers")
        config = await client.get("/api/config")
        created_key = await client.post(
            "/api/apikeys",
            json={"name": "automation", "agentIds": [created["agentId"]]},
        )
        token = created_key.json()["token"]
        listed_keys = await client.get("/api/apikeys")
        bearer_me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        rejected_provider_secret = await client.post(
            "/api/providers",
            json={
                "name": "deepseek",
                "scope": "user",
                "apiBase": "https://api.deepseek.com",
                "apiKey": "must-not-persist",
            },
        )
        async with UnitOfWork(database) as unit:
            stored_providers = await unit.require_store().list_configs(
                kind="provider", user_id=created["userId"], agent_id=""
            )

        assert root.status_code == 200
        assert before.json()["configured"] is False
        assert second.status_code == 409
        assert invalid_login.json() == {"ok": False, "error": "invalid credentials"}
        assert me.json()["user"]["id"] == created["userId"]
        assert me.json()["authMethod"] == "cookie"
        assert status.json()["configured"] is True
        assert status.json()["running"] is True
        assert status.json()["provider"]["apiKey"] == "prov****cret"
        assert agents.json()["agents"][0]["id"] == created["agentId"]
        assert v1_agents.json() == {
            "agents": [
                {
                    "id": created["agentId"],
                    "name": "Analyst",
                    "model": "fixture/model-1",
                }
            ]
        }
        assert tasks.json() == []
        serialized = json.dumps([providers.json(), config.json()])
        assert "provider-secret" not in serialized
        assert created_key.status_code == 201
        assert token.startswith("fc_")
        assert token not in json.dumps(listed_keys.json())
        assert bearer_me.json()["authMethod"] == "apikey"
        assert rejected_provider_secret.status_code == 400
        assert all("apiKey" not in item.data for item in stored_providers)

        logged_out = await client.post("/api/logout")
        denied = await client.get("/api/me")
        assert logged_out.status_code == 200
        assert denied.status_code == 401


async def test_bearer_api_key_enforces_agent_acl(tmp_path: Path, monkeypatch: Any) -> None:
    async with gateway_client(tmp_path / "acl.db") as (client, database, app):
        created = await onboard(client)
        now = datetime.now(UTC)
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_agent(
                AgentRecord(
                    id="agent-denied",
                    user_id=created["userId"],
                    name="Denied",
                    config={"model": "fixture/model-1"},
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_api_key(
                APIKeyRecord(
                    id="key-1",
                    user_id=created["userId"],
                    name="agent key",
                    key_hash=hash_api_key("fc_agent_secret"),
                    key_prefix="fc_agent",
                )
            )
            await store.set_api_key_agents("key-1", [created["agentId"]])

        headers = {"Authorization": "Bearer fc_agent_secret"}
        task_time = datetime.now(UTC)
        monkeypatch.setattr(
            app.state.agent_manager,
            "recent_tasks",
            lambda: (
                TaskSnapshot(
                    id="allowed-task",
                    user_id=created["userId"],
                    agent_id=created["agentId"],
                    chat_key="allowed-root",
                    status="completed",
                    created_at=task_time,
                ),
                TaskSnapshot(
                    id="denied-agent-task",
                    user_id=created["userId"],
                    agent_id="agent-denied",
                    chat_key="denied-root",
                    status="completed",
                    created_at=task_time,
                ),
                TaskSnapshot(
                    id="other-user-task",
                    user_id="other-user",
                    agent_id=created["agentId"],
                    chat_key="other-root",
                    status="completed",
                    created_at=task_time,
                ),
            ),
        )
        agents = await client.get("/api/agents", headers=headers)
        allowed = await client.get(f"/api/agents/{created['agentId']}", headers=headers)
        denied = await client.get("/api/agents/agent-denied", headers=headers)
        tasks = await client.get("/api/tasks", headers=headers)

        assert [item["id"] for item in agents.json()["agents"]] == [created["agentId"]]
        assert allowed.status_code == 200
        assert denied.status_code == 404
        assert tasks.status_code == 200
        assert [item["id"] for item in tasks.json()] == ["allowed-task"]


async def test_chat_stream_and_history_use_configured_provider(tmp_path: Path) -> None:
    imported_prompts: list[str] = []

    async def provider_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer provider-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "model-1"
        if payload["messages"][0]["role"] == "system":
            imported_prompts.append(payload["messages"][0]["content"])
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"hello "},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"world"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(provider_handler)
    async with gateway_client(tmp_path / "chat.db", transport=transport) as (
        client,
        database,
        _app,
    ):
        tested = await client.post(
            "/api/test-provider",
            json={
                "apiBase": "https://llm.test/v1",
                "apiKey": "provider-secret",
                "apiType": "openai-chat",
                "model": "fixture/model-1",
            },
        )
        created = await onboard(client)
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            agent = await store.get_agent(created["agentId"])
            assert agent is not None
            await store.save_agent(agent.model_copy(update={"config": {}}))
            await store.save_agent_file(
                AgentFileRecord(
                    agent_id=agent.id,
                    user_id=agent.user_id,
                    filename="agent.json",
                    data=b'{"model":"fixture/model-1","maxToolIterations":4}',
                )
            )
            await store.save_agent_file(
                AgentFileRecord(
                    agent_id=agent.id,
                    user_id=agent.user_id,
                    filename="SOUL.md",
                    data=b"You are the imported analyst.",
                )
            )
            await store.save_agent_file(
                AgentFileRecord(
                    agent_id=agent.id,
                    user_id=agent.user_id,
                    filename="IDENTITY.md",
                    data=b"Name: Imported Analyst",
                )
            )
        await login(client)
        provider_list = await client.get("/api/providers", params={"scope": "user"})
        provider_id = provider_list.json()["providers"][0]["id"]
        stored_test = await client.post(
            f"/api/providers/{provider_id}/test", json={"model": "fixture/model-1"}
        )
        response = await client.post(
            "/api/chat/stream",
            json={
                "agentId": created["agentId"],
                "sessionId": "session-1",
                "message": "say hello",
            },
        )
        history = await client.get(
            "/api/chat/history",
            params={"agentId": created["agentId"], "sessionId": "session-1"},
        )
        non_stream = await client.post(
            "/api/chat",
            json={
                "agentId": created["agentId"],
                "sessionId": "session-2",
                "message": "say hello without streaming",
            },
        )

        assert tested.json() == {"ok": True}
        assert stored_test.json() == {"ok": True}
        assert response.status_code == 200
        assert non_stream.json() == {"reply": "hello world"}
        assert len(imported_prompts) == 2
        assert "You are the imported analyst." in imported_prompts[0]
        assert "Name: Imported Analyst" in imported_prompts[0]
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event["type"] for event in events] == [
            "content_delta",
            "content_delta",
            "content",
            "done",
        ]
        assert events[-1]["data"]["seq"] == 3
        messages = history.json()["history"]
        assert [message["role"] for message in messages] == ["system", "user", "assistant"]
        assert "You are the imported analyst." in messages[0]["content"]
        assert messages[-1]["content"] == "hello world"


async def test_admin_act_as_is_tenant_scoped_and_read_only(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "admin.db") as (client, database, _app):
        created = await onboard(client)
        now = datetime.now(UTC)
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_user(
                UserRecord(
                    id="usr_benchmark",
                    username="benchmark",
                    email="benchmark@example.test",
                    password_hash=hash_password("benchmark password"),
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_agent(
                AgentRecord(
                    id="agent-benchmark",
                    user_id="usr_benchmark",
                    name="Benchmark Coordinator",
                    config={"model": "fixture/model-1"},
                    created_at=now,
                    updated_at=now,
                )
            )
        await login(client)

        users = await client.get("/api/admin/users")
        all_agents = await client.get("/api/admin/agents")
        acting = {"x-fastclaw-act-as": "usr_benchmark"}
        me = await client.get("/api/me", headers=acting)
        agents = await client.get("/api/agents", headers=acting)
        mutation = await client.put(
            "/api/agents/agent-benchmark",
            headers=acting,
            json={"name": "must not change"},
        )
        chat = await client.post(
            "/api/chat",
            headers=acting,
            json={"agentId": "agent-benchmark", "sessionId": "read-only", "message": "no"},
        )

        assert {item["id"] for item in users.json()["users"]} == {
            created["userId"],
            "usr_benchmark",
        }
        benchmark = next(
            item for item in all_agents.json()["agents"] if item["id"] == "agent-benchmark"
        )
        assert benchmark["ownerUsername"] == "benchmark"
        assert me.json()["actAsUserId"] == "usr_benchmark"
        assert me.json()["readOnly"] is True
        assert [item["id"] for item in agents.json()["agents"]] == ["agent-benchmark"]
        assert mutation.status_code == 403
        assert chat.status_code == 403


async def test_agent_system_files_config_and_workspace_listing(tmp_path: Path) -> None:
    database_path = tmp_path / "files.db"
    async with gateway_client(database_path) as (client, _, _app):
        created = await onboard(client)
        agent_id = created["agentId"]
        base_dir = tmp_path / "data" / "agents" / agent_id
        base_dir.mkdir(parents=True)
        (base_dir / "SOUL.md").write_text("base soul", encoding="utf-8")
        workspace = tmp_path / "data" / "workspaces" / agent_id
        workspace.mkdir(parents=True)
        (workspace / "report.md").write_text("report", encoding="utf-8")
        await login(client)

        base = await client.get(f"/api/agents/{agent_id}/system-files/SOUL.md")
        uploaded = await client.post(
            f"/api/agents/{agent_id}/files",
            params={"sessionId": "session-1"},
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        rejected_path = await client.post(
            f"/api/agents/{agent_id}/files",
            files={"file": ("../escape.txt", b"no", "text/plain")},
        )
        saved = await client.put(
            f"/api/agents/{agent_id}/system-files/SOUL.md",
            json={"content": "edited soul"},
        )
        override = await client.get(f"/api/agents/{agent_id}/system-files/SOUL.md")
        updated = await client.put(
            f"/api/agents/{agent_id}",
            json={"description": "updated", "policy": "delegate-only"},
        )
        config = await client.get(f"/api/agents/{agent_id}/config")
        files = await client.get(f"/api/agents/{agent_id}/files")
        downloaded = await client.get(f"/api/agents/{agent_id}/files/report.md")
        reverted = await client.delete(f"/api/agents/{agent_id}/system-files/SOUL.md")
        fallback = await client.get(f"/api/agents/{agent_id}/system-files/SOUL.md")

        assert base.json() == {"content": "base soul", "source": "fs"}
        assert uploaded.json()["files"] == [{"path": "sessions/session-1/evidence.txt", "size": 8}]
        assert rejected_path.status_code == 400
        assert saved.json() == {"ok": True}
        assert override.json() == {
            "content": "edited soul",
            "source": "db",
            "baseContent": "base soul",
        }
        assert updated.json()["agent"]["description"] == "updated"
        assert config.json()["policy"] == "delegate-only"
        assert files.json()["files"][0]["path"] == "report.md"
        assert downloaded.text == "report"
        assert reverted.json() == {"ok": True}
        assert fallback.json() == {"content": "base soul", "source": "fs"}


async def test_session_management_config_and_unsupported_envelopes(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "sessions.db") as (client, database, _app):
        created = await onboard(client)
        now = datetime.now(UTC)
        async with UnitOfWork(database) as unit:
            await unit.require_store().save_session(
                SessionRecord(
                    user_id=created["userId"],
                    agent_id=created["agentId"],
                    key="session-1",
                    title="Old",
                    created_at=now,
                    updated_at=now,
                )
            )
        await login(client)

        renamed = await client.put(
            "/api/chat/sessions/session-1",
            json={"agentId": created["agentId"], "title": "Renamed"},
        )
        sessions = await client.get("/api/chat/sessions", params={"agentId": created["agentId"]})
        safe_config = await client.post(
            "/api/config", json={"agents": {"defaults": {"maxTokens": 2048}}}
        )
        leaked_config = await client.post(
            "/api/config", json={"providers": {"deepseek": {"apiKey": "plaintext"}}}
        )
        plugins = await client.get("/api/plugins")
        disabled_plugin = await client.put("/api/plugins/finance-tools", json={"enabled": False})
        persisted_plugin = await client.get("/api/plugins")
        rejected_plugin_path = await client.put(
            "/api/plugins/finance-tools",
            json={"config": {"pythonBin": "/tmp/attacker"}},
        )
        unsupported = await client.get("/api/channels")
        deleted = await client.delete(
            "/api/chat/sessions/session-1", params={"agentId": created["agentId"]}
        )

        assert renamed.json() == {"ok": True}
        assert sessions.json()["sessions"][0]["title"] == "Renamed"
        assert safe_config.json() == {"ok": True}
        assert leaked_config.status_code == 400
        assert plugins.json()[0]["id"] == "finance-tools"
        assert plugins.json()[0]["status"] == "running"
        assert disabled_plugin.json()["plugin"]["enabled"] is False
        assert persisted_plugin.json()[0]["status"] == "stopped"
        assert rejected_plugin_path.status_code == 400
        assert unsupported.status_code == 501
        assert unsupported.json()["code"] == "not_implemented"
        assert deleted.json() == {"ok": True}


async def test_plugin_enablement_persists_across_gateway_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "plugin-persistence.db"
    async with gateway_client(database_path) as (client, _database, _app):
        await onboard(client)
        await login(client)
        response = await client.put("/api/plugins/finance-tools", json={"enabled": False})
        assert response.status_code == 200

    async with gateway_client(database_path) as (client, _database, _app):
        await login(client)
        response = await client.get("/api/plugins")

        assert response.status_code == 200
        assert response.json()[0]["enabled"] is False
        assert response.json()[0]["status"] == "stopped"
